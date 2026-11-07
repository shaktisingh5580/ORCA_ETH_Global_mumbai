"""Purpose: validate bounded ENS onboarding observations without network or secrets.
Behavior: reject invalid names and failed verifiers; never promote observation to authority.
Refs: institutional plan F03.1; Phase: ENS observation increment.
"""

import json
from types import SimpleNamespace

import pytest

from orca.infrastructure.federation import ens_observation as ens


def test_invalid_name_never_starts_verifier(monkeypatch):
    monkeypatch.setattr(ens.shutil, "which", lambda _: pytest.fail("Unexpected process"))
    with pytest.raises(ValueError):
        ens.inspect_name("https://malicious.example")


def test_unavailable_verifier_never_grants_authority(monkeypatch):
    monkeypatch.setattr(ens.shutil, "which", lambda _: "node")
    monkeypatch.setattr(
        ens.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="")
    )
    with pytest.raises(RuntimeError, match="UNAVAILABLE"):
        ens.inspect_name("analyst.orca-research-lab.eth")


def test_observation_is_read_only_and_process_has_no_keys(monkeypatch):
    name = "analyst.orca-research-lab.eth"
    monkeypatch.setattr(ens.shutil, "which", lambda _: "node")
    monkeypatch.setenv("ARKIV_PRIVATE_KEY", "test-secret-must-not-reach-reader")

    def run(args, **kwargs):
        assert args[-2:] == ["inspect", name]
        assert "ARKIV_PRIVATE_KEY" not in kwargs["env"]
        assert kwargs["timeout"] == 35
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"name": name, "authority_verified": False})
        )

    monkeypatch.setattr(ens.subprocess, "run", run)
    assert ens.inspect_name(name)["authority_verified"] is False
