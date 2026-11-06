"""Purpose: initialize separate local service/wallet identities and register approved bindings.
Behavior: write secrets only to ignored environment files; never overwrite an existing identity.
Refs: H04-agent-coordination.json; user authorized prototype setup; Phase: H04.
"""

import argparse
import json
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotenv import dotenv_values, set_key
from sqlalchemy import text

from orca.api.routers.projects import resolve_tenant
from orca.application.network import encode
from orca.config import Settings
from orca.contracts.network import AgentBinding, Capability
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.runtime import (
    ROOT,
    ArkivGateway,
    NetworkConfiguration,
    load_runtime,
)
from orca.infrastructure.federation.signing import b64, strict_json


def initialize(env_file: Path) -> None:
    folder = ROOT / ".cache/network"
    folder.mkdir(parents=True, exist_ok=True)
    config_file = folder / "operator-bindings.json"
    if config_file.exists():
        print("Existing operator bindings preserved; use --register or --fund explicitly")
        return
    if not (ROOT / ".env.arkiv").exists():
        raise ValueError("existing dedicated coordinator wallet environment is required")
    gateway = ArkivGateway(ROOT / ".env.arkiv", folder / "coordinator", "")
    owner = gateway.call({"command": "wallet"}, write=True)["owner"]
    identities = []
    service_secrets = []
    for role, capability, port in (
        ("coordinator", "sst_reproduction", 8000),
        ("ocean", "sst_reproduction", 8101),
        ("reviewer", "sst_review", 8102),
    ):
        service_key = Ed25519PrivateKey.generate()
        private = b64(
            service_key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        )
        public = b64(
            service_key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )
        wallet_file = ROOT / (".env.arkiv" if role == "coordinator" else f".env.arkiv-{role}")
        if role != "coordinator":
            if wallet_file.exists():
                raise ValueError(
                    "wallet file exists without complete bindings; reconcile initialization before retrying"
                )
            wallet_owner = gateway.call(
                {"command": "initialize_wallet", "env_file": str(wallet_file)}
            )["owner"]
        else:
            wallet_owner = owner
        binding = AgentBinding(
            id=uuid4(),
            binding_version=uuid4(),
            name=f"ORCA team {role} agent",
            capability=cast(Capability, capability),
            endpoint=f"http://127.0.0.1:{port}/a2a",
            public_key=public,
            key_id=f"{role}-service-v1",
            wallet=wallet_owner,
        )
        identities.append(binding)
        service_secrets.append((role, private, wallet_file))
    configuration = NetworkConfiguration(coordinator=identities[0], agents=tuple(identities[1:]))
    configuration.validate_endpoints()
    # Save identities before enabling any server or registering them in the database.
    config_file.write_text(configuration.model_dump_json(indent=2), encoding="utf-8")
    for binding, (role, private, wallet_file) in zip(identities, service_secrets, strict=True):
        if role == "coordinator":
            for name, value in {
                "ORCA_NETWORK_CONFIG_FILE": str(config_file),
                "ORCA_NETWORK_PRIVATE_KEY": private,
                "ORCA_NETWORK_WALLET_ENV_FILE": str(wallet_file),
                "ORCA_NETWORK_STATE_DIR": str(folder / role),
            }.items():
                set_key(str(env_file), name, value)
        else:
            binding_file = folder / f"{role}-binding.json"
            binding_file.write_text(binding.model_dump_json(indent=2), encoding="utf-8")
            destination = ROOT / f".env.provider-{role}"
            values = {
                "ORCA_PROVIDER_BINDING_FILE": str(binding_file),
                "ORCA_PROVIDER_COORDINATOR_FILE": str(config_file),
                "ORCA_PROVIDER_PRIVATE_KEY": private,
                "ORCA_PROVIDER_WALLET_ENV_FILE": str(wallet_file),
                "ORCA_PROVIDER_CAPTURE_FOLDER": str(
                    ROOT / ".cache/science-inputs/mur-native-batch"
                ),
                "ORCA_PROVIDER_STATE_DIR": str(folder / role),
            }
            with destination.open("x", encoding="utf-8") as output:
                output.write("# Private separately keyed provider; no Supabase credentials.\n")
                for name, value in values.items():
                    output.write(name + "=" + json.dumps(value) + "\n")
    print(
        json.dumps(
            {
                "profile": "team-controlled",
                "bindings_file": str(config_file),
                "coordinator": identities[0].wallet,
                "providers": [
                    {
                        "id": str(binding.id),
                        "capability": binding.capability,
                        "wallet": binding.wallet,
                    }
                    for binding in identities[1:]
                ],
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.demo")
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--register", action="store_true")
    parser.add_argument(
        "--operator-provision",
        action="store_true",
        help="Local infrastructure operator may provision team agents only in the configured prototype tenant; no HTTP permission or user role changes",
    )
    parser.add_argument("--fund", action="store_true")
    parser.add_argument("--organization-id", type=UUID)
    parser.add_argument("--subject")
    parser.add_argument("--revoke-agent", type=UUID)
    args = parser.parse_args()
    if args.initialize:
        initialize(args.env_file)
    values = dotenv_values(args.env_file)
    if args.fund:
        configuration = NetworkConfiguration.model_validate(
            strict_json(Path(str(values["ORCA_NETWORK_CONFIG_FILE"])).read_bytes())
        )
        gateway = ArkivGateway(
            ROOT / ".env.arkiv",
            ROOT / ".cache/network/coordinator",
            configuration.coordinator.wallet,
        )
        for binding in configuration.agents:
            result = gateway.call(
                {
                    "command": "fund",
                    "recipient": binding.wallet,
                    "amount": "0.1",
                    "receipt_file": str(ROOT / ".cache/network" / f"fund-{binding.id}.json"),
                },
                write=True,
            )
            print(json.dumps({"provider_id": str(binding.id), **result}))
    if args.register or args.revoke_agent:
        if not args.organization_id or not args.subject:
            raise ValueError("registration requires an authorized organization and subject")
        settings = Settings(_env_file=args.env_file, network_enabled=True)  # type: ignore[call-arg]
        runtime = load_runtime(settings)
        if not settings.database_url:
            raise ValueError("runtime database required")
        db = create_runtime_engine(settings.database_url)
        principal = Principal(args.subject, "operator", frozenset().union(*ROLE_SCOPES.values()))
        try:
            tenant, roles = resolve_tenant(db, principal, args.organization_id)
            if not roles.intersection({"org_owner", "org_admin"}) and not (
                args.operator_provision and tenant == settings.prototype_source_tenant and roles
            ):
                raise PermissionError(
                    "organization owner/admin approval required for provider onboarding"
                )
            with TenantUnitOfWork(db, tenant) as session:
                if args.revoke_agent:
                    session.execute(
                        text(
                            "UPDATE federation.agents SET revoked=true WHERE tenant_id=:t AND id=:id"
                        ),
                        {"t": tenant, "id": args.revoke_agent},
                    )
                else:
                    for binding in runtime.configuration.agents:
                        existing = (
                            session.execute(
                                text(
                                    "SELECT binding,revoked FROM federation.agents WHERE tenant_id=:t AND id=:id"
                                ),
                                {"t": tenant, "id": binding.id},
                            )
                            .mappings()
                            .one_or_none()
                        )
                        if existing is not None and (
                            existing["binding"] != binding.model_dump(mode="json")
                            or existing["revoked"]
                        ):
                            raise ValueError(
                                "existing binding differs or is revoked; explicit new identity required"
                            )
                        session.execute(
                            text(
                                "INSERT INTO federation.agents(id,tenant_id,binding_version,binding) VALUES(:id,:t,:version,CAST(:binding AS jsonb)) ON CONFLICT(tenant_id,id) DO NOTHING"
                            ),
                            {
                                "id": binding.id,
                                "t": tenant,
                                "version": binding.binding_version,
                                "binding": encode(binding.model_dump(mode="json")),
                            },
                        )
            if args.register:
                set_key(str(args.env_file), "ORCA_NETWORK_ENABLED", "true")
            print(
                json.dumps(
                    {
                        "registered": args.register,
                        "revoked": bool(args.revoke_agent),
                        "tenant_id": str(tenant),
                    }
                )
            )
        finally:
            db.dispose()


if __name__ == "__main__":
    main()
