"""Purpose: verify the reproducible local release matrix, contracts and package contents.
Behavior: compile the runtime SDK, reject incomplete evidence, and install a newly built wheel.
Refs: main_implementation plan.md P14.3; api design.md §19; Phase: P14.
"""

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from export_openapi import artifacts
from openapi_spec_validator import validate
from release_matrix import (
    ATTESTATION_DOMAIN,
    REQUIRED_LOCAL_CASES,
    load_json,
    release_inventory,
    verify_required_junit,
    verify_signed_evidence,
)

from orca.infrastructure.federation.signing import b64, canonical

ROOT = Path(__file__).resolve().parents[3]
RECONCILIATION_REQUIRED = [
    "GET /v1/research/benchmarks",
    "GET /v1/research/benchmarks/{benchmark_id}",
    "POST /v1/research/reports/{report_id}/publish",
]


def run(*command, cwd=ROOT, env=None):
    output = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180
    )
    assert output.returncode == 0, f"{command[0]} failed:\n{output.stdout}\n{output.stderr}"
    return output.stdout


def test_all_enabled_220_op_inventory_checked():
    inventory = release_inventory()
    assert inventory["historical_operations"] == 220
    assert inventory["selected_design_operations"] == 165
    assert inventory["historical_outside_selection_registered"] == RECONCILIATION_REQUIRED
    rows = inventory["runtime_operations"]
    assert len({row["operation_id"] for row in rows}) == len(rows)
    assert all(row["operation_id"] and row["design_disposition"] != "EXCLUDE" for row in rows)
    assert {(row["method"], row["path"]) for row in rows if not row["security"]} == {
        ("GET", "/livez"),
        ("GET", "/v1/meta"),
    }
    assert not any(row["path"].startswith(("/v1/demo", "/v1/alerts")) for row in rows)


def test_openapi_client_regenerated():
    runtime = load_json("contracts/openapi/orca-v1.json")
    assert json.loads(artifacts()["openapi/orca-v1.json"]) == runtime
    validate(runtime)
    npm = "npm.cmd" if os.name == "nt" else "npm"
    run(npm, "run", "check", cwd=ROOT / "contracts/typescript")


def test_no_required_skips(tmp_path):
    path = tmp_path / "release.xml"
    suite = ET.Element("testsuite")
    for name in REQUIRED_LOCAL_CASES:
        ET.SubElement(suite, "testcase", name=name)
    ET.ElementTree(suite).write(path)
    assert verify_required_junit(path) == len(REQUIRED_LOCAL_CASES)

    case = suite[0]
    for tag in ("skipped", "failure", "error"):
        marker = ET.SubElement(case, tag)
        ET.ElementTree(suite).write(path)
        with pytest.raises(ValueError, match="failed, errored or skipped"):
            verify_required_junit(path)
        case.remove(marker)
    suite.append(ET.Element("testcase", name=case.attrib["name"]))
    ET.ElementTree(suite).write(path)
    with pytest.raises(ValueError, match="Expected exactly one"):
        verify_required_junit(path)
    suite.remove(suite[-1])
    suite.remove(case)
    ET.ElementTree(suite).write(path)
    with pytest.raises(ValueError, match="Expected exactly one"):
        verify_required_junit(path)
    ET.ElementTree(ET.Element("testsuite")).write(path)
    with pytest.raises(ValueError, match="No tests collected"):
        verify_required_junit(path)


def test_local_signature_and_federation_scope():
    key = Ed25519PrivateKey.generate()
    public = b64(key.public_key().public_bytes_raw())

    def signed(profile, gate):
        evidence = {
            "profile": profile,
            "gate_status": gate,
            "federation_prerequisites": {"G09": "BLOCKED", "G10": "BLOCKED", "G11": "BLOCKED"},
            "cases": 5,
        }
        return {
            **evidence,
            "signature": {
                "algorithm": "Ed25519",
                "public_key": public,
                "value": b64(key.sign(ATTESTATION_DOMAIN + canonical(evidence))),
            },
        }

    blocked = signed("research-federated", "BLOCKED")
    verify_signed_evidence(blocked, public)
    with pytest.raises(ValueError, match="signature is invalid"):
        verify_signed_evidence(blocked | {"cases": 6}, public)
    with pytest.raises(ValueError, match="retai[n].*federation"):
        verify_signed_evidence(signed("research-core", "PASS"), public)
    with pytest.raises(ValueError, match="prerequisites have not passed"):
        verify_signed_evidence(signed("research-federated", "PASS"), public)


