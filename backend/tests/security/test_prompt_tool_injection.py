"""Purpose: verify companion replies fail closed under bad model output or lost access.
Behavior: bound classification, keep evidence local, and cite only verified inputs.
Refs: main implementation plan P12.2; A-API §§3.13,17.
"""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from p06_evidence import insufficient_manifest

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.api.routers import companion_reply
from orca.application.companion_reply import ReplyRequest, answer
from orca.config import Settings
from orca.contracts.science_evidence import ScienceEvidenceManifest
from orca.domain.identity import Principal
from orca.infrastructure import gemini_companion


def supported():
    fixture = insufficient_manifest()
    values = fixture.model_dump(exclude={"manifest_digest"})
    values["inputs"] = fixture.inputs
    values.update(
        source_mode="preserved_real",
        method_approval_status="approved",
        result_status="supported",
        source_qualified=True,
        baseline_qualified=True,
        absolute_tolerance_celsius=0.01,
    )
    return ScienceEvidenceManifest.build(**values)


def body(**values):
    return ReplyRequest(artifact_id=uuid4(), question="What years were covered?", **values)


def test_llm_output_cannot_invent_science():
    request = body(share_question_with_model=True)
    manifest = supported()
    forged = answer(request, manifest, lambda _: "The SST is 99 C")  # intentionally invalid
    assert forged.status == "abstained" and forged.citation is None
    result = answer(request, manifest, lambda _: "coverage")
    assert result.citation.manifest_digest == manifest.manifest_digest
    assert result.citation.artifact_id == request.artifact_id
    assert "2025" in result.text and "2003" in result.text
    assert "99" not in result.text


def test_model_unavailable_returns_partial():
    result = answer(body(share_question_with_model=True), supported(), lambda _: None)
    assert result.status == "abstained" and result.reason == "model_unavailable"
    assert result.citation is None


def test_injection_from_data_source_untrusted():
    manifest = insufficient_manifest()

    def forbidden(_):
        pytest.fail("Synthetic or insufficient evidence must not reach the model")

    assert (
        answer(body(share_question_with_model=True), manifest, forbidden).reason
        == "evidence_unavailable"
    )
    assert answer(body(), supported(), forbidden).reason == "model_consent_required"


def test_tool_scope_and_budget(monkeypatch):
    calls = []
    output = {"text": "coverage"}

    class Connection:
        def __init__(self, host, timeout):
            assert host == "generativelanguage.googleapis.com" and timeout == 5

        def request(self, method, path, payload, headers):
            assert method == "POST"
            assert path == "/v1beta/models/gemini-test:generateContent"
            data = json.loads(payload)
            assert "tools" not in data
            assert data["generationConfig"] == {"maxOutputTokens": 64, "candidateCount": 1}
            assert data["contents"][0]["parts"] == [{"text": "years?"}]
            assert b"test-key" not in payload
            assert headers["x-goog-api-key"] == "test-key"
            calls.append(data)

        def getresponse(self):
            def read(size):
                assert size == 4097
                return json.dumps(
                    {"candidates": [{"finishReason": "STOP", "content": {"parts": [output]}}]}
                ).encode()

            return SimpleNamespace(status=200, read=read)

        def close(self):
            pass

    monkeypatch.setattr(gemini_companion.http.client, "HTTPSConnection", Connection)
    assert gemini_companion.classify("test-key", "gemini-test", "years?") == "coverage"
    output = {"functionCall": {"name": "get_private_evidence"}}
    assert gemini_companion.classify("test-key", "gemini-test", "years?") is None
    assert gemini_companion.classify("test-key", "gemini-test", "x" * 1201) is None
    assert len(calls) == 2


@pytest.mark.enable_socket
def test_reply_rechecks_authorization_before_disclosing_citation(monkeypatch):
    token = "abcdefghijklmnopqrstuvwxyz0123456789"
    app = create_app(
        Settings(
            monitoring_token=token,
            companion_enabled=True,
            companion_model_enabled=True,
            gemini_api_key="test-key",
        )
    )
    app.dependency_overrides[current_principal] = lambda: Principal("member", "issuer", frozenset())
    manifest = supported()
    reads = []
    calls = []

    def read(_, principal, org, conversation, artifact, **kwargs):
        reads.append(artifact)
        return manifest if len(reads) == 1 else None

    def classify(_, model, question):
        calls.append(question)
        return "coverage"

    monkeypatch.setattr(companion_reply, "authorized_manifest", read)
    monkeypatch.setattr(
        companion_reply,
        "service",
        lambda *_: nullcontext(
            SimpleNamespace(lock=lambda _: SimpleNamespace(archived=False)),
        ),
    )
    monkeypatch.setattr(
        companion_reply,
        "accessible_manifest",
        lambda repo, artifact: read(None, None, None, None, artifact),
    )
    monkeypatch.setattr(companion_reply, "classify", classify)
    request = body(share_question_with_model=True)
    with TestClient(app) as client:
        response = client.post(
            f"/v1/conversations/{uuid4()}/reply",
            params={"organization_id": str(uuid4())},
            json=request.model_dump(mode="json"),
        )
    assert response.status_code == 200
    assert response.json()["reason"] == "evidence_unavailable"
    assert response.json()["citation"] is None
    assert reads == [request.artifact_id, request.artifact_id]
    assert calls == [request.question]
