"""Purpose: validate frontend scenarios independently and enforce release separation.
Behavior: check JSON Schema and actual HTTP responses; inspect wheel and sdist contents.
Refs: main plan P01.3; API section 11.2; Phase: P01.
"""

import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from check_import_boundaries import violations
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from mock_server import FIXTURES, TYPES, create_mock_app

BACKEND = Path(__file__).resolve().parents[2]


@pytest.mark.allow_hosts(["127.0.0.1", "::1"])
def test_fixture_success_empty_partial_error_validate_contract():
    fixture = json.loads(FIXTURES.read_text())
    assert "synthetic" in fixture["origin"] or "test fixture" in fixture["origin"]
    assert set(fixture["scenarios"]) == {"success", "empty", "partial", "error"}
    schema = json.loads((BACKEND.parent / "contracts/schemas/common.json").read_text())
    with TestClient(create_mock_app()) as client:
        for name, scenario in fixture["scenarios"].items():
            model = TYPES[scenario["schema"]]
            title = model.model_json_schema()["title"]
            definition = next(k for k, v in schema["$defs"].items() if v["title"] == title)
            independent = dict(schema, **{"$ref": f"#/$defs/{definition}"})
            validator = Draft202012Validator(independent, format_checker=FormatChecker())
            validator.validate(scenario["body"])
            assert not validator.is_valid(dict(scenario["body"], unexpected=True))
            response = client.get(f"/scenarios/{name}")
            assert response.status_code == scenario["status"]
            assert response.json() == scenario["body"]
            assert response.headers["x-orca-test-only"] == "synthetic-fixture"
            model.model_validate(response.json())
        assert client.get("/scenarios/absent").status_code == 404
    assert fixture["scenarios"]["empty"]["body"]["items"] == []
    assert fixture["scenarios"]["partial"]["body"]["status"] == "insufficient_evidence"


def test_mock_harness_not_in_release_build():
    wheel = BACKEND / "dist/orca_backend-0.1.0-py3-none-any.whl"
    sdist = BACKEND / "dist/orca_backend-0.1.0.tar.gz"
    assert wheel.is_file() and sdist.is_file(), "Run uv build before tests (the gate does this)"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "orca/api/app.py" in names
        for name in names:
            assert not {"tests", "fixtures", "scripts", ".env"}.intersection(Path(name).parts)
            assert b"Synthetic frontend project" not in archive.read(name)
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
        assert any(name.endswith("src/orca/api/app.py") for name in names)
        assert not any(
            {"tests", "fixtures", ".env"}.intersection(Path(name).parts) for name in names
        )


def test_mock_runtime_imports_forbidden(tmp_path):
    assert violations(BACKEND / "src") == []
    path = tmp_path / "bootstrap.py"
    for source in (
        "from tests.support import mock_server",
        "import fixtures.api",
        "import importlib\nimportlib.import_module('tests.support.mock_server')",
        "__import__(module_name)",
        "flag = 'USE_DUMMY_DATA'",
    ):
        path.write_text(source)
        assert violations(tmp_path), source
    path.unlink()
    (tmp_path / "domain").mkdir()
    (tmp_path / "domain/model.py").write_text("import fastapi")
    assert violations(tmp_path)
