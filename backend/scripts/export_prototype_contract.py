"""Purpose: give the existing dashboard developer a reproducible implemented prototype contract.
Behavior: export enabled route schemas offline without loading private environment files.
Refs: HACKATHON_IMPLEMENTATION_PLAN.md H03; FRONTEND_BACKEND_HANDOFF.md; Phase: H03.
"""

import argparse
import importlib
import json
import sys
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr

from orca.api.app import create_app
from orca.api.routers.network import router as network_router
from orca.config import Settings

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "contracts/openapi/orca-prototype.json"


def contract(include_demo: bool = False) -> str:
    app = create_app(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            monitoring_token=SecretStr("contract-export-only-not-a-runtime-token"),
            database_url="postgresql+psycopg://contract:contract@127.0.0.1:15432/orca_test",
            object_store_endpoint="http://127.0.0.1:59000",
            object_store_access_key=SecretStr("contract-export-only"),
            object_store_secret_key=SecretStr("contract-export-only"),
            research_demo_enabled=True,
            file_uploads_enabled=True,
            prototype_enabled=True,
            prototype_bundle_digest="sha256:" + "0" * 64,
            prototype_source_tenant=UUID(int=1),
        )
    )
    # Export the explicitly enabled handoff profile without loading private runtime identities.
    app.include_router(network_router)
    if include_demo:
        # Contract generation is development tooling; runtime wheels never import fixtures.
        sys.path.insert(0, str(ROOT / "backend/tests/support"))
        app.include_router(importlib.import_module("disagreement_demo").router)
    try:
        return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    finally:
        app.state.runtime_engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--include-demo", action="store_true")
    args = parser.parse_args()
    if args.include_demo:
        TARGET = ROOT / "contracts/openapi/orca-hackathon-demo.json"
    value = contract(args.include_demo)
    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != value:
            raise SystemExit("Prototype OpenAPI drift; regenerate the implemented contract")
    else:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(value, encoding="utf-8", newline="\n")
