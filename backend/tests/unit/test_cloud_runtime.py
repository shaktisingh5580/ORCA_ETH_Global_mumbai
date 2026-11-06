"""Purpose: verify private cloud materialization and source restoration isolation.
Behavior: check scoped provider secrets, strict origins and exclusion of the whole bundle from children.
Refs: H05 Render deployment gate; Phase: H05; synthetic credentials only.
"""

import json

import pytest
from dotenv import dotenv_values
from network_setup import configuration

from orca.cloud_runtime import CloudBundle, child_environment, env_text, materialize, read_bundle


@pytest.mark.enable_socket
def test_cloud_bundle_preserves_signers_but_does_not_distribute_database_credentials(
    tmp_path, monkeypatch
):
    runtime, _, secrets = configuration(tmp_path)
    values = {
        "monitoring_token": "0123456789abcdefghijklmnopqrstuvwxyz",
        "database_url": "postgresql+psycopg://runtime:test-only@127.0.0.1:15432/orca_test",
        "object_store_endpoint": "http://127.0.0.1:59000",
        "object_store_access_key": "test-only",
        "object_store_secret_key": "test-only",
        "oidc_issuer": "https://auth.example.test",
        "oidc_audience": "test",
        "oidc_jwks_url": "https://auth.example.test/keys",
        "prototype_enabled": True,
        "prototype_bundle_digest": "sha256:" + "a" * 64,
        "prototype_source_tenant": "00000000-0000-0000-0000-000000000001",
        "network_enabled": True,
        "network_private_key": runtime.private_key,
    }
    bundle = CloudBundle(
        schema_version="orca.cloud-runtime-bundle/1.0",
        settings=values,
        bindings=runtime.configuration,
        coordinator_wallet={"ARKIV_PRIVATE_KEY": "test-only-coordinator"},
        providers={
            role: {"service_private_key": private, "wallet_private_key": "test-only-wallet"}
            for role, (_, private) in zip(("ocean", "reviewer"), secrets, strict=True)
        },
    )
    api, commands = materialize(bundle, tmp_path / "state", "https://frontend.example.test")
    assert dotenv_values(api)["ORCA_DATABASE_URL"] == values["database_url"]
    producer = dotenv_values(tmp_path / "state/ocean.env")
    assert "ORCA_DATABASE_URL" not in producer and "ORCA_OBJECT_STORE_SECRET_KEY" not in producer
    assert len(commands) == 4
    monkeypatch.setenv("ORCA_RUNTIME_BUNDLE", bundle.model_dump_json())
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-only")
    assert (
        "ORCA_RUNTIME_BUNDLE" not in child_environment()
        and "SUPABASE_SERVICE_ROLE_KEY" not in child_environment()
    )
    with pytest.raises(ValueError):
        materialize(bundle, tmp_path / "bad", "https://frontend.example.test/path")
    with pytest.raises(ValueError):
        materialize(bundle, tmp_path / "bad", "http://frontend.example.test")
    file = tmp_path / "quoted.env"
    file.write_text(env_text({"VALUE": json.dumps({"text": "researcher's input"})}))
    assert json.loads(dotenv_values(file)["VALUE"])["text"] == "researcher's input"


def test_file_mounted_bundle_is_literal_and_never_ambiguous(tmp_path, monkeypatch):
    value = b'{"test_secret":"literal $VALUE and quotes"}'
    filename = tmp_path / "private-bundle.json"
    filename.write_bytes(value)
    monkeypatch.delenv("ORCA_RUNTIME_BUNDLE", raising=False)
    monkeypatch.setenv("ORCA_RUNTIME_BUNDLE_FILE", str(filename))
    assert read_bundle() == value
    monkeypatch.setenv("ORCA_RUNTIME_BUNDLE", "another-source")
    with pytest.raises(ValueError, match="only one"):
        read_bundle()
    monkeypatch.delenv("ORCA_RUNTIME_BUNDLE")
    filename.write_bytes(b"x" * 1_048_577)
    with pytest.raises(ValueError, match="size"):
        read_bundle()
