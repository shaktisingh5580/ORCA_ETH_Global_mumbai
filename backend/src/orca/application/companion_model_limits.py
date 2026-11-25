"""Purpose: reserve a Gemini admission under both companion rate limits.
Behavior: use atomic database upserts in one transaction; denial rolls back both counters.
Refs: main implementation plan P12.2; A-API §§14,17.
"""

from datetime import datetime
from typing import cast

from sqlalchemy import text

from orca.application.companion import Conversations


class ModelRateLimited(Exception):
    pass


def reserve(repo: Conversations, *, user_limit: int, organization_limit: int) -> None:
    # A single database timestamp defines both fixed windows. Keep lock order
    # organization -> user across all callers and commit before any provider call.
    window = cast(
        datetime,
        repo.session.execute(text("SELECT date_trunc('minute', clock_timestamp())")).scalar_one(),
    )
    identity = repo.principal
    if not identity.issuer or not identity.subject:
        raise ValueError("Model admission requires a verified issuer and subject")
    for issuer, subject, limit in (
        ("", "", organization_limit),
        (identity.issuer, identity.subject, user_limit),
    ):
        admitted = repo.session.execute(
            text("""INSERT INTO research.companion_model_counters AS c
          (tenant_id,issuer,subject,window_start,used) VALUES (:tenant,:issuer,:subject,:window,1)
          ON CONFLICT (tenant_id,issuer,subject) DO UPDATE SET window_start=EXCLUDED.window_start,
          used=CASE WHEN c.window_start<EXCLUDED.window_start THEN 1 ELSE c.used+1 END
          WHERE c.window_start<EXCLUDED.window_start
             OR (c.window_start=EXCLUDED.window_start AND c.used<:limit)
          RETURNING used"""),
            {
                "tenant": repo.tenant,
                "issuer": issuer,
                "subject": subject,
                "window": window,
                "limit": limit,
            },
        ).scalar_one_or_none()
        if admitted is None:
            raise ModelRateLimited
