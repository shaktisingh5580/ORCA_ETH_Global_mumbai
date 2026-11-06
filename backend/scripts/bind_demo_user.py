"""Purpose: authorize an existing real Supabase user for the prepared research demo.
Behavior: verify Auth identity and add researcher membership without inventing users or credentials.
Refs: accelerated-research-arkiv-demo.json; Phase: accelerated P02/P04 demo integration.
"""

import argparse
import json
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--subject", type=UUID, required=True, help="Actual Supabase Auth user UUID"
    )
    args = parser.parse_args()
    summary = json.loads((ROOT / ".cache/demo/cloud-demo.json").read_text(encoding="utf-8"))
    values = dotenv_values(ROOT / ".env")
    admin = values.get("ORCA_MIGRATION_DATABASE_URL") or values.get("ORCA_TEST_DATABASE_URL")
    if not admin:
        raise ValueError("Admin configuration missing")
    with psycopg.connect(admin, connect_timeout=10) as connection:
        if (
            connection.execute("SELECT 1 FROM auth.users WHERE id=%s", (args.subject,)).fetchone()
            is None
        ):
            raise ValueError("User must exist in Supabase Auth")
        connection.execute(
            """INSERT INTO iam.memberships(id,tenant_id,subject,role)
          VALUES(%s,%s,%s,'researcher') ON CONFLICT(tenant_id,subject) DO NOTHING""",
            (uuid4(), UUID(summary["tenant_id"]), str(args.subject)),
        )
    print("Real user linked to demo organization:", summary["organization_id"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("Demo membership unavailable:", type(error).__name__)
        raise SystemExit(1) from None
