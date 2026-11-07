"""Purpose: validate scientific qualification and external provider egress boundaries.
Behavior: exercise unit/format failures and pin TLS connections without contacting real hosts.
Refs: institutional phase F02.3–F02.4; Phase: F02.
"""

import socket

import pytest

from orca.infrastructure.federation import https_egress as egress
from orca.institution_provider import compute


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.org/a2a",
        "https://user:secret@example.org/a2a",
        "https://example.org:8080/a2a",
        "https://example.org/a2a?redirect=x",
        "https://example.org/other",
    ],
)
def test_registration_endpoint_rejects_unsafe_shape(endpoint):
    with pytest.raises(ValueError):
        egress.endpoint_origin(endpoint)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "192.168.1.2"],
)
def test_every_dns_answer_must_be_public(monkeypatch, address):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)),
        ],
    )
    with pytest.raises(ValueError):
        egress.public_addresses("operator.example")


def test_tls_connect_uses_validated_ip_with_original_hostname(monkeypatch):
    calls = []
    raw = object()
    monkeypatch.setattr(
        socket, "create_connection", lambda target, timeout: calls.append(target) or raw
    )
    connection = egress.PinnedTLS("operator.example", "8.8.8.8")

    class TLS:
        def wrap_socket(self, sock, server_hostname):
            assert sock is raw
            calls.append(server_hostname)
            return "test-only-socket"

    connection.tls_context = TLS()
    connection.connect()
    assert calls == [("8.8.8.8", 443), "operator.example"]


def test_actual_summary_requires_explicit_finite_values():
    body = {
        "schema_version": "orca.provider-qualification/1.0",
        "operation_id": "test",
        "values": [26, 28],
        "unit": "C",
    }
    assert compute(body)["mean_celsius"] == 27
    assert compute({**body, "values": [299.15, 301.15], "unit": "K"})[
        "mean_celsius"
    ] == pytest.approx(27)
    for values in ([], [True], [float("nan")], ["27"]):
        with pytest.raises(ValueError):
            compute({**body, "values": values})
    with pytest.raises(ValueError):
        compute({**body, "unit": "guessed"})


@pytest.mark.parametrize(
    "status,encoding,size", [(302, "identity", 1), (200, "gzip", 1), (200, "identity", 33)]
)
def test_provider_response_rejects_redirect_compression_and_oversize(
    monkeypatch, status, encoding, size
):
    class Response:
        def __init__(self):
            self.status = status

        def getheader(self, name, default):
            return encoding

        def read(self, limit):
            return b"x" * min(size, limit)

        def getheaders(self):
            return []

    class Connection:
        sock = None

        def __init__(self, *args):
            pass

        def connect(self):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(egress, "public_addresses", lambda host: ["8.8.8.8"])
    monkeypatch.setattr(egress, "PinnedTLS", Connection)
    with pytest.raises(ValueError):
        egress.request_public(
            "GET", "https://operator.example/.well-known/agent-card.json", maximum=32
        )
