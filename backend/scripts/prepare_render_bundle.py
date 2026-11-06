"""Purpose: prepare a reviewable private Render secret bundle without committing credentials.
Behavior: retain existing agent identities, remove local paths and emit only setup/status metadata.
Refs: H05 cloud deployment decision; RENDER_DEPLOYMENT_HANDOFF.md; Phase: H05.
"""

import argparse
import json
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, SecretStr

from orca.cloud_runtime import CloudBundle, env_text
from orca.config import Settings
from orca.infrastructure.federation.runtime import ROOT, NetworkConfiguration
from orca.infrastructure.federation.signing import strict_json


def serializable(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, BaseModel):
        return serializable(value.model_dump())
    if isinstance(value, dict):
        return {name: serializable(item) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.demo")
    parser.add_argument("--frontend-origin", default="")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".cache/render")
    args = parser.parse_args()
    settings = Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    if not settings.network_config_file:
        raise ValueError("existing pinned agent configuration required")
    bindings = NetworkConfiguration.model_validate(
        strict_json(Path(settings.network_config_file).read_bytes())
    )
    values = dotenv_values(settings.network_wallet_env_file)
    coordinator = {
        name: str(values.get(name) or "") for name in ("ARKIV_PRIVATE_KEY", "ARKIV_ACCESS_KEY")
    }
    providers = {}
    for role in ("ocean", "reviewer"):
        configured = dotenv_values(ROOT / f".env.provider-{role}")
        wallet = dotenv_values(Path(str(configured["ORCA_PROVIDER_WALLET_ENV_FILE"])))
        providers[role] = {
            "service_private_key": str(configured["ORCA_PROVIDER_PRIVATE_KEY"]),
            "wallet_private_key": str(wallet["ARKIV_PRIVATE_KEY"]),
            "access_key": str(wallet.get("ARKIV_ACCESS_KEY") or ""),
        }
    bundle = CloudBundle(
        schema_version="orca.cloud-runtime-bundle/1.0",
        settings=serializable(settings),
        bindings=bindings,
        coordinator_wallet=coordinator,
        providers=providers,
    )
    folder = args.output_dir
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "runtime-bundle.json"
    target.write_text(bundle.model_dump_json(), encoding="utf-8", newline="\n")
    target.chmod(0o600)
    envfile = folder / "render.env"
    envfile.write_text(
        env_text(
            {
                "ORCA_RUNTIME_BUNDLE": bundle.model_dump_json(),
                "ORCA_FRONTEND_ORIGIN": args.frontend_origin,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    envfile.chmod(0o600)
    print(
        json.dumps(
            {
                "bundle_file": str(target),
                "environment_import_file": str(envfile),
                "frontend_origin_configured": bool(args.frontend_origin),
                "contains_private_credentials": True,
                "commit_or_share_with_frontend": False,
                "deployed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
