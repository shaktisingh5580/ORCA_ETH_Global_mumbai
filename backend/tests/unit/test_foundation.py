"""Purpose: prove locked installation, fail-fast configuration and production boundaries.
Behavior: install into a fresh environment, import every runtime module, run static tools.
Refs: main plan P01.1; API section 2; Phase: P01.
"""

import ast
import importlib
import json
import os
import re
import secrets
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from check_file_headers import violations as header_errors
from check_import_boundaries import violations as import_errors
from pydantic import SecretStr, ValidationError

from orca.config import Settings

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent


def test_clean_install_from_lock(tmp_path):
    environment = tmp_path / "clean-runtime"
    env = dict(
        os.environ,
        UV_PROJECT_ENVIRONMENT=str(environment),
        UV_CACHE_DIR=str(ROOT / ".cache/uv"),
        UV_PYTHON_INSTALL_DIR=str(ROOT / ".cache/python"),
    )
    command = [
        "uv",
        "sync",
        "--locked",
        "--no-dev",
        "--no-install-project",
    ]
    if os.environ.get("CI") != "true":
        command.append("--offline")
    result = subprocess.run(
        command, cwd=BACKEND, env=env, capture_output=True, text=True, timeout=180
    )
    log = "Command: " + " ".join(command) + "\n" + result.stdout + result.stderr
    evidence = ROOT / "docs/gates/P01/clean-install.txt"
    evidence.write_text(log, encoding="utf-8")
    assert result.returncode == 0, log
    executable = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    wheel = next((BACKEND / "dist").glob("orca_backend-*.whl"))
    install_command = [
        "uv",
        "pip",
        "install",
        "--python",
        str(executable),
        "--offline",
        "--no-deps",
        str(wheel),
    ]
    installed_project = subprocess.run(
        install_command, cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60
    )
    log += (
        "\nCommand: "
        + " ".join(install_command)
        + "\n"
        + installed_project.stdout
        + installed_project.stderr
    )
    assert installed_project.returncode == 0, log
    modules = [
        ".".join(p.relative_to(BACKEND / "src").with_suffix("").parts).removesuffix(".__init__")
        for p in sorted((BACKEND / "src").rglob("*.py"))
    ]
    code = (
        "import importlib,json,sys; from importlib.metadata import distributions; "
        + f"[importlib.import_module(n) for n in {modules!r}]; "
        + "assert not any(n.split('.')[0] in {'tests','pytest','fixtures','mock_server'} for n in sys.modules); print(json.dumps({d.metadata['Name'].lower():d.version for d in distributions()},sort_keys=True))"
    )
    loaded = subprocess.run(
        [str(executable), "-I", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    evidence.write_text(
        log
        + "\nInstalled runtime modules import in isolated mode:\n"
        + loaded.stdout
        + loaded.stderr,
        encoding="utf-8",
    )
    assert loaded.returncode == 0, loaded.stderr
    installed = json.loads(loaded.stdout)
    locked = {
        p["name"]: p["version"] for p in tomllib.loads((BACKEND / "uv.lock").read_text())["package"]
    }
    assert installed and all(
        locked.get(re.sub(r"[-_.]+", "-", name).lower()) == version
        for name, version in installed.items()
    )
    # H04 uses httpx in the real A2A client; it is now a locked runtime dependency.
    assert not {"pytest", "ruff", "mypy", "pytest-socket"}.intersection(installed)
    assert installed["httpx"] == locked["httpx"]


def test_import_production_graph():
    for path in (BACKEND / "src").rglob("*.py"):
        name = ".".join(path.relative_to(BACKEND / "src").with_suffix("").parts).removesuffix(
            ".__init__"
        )
        importlib.import_module(name)
    assert import_errors(BACKEND / "src") == []
    assert header_errors(BACKEND) == []


def test_invalid_secretless_config_fails_fast(monkeypatch):
    for key in list(os.environ):
        if key.startswith("ORCA_"):
            monkeypatch.delenv(key)
    for values in (
        {},
        {"monitoring_token": ""},
        {"monitoring_token": "a" * 40},
        {"monitoring_token": "changeme" * 8},
        {
            "monitoring_token": secrets.token_urlsafe(48),
            "environment": "production",
            "docs_public": True,
        },
    ):
        with pytest.raises(ValidationError):
            Settings(**values)
    token = secrets.token_urlsafe(48)
    settings = Settings(monitoring_token=SecretStr(token))
    assert token not in repr(settings)
    assert not settings.docs_public
    with pytest.raises(ValidationError):
        Settings(monitoring_token=SecretStr(token), undocumented=True)


def test_production_requires_complete_supabase_configuration():
    token = SecretStr(secrets.token_urlsafe(48))
    base = {"monitoring_token": token, "environment": "production"}
    with pytest.raises(ValidationError, match="database"):
        Settings(**base)
    settings = Settings(
        **base,
        database_url="postgresql+psycopg://runtime:secret@db.example/postgres",
        oidc_issuer="https://project.supabase.co/auth/v1",
        oidc_audience="authenticated",
        oidc_jwks_url="https://project.supabase.co/auth/v1/.well-known/jwks.json",
        object_store_endpoint="https://project.storage.supabase.co/storage/v1/s3",
        object_store_access_key=SecretStr("access-key"),
        object_store_secret_key=SecretStr("secret-key"),
    )
    assert settings.environment == "production"


def test_lint_type_format():
    outputs = []
    for args in (
        ["ruff", "check", "."],
        ["ruff", "format", "--check", "."],
        ["mypy", "src", "scripts"],
    ):
        result = subprocess.run(
            [sys.executable, "-m", *args], cwd=BACKEND, capture_output=True, text=True, timeout=120
        )
        outputs.append(" ".join(args) + "\n" + result.stdout + result.stderr)
        (ROOT / "docs/gates/P01/static-checks.txt").write_text("\n".join(outputs), encoding="utf-8")
        assert result.returncode == 0, outputs[-1]


def secret_findings(root):
    patterns = [
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"ghp_[A-Za-z0-9]{30,}",
    ]
    findings = []
    for folder in ("src", "scripts", "tests"):
        for path in (root / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(re.search(pattern, text) for pattern in patterns):
                findings.append(path.name)
            for node in ast.walk(ast.parse(text)):
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    if (
                        any(
                            isinstance(t, ast.Name)
                            and re.search(
                                r"(?:password|secret|api_key|monitoring_token)$", t.id, re.I
                            )
                            for t in node.targets
                        )
                        and node.value.value
                    ):
                        findings.append(path.name)
    for path in root.glob(".env*"):
        if path.name != ".env.example":
            continue  # Local ignored runtime settings are not release inputs.
        for line in path.read_text().splitlines():
            if line.startswith("ORCA_MONITORING_TOKEN=") and line.split("=", 1)[1].strip():
                findings.append(path.name)
    return findings


def test_no_committed_secrets(tmp_path):
    assert secret_findings(BACKEND) == []
    (tmp_path / "src").mkdir()
    (tmp_path / "src/leak.py").write_text("monitoring_" + "token = 'do-not-commit'\n")
    assert secret_findings(tmp_path) == ["leak.py"]
