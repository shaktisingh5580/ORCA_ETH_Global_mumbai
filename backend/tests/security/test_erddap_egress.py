"""Purpose: exercise P03 connector trust boundaries using test-only transports.
Behavior: reject private DNS, unapproved hosts, redirects, compression and oversized bytes.
Refs: main plan P03.2; API 7; Phase: P03.2.
"""

import io
from unittest.mock import Mock, patch

import pytest

from orca.infrastructure.connectors.erddap import ERDDAP, SourceError, public_addresses


class Response(io.BytesIO):
    def __init__(self, body=b"{}", status=200, headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


def request(response, limit=20):
    with (
        patch("orca.infrastructure.connectors.erddap.public_addresses", return_value=("8.8.8.8",)),
        patch("orca.infrastructure.connectors.erddap.PinnedHTTPS") as connection,
    ):
        connection.return_value.getresponse.return_value = response
        return ERDDAP("example.org", frozenset({"example.org"}), max_bytes=limit).metadata("sst")


def test_redirect_ssrf_reject():
    with pytest.raises(SourceError):
        ERDDAP("localhost", frozenset({"example.org"}))
    with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 443))]):
        with pytest.raises(SourceError, match="UNSAFE"):
            public_addresses("example.org")
    with pytest.raises(SourceError) as error:
        request(Response(status=302, headers={"Location": "http://127.0.0.1"}))
    assert error.value.status == 302


def test_validated_public_ipv4_precedes_ipv6_without_dropping_fallback():
    with patch(
        "socket.getaddrinfo",
        return_value=[
            (0, 0, 0, "", ("2600:1f18::1", 443)),
            (0, 0, 0, "", ("8.8.8.8", 443)),
        ],
    ):
        assert public_addresses("example.org") == ("8.8.8.8", "2600:1f18::1")


def test_invalid_401_403_429_retry_after():
    for status in (401, 403, 429):
        with pytest.raises(SourceError) as error:
            request(Response(status=status, headers={"Retry-After": "30"}))
        assert error.value.status == status and error.value.retry_after == "30"


def test_oversize_413():
    for response in (Response(b"x" * 21), Response(headers={"Content-Length": "21"})):
        with pytest.raises(SourceError) as error:
            request(response)
        assert error.value.status == 413
    with pytest.raises(SourceError, match="ENCODED"):
        request(Response(headers={"Content-Encoding": "gzip"}))


def test_http_timeout_and_malformed():
    with pytest.raises(SourceError, match="MALFORMED"):
        request(Response(b"not json"))
    with (
        patch("orca.infrastructure.connectors.erddap.public_addresses", return_value=("8.8.8.8",)),
        patch("orca.infrastructure.connectors.erddap.PinnedHTTPS") as connection,
    ):
        connection.return_value.request.side_effect = TimeoutError()
        with pytest.raises(SourceError, match="TIMEOUT"):
            ERDDAP("example.org", frozenset({"example.org"})).metadata("sst")
    with patch("socket.getaddrinfo", side_effect=OSError("DNS failure")):
        with pytest.raises(SourceError, match="TRANSPORT"):
            ERDDAP("example.org", frozenset({"example.org"})).metadata("sst")


def test_validated_address_fallback_stays_within_one_deadline():
    first, second = Mock(), Mock()
    first.request.side_effect = TimeoutError()
    second.getresponse.return_value = Response(b"{}")
    with (
        patch(
            "orca.infrastructure.connectors.erddap.public_addresses",
            return_value=("2600:1f18::1", "8.8.8.8"),
        ),
        patch(
            "orca.infrastructure.connectors.erddap.PinnedHTTPS",
            side_effect=[first, second],
        ) as connection,
    ):
        client = ERDDAP("example.org", frozenset({"example.org"}), timeout=3)
        assert client.metadata("sst") == {}
    assert connection.call_args_list[0].args[:2] == ("example.org", "2600:1f18::1")
    assert connection.call_args_list[1].args[:2] == ("example.org", "8.8.8.8")
    assert 0 < connection.call_args_list[0].args[2] <= 3
    assert 0 < connection.call_args_list[1].args[2] <= 3


def test_default_transport_budget_allows_slow_tls_with_bounded_failover():
    client = ERDDAP("example.org", frozenset({"example.org"}))
    assert client.timeout == 60
    assert client._connect_attempt_seconds == 30


def test_empty_valid_subset():
    client = ERDDAP("example.org", frozenset({"example.org"}))
    with patch.object(client, "_get", return_value=b""):
        assert client.subset("sst", "temperature", ((0, 1, 0),)) == b""
    with pytest.raises(ValueError):
        client.subset("../secret", "temperature", ((0, 1, 0),))