@pytest.fixture(scope="module")
def release_packages(tmp_path_factory):
    target = tmp_path_factory.mktemp("release-dist")
    network = [] if os.environ.get("CI") == "true" else ["--offline"]
    env = dict(os.environ, UV_CACHE_DIR=str(ROOT / ".cache/uv"))
    run(
        "uv",
        "build",
        "--project",
        "backend",
        "--out-dir",
        str(target),
        "--python",
        sys.executable,
        *network,
        env=env,
    )
    (wheel,) = target.glob("orca_backend-*.whl")
    (source,) = target.glob("orca_backend-*.tar.gz")
    return wheel, source


def test_mock_artifacts_excluded(release_packages):
    wheel, source = release_packages
    license_text = (ROOT / "LICENSE").read_bytes()
    with ZipFile(wheel) as archive:
        wheel_paths = archive.namelist()
        (wheel_license,) = [
            name for name in wheel_paths if name.endswith(".dist-info/licenses/LICENSE")
        ]
        assert archive.read(wheel_license) == license_text
    with tarfile.open(source, "r:gz") as archive:
        source_paths = archive.getnames()
        (source_license,) = [name for name in source_paths if name.endswith("/LICENSE")]
        file = archive.extractfile(source_license)
        assert file is not None
        assert file.read() == license_text
    for paths in (wheel_paths, source_paths):
        assert not any(
            set(Path(name).parts) & {"fixtures", "tests", "demo_tooling", ".env", ".venv"}
            or Path(name).stem in {"mock_server", "disagreement_demo", "disagreement_data"}
            for name in paths
        )
        assert any(name.endswith("orca/api/dashboard/index.html") for name in paths)


def test_repro_clean_install(release_packages, tmp_path, record_property):
    wheel, source = release_packages
    clean = tmp_path / "release-runtime"
    offline = [] if os.environ.get("CI") == "true" else ["--offline"]
    env = dict(os.environ, UV_CACHE_DIR=str(ROOT / ".cache/uv"), UV_PROJECT_ENVIRONMENT=str(clean))
    run(
        "uv",
        "sync",
        "--project",
        "backend",
        "--locked",
        "--no-dev",
        "--no-install-project",
        "--python",
        sys.executable,
        *offline,
        env=env,
    )
    executable = clean / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run(
        "uv",
        "pip",
        "install",
        "--python",
        str(executable),
        "--offline",
        "--no-deps",
        str(wheel),
        env=env,
    )
    code = """
import importlib
import importlib.metadata
import json
import platform
import sys

for name in ("orca.api.app", "orca.cloud_runtime", "orca.institution_provider", "orca.workers.runner"):
    importlib.import_module(name)
assert not any(name.split(".")[0] in {"pytest", "tests", "fixtures", "mock_server"} for name in sys.modules)
inventory = []
for distribution in importlib.metadata.distributions():
    metadata = distribution.metadata
    license_fields = [metadata.get("License-Expression"), metadata.get("License")]
    expression = next((value.strip() for value in license_fields
                       if value and 0 < len(value.strip()) < 160 and "\\n" not in value.strip()), None)
    inventory.append({
        "package": metadata["Name"].lower().replace("_", "-"),
        "version": distribution.version,
        "license": expression,
        "license_classifiers": [item for item in metadata.get_all("Classifier", [])
                                if item.startswith("License ::")],
        "license_files": [item for item in metadata.get_all("License-File", [])],
    })
print(json.dumps({
    "python": platform.python_version(),
    "dependencies": sorted(inventory, key=lambda item: item["package"]),
}))
"""
    result = json.loads(run(str(executable), "-I", "-c", code, cwd=tmp_path))
    installed = {row["package"]: row["version"] for row in result["dependencies"]}
    assert "orca-backend" in installed and "pytest" not in installed
    assert (
        next(row for row in result["dependencies"] if row["package"] == "orca-backend")["license"]
        == "MIT"
    )
    lock = tomllib.loads((ROOT / "backend/uv.lock").read_text(encoding="utf-8"))
    assert set(installed.items()) <= {(item["name"], item["version"]) for item in lock["package"]}
    record_property("python_version", result["python"])
    record_property("wheel_sha256", hashlib.sha256(wheel.read_bytes()).hexdigest())
    record_property("sdist_sha256", hashlib.sha256(source.read_bytes()).hexdigest())
    record_property(
        "installed_license_inventory", json.dumps(result["dependencies"], sort_keys=True)
    )
