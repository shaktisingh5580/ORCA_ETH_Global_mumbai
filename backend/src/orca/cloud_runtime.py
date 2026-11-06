"""Purpose: run the complete team-operated prototype on a persistent cloud service.
Behavior: restore verified source bytes, materialize scoped secrets and supervise API/agent/workers.
Refs: H05-cloud-and-disagreement-demo.json; Render deployment handoff; Phase: H05.
"""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from orca.config import Settings
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.federation.runtime import NetworkConfiguration, load_runtime
from orca.infrastructure.federation.signing import strict_json
from orca.infrastructure.storage.composition import snapshot_store


class CloudBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: str
    settings: dict[str, Any]
    bindings: NetworkConfiguration
    coordinator_wallet: dict[str, str]
    providers: dict[str, dict[str, str]]


def private_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
    path.chmod(0o600)


def read_bundle() -> bytes:
    """Load the deployment secret from exactly one private runtime source."""
    raw = os.environ.get("ORCA_RUNTIME_BUNDLE", "")
    filename = os.environ.get("ORCA_RUNTIME_BUNDLE_FILE", "")
    if raw and filename:
        raise ValueError("configure only one runtime bundle source")
    if filename:
        with Path(filename).open("rb") as stream:
            value = stream.read(1_048_577)
    elif raw:
        value = raw.encode("utf-8")
    else:
        raise ValueError("private runtime bundle required")
    if not value or len(value) > 1_048_576:
        raise ValueError("invalid runtime bundle size")
    return value


def env_text(values: dict[str, Any]) -> str:
    return (
        "\n".join(
            f"{key}={json.dumps(str(value), ensure_ascii=True)}" for key, value in values.items()
        )
        + "\n"
    )


def settings_environment(settings: Settings) -> dict[str, str]:
    output = {}
    for name, value in settings.model_dump().items():
        if value is None:
            continue
        if hasattr(value, "get_secret_value"):
            serialized = value.get_secret_value()
        elif isinstance(value, (list, tuple, dict)):
            serialized = json.dumps(settings.model_dump(mode="json")[name])
        elif isinstance(value, bool):
            serialized = "true" if value else "false"
        else:
            serialized = str(value)
        output["ORCA_" + name.upper()] = serialized
    return output


