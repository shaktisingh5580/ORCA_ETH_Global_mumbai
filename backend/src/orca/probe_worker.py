"""Purpose: execute durable bounded metadata probes through approved ERDDAP bindings.
Behavior: commit claims before network I/O and publish sanitized evidence under expiring tokens.
Refs: API 3.4 source-probes; main plan P03.2/P03.4; Phase: P03.
"""

import argparse
import hashlib
import json
import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.source_probes import SourceProbes
from orca.config import Settings
from orca.contracts.acquisition import SourceBinding
from orca.contracts.erddap_metadata import parse_metadata
from orca.contracts.source_probes import ProbeRequest, SourceProbeEvidence
from orca.infrastructure.connectors.erddap import ERDDAP, SourceError
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork


def run_once(
    tenant: UUID,
    transaction: Callable[[], AbstractContextManager[Session]],
    bindings: tuple[SourceBinding, ...],
) -> str:
    with transaction() as session:
        claim = SourceProbes(session, tenant).claim()
        if claim is None:
            return "idle"
        body = ProbeRequest.model_validate(claim["payload"])
        stored: Any = session.execute(
            text(
                "SELECT metadata_digest FROM research.catalog_versions WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": tenant, "id": body.distribution_id},
        ).scalar_one()
    evidence = None
    try:
        binding = next(
            (b for b in bindings if b.tenant_id == tenant and b.version_id == body.distribution_id),
            None,
        )
        if binding is None:
            raise ValueError("distribution not configured")
        client = ERDDAP(
            binding.host,
            frozenset({binding.host}),
            max_bytes=body.limits.max_bytes,
            timeout=body.limits.timeout_seconds,
        )
        raw = client._get(f"/erddap/info/{binding.dataset}/index.json")
        variables = parse_metadata(json.loads(raw))
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        evidence = SourceProbeEvidence(
            response_digest=digest,
            response_bytes=len(raw),
            retrieved_at=datetime.now(timezone.utc),
            metadata_matches_pinned_version=digest == stored,
            variable_count=len(variables),
        )
    except (SourceError, ValueError, KeyError, TypeError):
        pass
    with transaction() as session:
        completed = SourceProbes(session, tenant).finish(
            claim["id"], claim["lease_token"], evidence
        )
    return ("succeeded" if evidence else "failed") if completed else "lease_lost"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run approved metadata source probes")
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    settings = Settings()  # type: ignore[call-arg]
    if not settings.source_probes_enabled or not settings.database_url:
        raise ValueError("source probes are disabled")
    engine = create_runtime_engine(settings.database_url)
    logging.basicConfig(level=logging.INFO)
    try:
        while True:
            try:
                outcome = run_once(
                    args.tenant,
                    lambda: TenantUnitOfWork(engine, args.tenant),
                    settings.acquisition_sources,
                )
            except Exception:
                logging.error("SOURCE_PROBE_INFRASTRUCTURE_ERROR; lease remains recoverable")
                if not args.watch:
                    return 1
                time.sleep(10)
                continue
            logging.info("source probe outcome=%s", outcome)
            if not args.watch:
                return 0 if outcome in {"idle", "succeeded"} else 1
            if outcome != "succeeded":
                time.sleep(5)
    except KeyboardInterrupt:
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
