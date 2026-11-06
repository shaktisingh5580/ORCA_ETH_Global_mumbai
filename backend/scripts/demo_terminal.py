"""Purpose: present the working cloud-research-to-Arkiv demo entirely in a terminal.
Behavior: read real cloud bytes, recompute the stored result and independently verify Arkiv.
Refs: accelerated-research-arkiv-demo.json; Phase: accelerated research/P11 demo.
"""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

from dotenv import dotenv_values
from sqlalchemy import text

from orca.application.demo import analyze_snapshot, result_digest
from orca.application.snapshots import SnapshotRepository
from orca.config import Settings
from orca.contracts.demo import DemoResult, PublicDemoResult
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.storage.composition import snapshot_store

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only live ORCA research and Arkiv demonstration"
    )
    parser.add_argument("--pause", action="store_true", help="Wait between steps for presentation")
    parser.add_argument(
        "--show-daily", action="store_true", help="Print the complete daily sample table"
    )
    args = parser.parse_args()

    def step(number: int, title: str) -> None:
        if args.pause and number > 1:
            input("\nPress Enter for the next step...")
        print(f"\n[{number}/5] {title}", flush=True)

    print("ORCA | Real marine research -> public Arkiv reference", flush=True)
    print(
        "Read-only operator demo. Uses Supabase runtime credentials; no browser or API session required."
    )
    print("No new chain write. No synthetic source data. Scientific validation remains pending.")
    summary = json.loads((ROOT / ".cache/demo/cloud-demo.json").read_text(encoding="utf-8"))
    receipt = json.loads(
        (ROOT / "docs/gates/DEMO/arkiv-publication.json").read_text(encoding="utf-8")
    )
    runtime = dotenv_values(ROOT / ".env.demo")
    os.environ.update({key: value for key, value in runtime.items() if value is not None})
    settings = Settings()  # type: ignore[call-arg]
    assert settings.database_url
    engine = create_runtime_engine(settings.database_url)
    store = snapshot_store(settings)
    tenant = UUID(summary["tenant_id"])
    try:
        step(1, "Load real snapshot provenance and private result from Supabase")
        with TenantUnitOfWork(engine, tenant) as session:
            manifest = SnapshotRepository(session, tenant).get(UUID(summary["snapshot_id"]))
            row: Any = (
                session.execute(
                    text(
                        "SELECT manifest,result_digest FROM research.demo_results WHERE tenant_id=:tenant AND id=:id"
                    ),
                    {"tenant": tenant, "id": UUID(summary["result_id"])},
                )
                .mappings()
                .one_or_none()
            )
            if manifest is None or row is None:
                raise ValueError("Prepared cloud snapshot/result unavailable")
            stored = DemoResult.model_validate(row["manifest"])
            if (
                stored.id != UUID(summary["result_id"])
                or stored.snapshot_id != manifest.id
                or result_digest(stored) != row["result_digest"]
            ):
                raise ValueError("Stored cloud result identity or digest mismatch")
        print("Source: NASA JPL MUR foundation SST, historical access sample")
        print("Snapshot:", manifest.id)
        print("Exact source digest:", manifest.raw_content_digest)

        step(2, "Read preserved source bytes from private cloud storage")
        content = store.read(tenant, manifest.raw_content_digest)
        print(f"Read {len(content):,} bytes; SHA-256 verified against the snapshot manifest.")

        step(3, "Recompute the descriptive SST result and compare its digest")
        computed = analyze_snapshot(manifest, content)
        computed_digest = result_digest(computed)
        if (
            computed_digest != row["result_digest"]
            or computed_digest != receipt["result_digest"]
            or str(computed.id) != receipt["result_id"]
        ):
            raise ValueError("Recomputed result differs from the cloud/Arkiv publication receipt")
        print(f"Observations: {computed.finite_count:,} / {computed.row_count:,} rows")
        print(f"Sample mean: {computed.sample_mean:.4f} C")
        print(f"Minimum: {computed.minimum:.4f} C | Maximum: {computed.maximum:.4f} C")
        print("Daily values:", len(computed.daily_sample_means))
        print("Result digest:", computed_digest)
        print("MATCH: recomputed result = stored private result = published result reference")
        if args.show_daily:
            for day, temperature in computed.daily_sample_means.items():
                print(f"  {day}  {temperature:.4f} C")

        step(4, "Read Arkiv directly using a separate read-only SDK process")
        if not manifest.product_version.license.can_redistribute:
            raise ValueError("Source rights do not permit this public reference")
        public = PublicDemoResult(result_id=computed.id, result_digest=computed_digest)
        public_path = ROOT / ".cache/demo/terminal-public-result.json"
        public_path.write_text(public.model_dump_json(indent=2), encoding="utf-8")
        node = shutil.which("node")
        if not node:
            raise ValueError("Node.js is required for the Arkiv read")
        arkiv = dotenv_values(ROOT / ".env.arkiv")
        child_env = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "PATH",
                "PATHEXT",
                "SYSTEMROOT",
                "SYSTEMDRIVE",
                "WINDIR",
                "TEMP",
                "TMP",
                "USERPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
                "NODE_EXTRA_CA_CERTS",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "NODE_USE_ENV_PROXY",
            }
        }
        child_env["ARKIV_ACCESS_KEY"] = arkiv.get("ARKIV_ACCESS_KEY") or ""
        # The read subprocess receives no wallet private key, Supabase credentials or user token.
        command = [
            node,
            str(ROOT / "arkiv/src/cli.mjs"),
            "read",
            "--key",
            receipt["entity_key"],
            "--owner",
            receipt["owner"],
            "--file",
            str(public_path),
        ]
        result = subprocess.run(
            command, cwd=ROOT / "arkiv", env=child_env, capture_output=True, text=True, timeout=60
        )
        if result.returncode:
            diagnostic = (
                (result.stdout + result.stderr).replace(child_env["ARKIV_ACCESS_KEY"], "[REDACTED]")
                if child_env["ARKIV_ACCESS_KEY"]
                else result.stdout + result.stderr
            )
            (ROOT / ".cache/demo/terminal-arkiv-error.log").write_text(diagnostic, encoding="utf-8")
            print(
                "Arkiv read unavailable. Check RPC access and entity expiry; no verification success is claimed."
            )
            return 1
        verified = json.loads(result.stdout)
        if (
            verified["resultDigest"] != computed_digest
            or verified["verification"] != "schema_and_wallet_owner_verified"
        ):
            raise ValueError("Independent Arkiv read mismatch")
        print("Entity:", verified["entityKey"])
        print("Owner:", verified["owner"])
        print("MATCH: schema, wallet owner, result ID and public export digests verified")
        print("Transaction:", receipt["transaction_hash"])

        step(5, "Explain what this proves and what remains future work")
        print(
            "LIVE DEMO COMPLETE: preserved cloud bytes -> recomputed result -> matching Arkiv reference"
        )
        print(
            "Supabase holds private source/result data. Arkiv exposes the minimal public reference."
        )
        for limitation in computed.limitations:
            print("LIMIT:", limitation)
        print(
            "Future: independent agent tasks, claims, assignments and review references; ENS binding."
        )
        print(
            "Wallet ownership is not institutional identity. Recalculation is not independent scientific validation."
        )
        return 0
    finally:
        engine.dispose()
        close = getattr(store.client, "close", None)
        if close:
            close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:
        print("Demo stopped:", type(error).__name__)
        print(
            "Check local configuration, cloud connectivity and Arkiv availability. Credentials are not printed."
        )
        raise SystemExit(1) from None
