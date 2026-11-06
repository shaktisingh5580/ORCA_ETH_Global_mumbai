"""Purpose: retrieve an invitation for deliberate operator delivery outside the public API.
Behavior: verify current owner/admin membership and print a single private acceptance token locally.
Refs: API 3.2; main plan P02; Phase: P02 completion repair; operator tooling.
"""

import argparse
import hmac
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text

from orca.application.invitations import invitation_token, token_hash
from orca.application.organizations import Organizations
from orca.config import Settings
from orca.domain.identity import Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.tenant_context import assert_runtime_role
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Private invitation handoff; do not capture output in logs"
    )
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--organization-id", required=True, type=UUID)
    parser.add_argument("--invitation-id", required=True, type=UUID)
    parser.add_argument("--operator-subject", required=True)
    args = parser.parse_args()
    settings = Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    if not settings.database_url or not settings.invitation_signing_key:
        raise SystemExit("Runtime database and private invitation signing key required")
    db = create_runtime_engine(settings.database_url)
    try:
        with db.begin() as connection:
            assert_runtime_role(connection)
            tenant = connection.execute(
                text("SELECT iam.resolve_tenant(:subject,:org)"),
                {"subject": args.operator_subject, "org": args.organization_id},
            ).scalar_one_or_none()
        if tenant is None:
            raise SystemExit("Organization unavailable to operator")
        with TenantUnitOfWork(db, tenant) as session:
            roles = Organizations(session, tenant, args.organization_id).authorize(
                Principal(args.operator_subject, "local-operator", frozenset({"org:members"})),
                "org:members",
            )
            row = (
                session.execute(
                    text("SELECT * FROM iam.invitations WHERE tenant_id=:t AND id=:id"),
                    {"t": tenant, "id": args.invitation_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["accepted_at"] or row["expires_at"] <= datetime.now(timezone.utc):
                raise SystemExit("Invitation unavailable or expired")
            if row["role"] in {"org_owner", "source_operator"} and "org_owner" not in roles:
                raise SystemExit("Owner authority required for this invitation")
            token = invitation_token(
                settings.invitation_signing_key.get_secret_value(),
                row["id"],
                row["email"],
                row["expires_at"],
            )
            if not hmac.compare_digest(token_hash(token), row["token_hash"]):
                raise SystemExit("Invitation signing key changed; issue a new invitation")
            print(token)
    finally:
        db.dispose()


if __name__ == "__main__":
    main()
