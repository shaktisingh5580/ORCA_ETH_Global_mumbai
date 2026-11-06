"""Purpose: enforce the research-only route ceiling and preserve historical source evidence.
Behavior: reject excluded/deferred runtime routes and verify the 220-to-165 scope derivation.
Refs: research-only guide sections 3, 8.4, 10.1 and ADR-R01; Phase: scope migration.
"""

import hashlib
import json
import secrets
from pathlib import Path

from pydantic import SecretStr

from orca.api.app import create_app
from orca.config import Settings

ROOT = Path(__file__).resolve().parents[3]
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
EXCLUDED_FRAGMENTS = (
    "/marine",
    "/alerts",
    "/conversations",
    "/voice",
    "/localization",
    "/training-jobs",
    "/model-registry",
    "/model-evaluations",
)


def load(path: str):
    content = (ROOT / path).read_text(encoding="utf-8")
    return json.loads(content[content.index("{") :])


def operation_keys(document):
    return {
        (method.upper(), path)
        for path, methods in document["paths"].items()
        for method in methods
        if method in HTTP_METHODS
    }


def test_scope_01_excluded_routes_absent_from_selected_and_runtime():
    selected = load("contracts/designed/orca-research-selected.json")
    runtime = create_app(
        Settings(monitoring_token=SecretStr(secrets.token_urlsafe(48)), environment="test")
    ).openapi()
    for document in (selected, runtime):
        assert not any(
            fragment in path for path in document["paths"] for fragment in EXCLUDED_FRAGMENTS
        )


def test_scope_manifest_exactly_reconciles_historical_inventory():
    manifest = load("contracts/designed/research-route-dispositions.json")
    historical = load("contracts/designed/orca-proposal.json")
    selected = load("contracts/designed/orca-research-selected.json")
    assert manifest["counts"] == {
        "CORE": 100,
        "DEFER": 21,
        "EXCLUDE": 34,
        "FED": 47,
        "OPS": 18,
    }
    assert len(manifest["operations"]) == 220
    assert len(operation_keys(historical)) == 220
    assert len(operation_keys(selected)) == 165
    assert operation_keys(selected) == {
        (entry["method"], entry["path"])
        for entry in manifest["operations"]
        if entry["disposition"] in {"CORE", "FED", "OPS"}
    }


def test_scope_05_federation_not_exposed_before_live_gates():
    manifest = load("contracts/designed/research-route-dispositions.json")
    federation = {
        (entry["method"], entry["path"])
        for entry in manifest["operations"]
        if entry["disposition"] == "FED"
    }
    runtime = create_app(
        Settings(monitoring_token=SecretStr(secrets.token_urlsafe(48)), environment="test")
    ).openapi()
    assert operation_keys(runtime).isdisjoint(federation)


def test_scope_09_local_sources_match_immutable_snapshot():
    snapshot = load("docs/review/research-only-source-snapshot.json")
    revisions = load("docs/review/A01-source-reconciliation.json")
    assert revisions["decision"] == "A01-ai-prototype"
    revised = {item["path"]: item for item in revisions["files"]}
    assert snapshot["decision"] == "ADR-R01"
    assert any("Research_Only_Scope" in item["path"] for item in snapshot["files"])
    for item in snapshot["files"]:
        path = ROOT / item["path"]
        if path.exists():
            expected = revised.get(item["path"], item)
            if expected is not item:
                assert expected["previous_sha256"] == item["sha256"]
            assert len(path.read_bytes()) == expected["bytes"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected["sha256"]


def test_research_release_profiles_replace_expanded_product_names():
    profiles = load("docs/releases/release_profiles.yaml")
    assert profiles["selected_target"] == "research-federated"
    assert [entry["name"] for entry in profiles["profiles"]] == [
        "research-foundation",
        "research-engine",
        "research-core",
        "research-federated",
    ]
    assert profiles["legacy_aliases"] == {"core-C": "research-core"}
