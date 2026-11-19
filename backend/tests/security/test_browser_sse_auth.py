"""Purpose: verify the run event authentication contract in a real browser.
Behavior: serve FastAPI on loopback with a test issuer and controlled event access.
Refs: main_implementation plan.md P14.1; api design.md §§10–11; Phase: P14.
"""

import json
import os
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from orca.api.app import create_app
from orca.api.routers import investigations
from orca.config import Settings
from orca.infrastructure.auth.oidc import OIDCVerifier


@pytest.mark.enable_socket
def test_browser_sse_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    browser = os.environ.get("ORCA_TEST_CHROMIUM")
    if not browser:
        pytest.skip("Set ORCA_TEST_CHROMIUM to a local Chrome or Chromium executable")
    app = create_app(
        Settings(environment="test", monitoring_token="P14-browser-test-token-local-only")
    )
    key = ec.generate_private_key(ec.SECP256R1())
    public = jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    public.update({"alg": "ES256", "kid": "browser-test"})
    issuer = "http://127.0.0.1"
    audience = "orca-api"
    app.state.oidc_verifier = OIDCVerifier(
        issuer, audience, issuer + "/keys", lambda: {"keys": [public]}
    )
    now = datetime.now(UTC)
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": "browser-reviewer",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=3),
    }
    token = jwt.encode(claims, key, algorithm="ES256", headers={"kid": "browser-test"})
    expired = jwt.encode(
        claims | {"exp": now - timedelta(minutes=3)},
        key,
        algorithm="ES256",
        headers={"kid": "browser-test"},
    )
    cursors: list[int] = []

    def events(_investigation, _run, _request, principal, after):
        assert principal.subject == "browser-reviewer"
        cursors.append(after)
        if after == 8:
            raise HTTPException(403, "Access revoked")
        return {"events": [{"sequence": 8, "type": "progress", "data": {"stage": "queue"}}]}

    monkeypatch.setattr(investigations, "run_events", events)
    path = f"/v1/research/investigations/{UUID(int=1)}/runs/{UUID(int=2)}/events/stream"

    @app.get("/p14-browser-test")
    def harness() -> HTMLResponse:
        return HTMLResponse(f"""
          <!doctype html><meta charset="utf-8"><div id="result">pending</div>
          <script type="module">
            const path = {json.dumps(path)};
            const token = {json.dumps(token)};
            const expired = {json.dumps(expired)};
            function get(url, bearer, cursor) {{
              const xhr = new XMLHttpRequest();
              xhr.open("GET", url, false);
              if (bearer) xhr.setRequestHeader("Authorization", "Bearer " + bearer);
              if (cursor) xhr.setRequestHeader("Last-Event-ID", cursor);
              xhr.send();
              return xhr;
            }}
            const stream = get(path, token, "7");
            const ok = get(path).status === 401 &&
              get(path, expired).status === 401 &&
              stream.status === 200 &&
              stream.getResponseHeader("Cache-Control") === "no-store" &&
              stream.getResponseHeader("Content-Type").startsWith("text/event-stream") &&
              stream.responseText.split("id: 8").length === 2 &&
              stream.responseText.includes('data: {{"stage": "queue"}}') &&
              !stream.responseText.includes(token) && !stream.responseText.includes("Access revoked");
            document.querySelector("#result").textContent = ok ? "browser-sse-passed" : "failed";
          </script>
        """)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            assert server.started
            opened = subprocess.run(
                [
                    browser,
                    "--headless",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--user-data-dir={tmp_path / 'p14-browser'}",
                    "--dump-dom",
                    f"http://127.0.0.1:{port}/p14-browser-test",
                ],
                capture_output=True,
                text=True,
                timeout=40,
                check=True,
            )
            assert '<div id="result">browser-sse-passed</div>' in opened.stdout
            assert cursors == [7, 7, 8]
        finally:
            server.should_exit = True
            worker.join(timeout=5)
