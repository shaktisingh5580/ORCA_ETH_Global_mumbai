"""Purpose: load explicit operator-pinned agent endpoints and invoke the isolated Arkiv bridge.
Behavior: restrict this prototype to literal loopback providers and keep wallet secrets out of HTTP.
Refs: H04 decision; docs/02_agentic_architecture.md egress/binding rules; Phase: H04.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict

from orca.config import Settings
from orca.contracts.network import AgentBinding, SignedEnvelope
from orca.infrastructure.federation.signing import b64, strict_json, unb64, verify

ROOT = Path(__file__).resolve().parents[5]


class NetworkConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coordinator: AgentBinding
    agents: tuple[AgentBinding, ...]

    def validate_endpoints(self) -> None:
        identities = [self.coordinator, *self.agents]
        if len({item.id for item in identities}) != len(identities):
            raise ValueError("distinct agent IDs required")
        if len({item.wallet.lower() for item in identities}) != len(identities):
            raise ValueError("distinct agent wallets required")
        if len({item.public_key for item in identities}) != len(identities):
            raise ValueError("distinct service signing keys required")
        for agent in self.agents:
            parsed = urlsplit(agent.endpoint)
            if (
                parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1"
                or parsed.port is None
                or not 1024 <= parsed.port <= 65535
                or parsed.path != "/a2a"
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError(
                    "hackathon providers must use explicitly pinned literal loopback /a2a endpoints"
                )


class ChainUnavailable(RuntimeError):
    def __init__(self, reconciliation: bool = False):
        self.reconciliation = reconciliation
        super().__init__("ARKIV_RECONCILIATION_REQUIRED" if reconciliation else "ARKIV_UNAVAILABLE")


@dataclass(frozen=True)
class ArkivGateway:
    wallet_env_file: Path
    state_dir: Path
    owner: str

    def call(self, data: dict[str, Any], *, write: bool = False) -> dict[str, Any]:
        def uncertain() -> bool:
            return bool(write and data.get("receipt_file") and Path(data["receipt_file"]).exists())

        configured = dotenv_values(self.wallet_env_file)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        }
        environment["ARKIV_ACCESS_KEY"] = configured.get("ARKIV_ACCESS_KEY") or ""
        if write:
            environment["ARKIV_PRIVATE_KEY"] = configured.get("ARKIV_PRIVATE_KEY") or ""
        node = shutil.which("node")
        if node is None:
            raise ChainUnavailable()
        try:
            completed = subprocess.run(
                [node, str(ROOT / "arkiv/src/coordination.mjs")],
                input=json.dumps(data, allow_nan=False),
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=150,
                env=environment,
                cwd=ROOT,
            )
            if completed.returncode or len(completed.stdout) > 2_000_000:
                raise ChainUnavailable(uncertain())
            value = strict_json(completed.stdout)
            if not isinstance(value, dict):
                raise ChainUnavailable()
            return value
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            raise ChainUnavailable(uncertain()) from error

    def publish(self, envelope: SignedEnvelope, binding: AgentBinding) -> dict[str, Any]:
        if binding.wallet.lower() != self.owner.lower():
            raise ValueError("pinned writer mismatch")
        filename = self.state_dir / "receipts" / f"{envelope.payload['event_id']}.json"
        result = self.call(
            {
                "command": "publish",
                "envelope": envelope.model_dump(),
                "public_key": binding.public_key,
                "owner": binding.wallet,
                "receipt_file": str(filename),
                "lock_file": str(self.state_dir / "writer.lock"),
            },
            write=True,
        )
        verify_chain_record(result["read_back"], binding, envelope)
        return result

    def read(self, key: str, binding: AgentBinding, envelope: SignedEnvelope) -> dict[str, Any]:
        result = self.call(
            {
                "command": "read",
                "entity_key": key,
                "owner": binding.wallet,
                "content_digest": envelope.content_digest,
            }
        )
        verify_chain_record(result, binding, envelope)
        return result


def verify_chain_record(
    record: dict[str, Any], binding: AgentBinding, expected: SignedEnvelope
) -> None:
    actual = SignedEnvelope.model_validate(record["envelope"])
    verify(actual, binding.public_key, binding.key_id, "public")
    if (
        actual != expected
        or record["owner"].lower() != binding.wallet.lower()
        or record["public_key"] != binding.public_key
        or actual.payload.get("actor_id") != str(binding.id)
        or actual.payload.get("binding_version") != str(binding.binding_version)
    ):
        raise ValueError("chain record differs from the pinned actor or expected statement")


@dataclass(frozen=True)
class NetworkRuntime:
    configuration: NetworkConfiguration
    private_key: str
    gateway: ArkivGateway


def load_runtime(settings: Settings) -> NetworkRuntime:
    if (
        not settings.network_enabled
        or not settings.network_config_file
        or not settings.network_private_key
    ):
        raise ValueError("agent coordination is not configured")
    configuration = NetworkConfiguration.model_validate(
        strict_json(Path(settings.network_config_file).read_bytes())
    )
    configuration.validate_endpoints()
    private_key = settings.network_private_key.get_secret_value()
    public_key = b64(
        Ed25519PrivateKey.from_private_bytes(unb64(private_key))
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    if public_key != configuration.coordinator.public_key:
        raise ValueError("coordinator private key differs from pinned public binding")
    return NetworkRuntime(
        configuration,
        private_key,
        ArkivGateway(
            Path(settings.network_wallet_env_file),
            Path(settings.network_state_dir),
            configuration.coordinator.wallet,
        ),
    )