def materialize(
    bundle: CloudBundle, state: Path, frontend_origin: str
) -> tuple[Path, list[tuple[str, list[str]]]]:
    if bundle.schema_version != "orca.cloud-runtime-bundle/1.0":
        raise ValueError("unsupported cloud secret bundle")
    if frontend_origin:
        parsed = urlsplit(frontend_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("an exact HTTPS frontend origin without a path is required")
    bundle.bindings.validate_endpoints()
    state.mkdir(parents=True, exist_ok=True)
    bindings_file = state / "operator-bindings.json"
    private_file(bindings_file, bundle.bindings.model_dump_json(indent=2))
    wallet = state / "coordinator-wallet.env"
    private_file(wallet, env_text(bundle.coordinator_wallet))
    configured = dict(bundle.settings)
    configured.update(
        {
            "environment": "production",
            "cors_origins": [frontend_origin] if frontend_origin else [],
            "cors_local_development": False,
            "cors_lan_development": False,
            "docs_public": False,
            "network_config_file": str(bindings_file),
            "network_wallet_env_file": str(wallet),
            "network_state_dir": str(state / "coordinator"),
        }
    )
    settings = Settings.model_validate(configured)
    api_env = state / "api.env"
    private_file(api_env, env_text(settings_environment(settings)))
    sources = state / "sources"
    commands = []
    for role, capability in (("ocean", "sst_reproduction"), ("reviewer", "sst_review")):
        matches = [agent for agent in bundle.bindings.agents if agent.capability == capability]
        if len(matches) != 1:
            raise ValueError("one producer and one reviewer required in this cloud profile")
        binding = matches[0]
        secrets = bundle.providers[role]
        binding_file = state / f"{role}-binding.json"
        private_file(binding_file, binding.model_dump_json())
        wallet_file = state / f"{role}-wallet.env"
        private_file(
            wallet_file,
            env_text(
                {
                    "ARKIV_PRIVATE_KEY": secrets["wallet_private_key"],
                    "ARKIV_ACCESS_KEY": secrets.get("access_key", ""),
                }
            ),
        )
        provider_env = state / f"{role}.env"
        private_file(
            provider_env,
            env_text(
                {
                    "ORCA_PROVIDER_BINDING_FILE": str(binding_file),
                    "ORCA_PROVIDER_COORDINATOR_FILE": str(bindings_file),
                    "ORCA_PROVIDER_PRIVATE_KEY": secrets["service_private_key"],
                    "ORCA_PROVIDER_WALLET_ENV_FILE": str(wallet_file),
                    "ORCA_PROVIDER_CAPTURE_FOLDER": str(sources),
                    "ORCA_PROVIDER_STATE_DIR": str(state / role),
                }
            ),
        )
        commands.append(
            (role, [sys.executable, "-m", "orca.agent_provider", "--env-file", str(provider_env)])
        )
    # Validate the private/public coordinator key binding before starting services.
    load_runtime(settings)
    commands.extend(
        (name, [sys.executable, "-m", module, "--env-file", str(api_env)])
        for name, module in (
            ("network-worker", "orca.network_worker"),
            ("research-worker", "orca.prototype_worker"),
        )
    )
    if settings.file_uploads_enabled:
        commands.append(
            (
                "upload-worker",
                [sys.executable, "-m", "orca.upload_worker", "--env-file", str(api_env)],
            )
        )
    return api_env, commands


def restore_sources(settings: Settings, folder: Path) -> None:
    if not settings.prototype_source_tenant or not settings.prototype_bundle_digest:
        raise ValueError("pinned source configuration required")
    store = snapshot_store(settings)
    tenant = settings.prototype_source_tenant
    bundle = strict_json(store.read(tenant, settings.prototype_bundle_digest))
    folder.mkdir(parents=True, exist_ok=True)
    members = bundle["members"]
    if len(members) != 21 or {member["year"] for member in members} != {2025, *range(2003, 2023)}:
        raise ValueError("unsupported source capture set")
    entries = [
        ("metadata.json", bundle["metadata_digest"]),
        *[(f"{member['year']}-01.csv", member["sha256"]) for member in members],
    ]
    for filename, digest in entries:
        path = folder / filename
        if path.exists() and "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            continue
        raw = store.read(tenant, digest)
        path.write_bytes(raw)


def child_environment() -> dict[str, str]:
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "LANG",
        "LC_ALL",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "ORCA_REPOSITORY_ROOT",
        "ORCA_DEMO_STATE_DIR",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", PYTHONUNBUFFERED="1")
    return environment


def await_provider(command: list[str], process: subprocess.Popen[bytes]) -> None:
    """Wait for the bound A2A card before allowing dependent workers to take jobs."""
    from dotenv import dotenv_values

    values = dotenv_values(command[command.index("--env-file") + 1])
    binding = strict_json(Path(str(values["ORCA_PROVIDER_BINDING_FILE"])).read_bytes())
    endpoint = urlsplit(binding["endpoint"])
    card = f"{endpoint.scheme}://{endpoint.netloc}/.well-known/agent-card.json"
    deadline = time.monotonic() + 45
    with httpx.Client(timeout=2, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("provider exited before readiness")
            try:
                response = client.get(card)
                if (
                    response.status_code == 200
                    and response.json().get("url") == binding["endpoint"]
                ):
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.25)
    raise RuntimeError("provider readiness timed out")


def main(
    extra_setup: Callable[[CloudBundle, Path, Path], list[tuple[str, list[str]]]] | None = None,
    api_factory: str = "orca.bootstrap:create_application",
    app_dir: str | None = None,
) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    bundle = CloudBundle.model_validate(strict_json(read_bundle()))
    state = Path(os.environ.get("ORCA_PERSISTENT_DIR", "/var/data/orca"))
    if not state.is_absolute():
        raise SystemExit("persistent state directory must be absolute")
    api_env, commands = materialize(bundle, state, os.environ.get("ORCA_FRONTEND_ORIGIN", ""))
    if extra_setup is not None:
        commands.extend(extra_setup(bundle, state, api_env))
    settings = Settings(_env_file=api_env)  # type: ignore[call-arg]
    if args.check_only:
        print(
            json.dumps(
                {
                    "status": "CONFIGURATION_VALID",
                    "services": [name for name, _ in commands],
                    "credentials_printed": False,
                }
            )
        )
        return
    restore_sources(settings, state / "sources")
    if not (state / "initialized").exists():
        # Fresh disk cannot safely replay an old host's ambiguous sends without its journals.
        # The deployment runbook drains/stops local signing workers before this cutover.
        if not settings.database_url:
            raise ValueError("runtime database required")
        db = create_runtime_engine(settings.database_url)
        try:
            with db.connect() as connection:
                pending: int = connection.execute(
                    text("SELECT count(*) FROM iam.network_pending_operations()")
                ).scalar_one()
            if pending:
                raise RuntimeError(
                    "Drain existing coordinator jobs before first cloud startup; old-host send journals have not been imported"
                )
        finally:
            db.dispose()
        private_file(state / "initialized", "cloud runtime initialized\n")
    port = int(os.environ.get("PORT", "10000"))
    commands.append(
        (
            "api",
            [
                sys.executable,
                "-m",
                "uvicorn",
                api_factory,
                "--factory",
                "--env-file",
                str(api_env),
                "--host",
                "0.0.0.0",
                "--port",
                str(port),
            ],
        )
    )
    if app_dir:
        commands[-1][1].extend(["--app-dir", app_dir])
    processes = []
    stopping = False

    def stop(signum: int, frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        commands.sort(key=lambda item: "orca.agent_provider" not in item[1])
        for name, command in commands:
            process = subprocess.Popen(command, env=child_environment())
            processes.append((name, process))
            if "orca.agent_provider" in command:
                await_provider(command, process)
        while not stopping:
            failed = [name for name, process in processes if process.poll() is not None]
            if failed:
                raise RuntimeError("supervised service stopped: " + ",".join(failed))
            time.sleep(1)
    finally:
        for _, process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for _, process in processes:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(
            "Cloud startup failed ("
            + type(error).__name__
            + "); inspect configuration and persistent storage without logging secrets"
        ) from None
