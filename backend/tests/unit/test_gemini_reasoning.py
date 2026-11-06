"""Purpose: verify bounded provider retries and safe failure reporting without external calls.
Behavior: emulate throttling only inside tests and verify unchanged request bodies on retry.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A02/A03; Phase: A03.
"""

import httpx
import pytest

from orca.infrastructure import gemini_reasoning as module


def test_provider_throttle_retries_once_with_json(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return (
            httpx.Response(429, json={})
            if len(requests) == 1
            else httpx.Response(
                200, json={"candidates": [{"content": {"role": "model", "parts": []}}]}
            )
        )

    original = httpx.Client
    monkeypatch.setattr(
        module.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler))
    )
    waits = []
    monkeypatch.setattr(module.time, "sleep", waits.append)
    module.GeminiReasoner("first,second").turn("test", [], [])
    assert waits == [] and requests[0].content == requests[1].content
    assert requests[1].headers["Content-Type"] == "application/json"


def test_provider_error_does_not_echo_response_secret(monkeypatch):
    original = httpx.Client
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: original(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(403, text="private-provider-detail")
            )
        ),
    )
    with pytest.raises(module.ModelUnavailable, match="^GEMINI_HTTP_403$"):
        module.GeminiReasoner("test-only").turn("test", [], [])


def test_round_robin_and_cooldown_persist_across_turns(monkeypatch):
    seen = []

    def handler(request):
        key = request.headers["x-goog-api-key"]
        seen.append(key)
        if key == "first":
            return httpx.Response(429, headers={"Retry-After": "3600"}, json={})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": []}}]})

    original = httpx.Client
    monkeypatch.setattr(
        module.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler))
    )
    model = module.GeminiReasoner("first,second,third")
    for _ in range(3):
        model.turn("test", [], [])
    assert seen == ["first", "second", "third", "second"]


def test_all_throttled_fail_bounded_without_sleep(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.headers["x-goog-api-key"])
        return httpx.Response(429, json={})

    original = httpx.Client
    monkeypatch.setattr(
        module.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler))
    )
    model = module.GeminiReasoner("first,second,first")
    with pytest.raises(module.ModelUnavailable, match="GEMINI_HTTP_429"):
        model.turn("test", [], [])
    with pytest.raises(module.ModelUnavailable, match="GEMINI_POOL_COOLDOWN"):
        model.turn("test", [], [])
    assert seen == ["first", "second"]
