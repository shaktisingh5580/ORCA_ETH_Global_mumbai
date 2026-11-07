"""Purpose: contact institution HTTPS services without allowing SSRF or DNS rebinding.
Behavior: validate every resolved address and connect a pinned IP with hostname-verified TLS.
Refs: institutional plan F02.3; MCP/A2A egress controls; Phase: F02.
"""

import asyncio
import http.client
import ipaddress
import socket
import ssl
import threading
from typing import Any
from urllib.parse import urlsplit

import httpx


def endpoint_origin(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.port not in (None, 443)
        or parsed.path != "/a2a"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or "\\" in endpoint
        or any(ord(c) < 33 for c in endpoint)
    ):
        raise ValueError("A public HTTPS endpoint ending in /a2a on port 443 is required")
    return f"https://{parsed.netloc}"


def public_addresses(host: str) -> list[str]:
    addresses = list(
        dict.fromkeys(str(r[4][0]) for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    )
    if (
        not addresses
        or len(addresses) > 16
        or any(not ipaddress.ip_address(a).is_global for a in addresses)
    ):
        raise ValueError("Endpoint resolves to a non-public address")
    return addresses


class PinnedTLS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        self.tls_context = ssl.create_default_context()
        super().__init__(host, 443, timeout=20, context=self.tls_context)
        self.address = address

    def connect(self) -> None:
        raw = socket.create_connection((self.address, 443), timeout=20)
        try:
            # SNI and certificate verification use the original host, never the pinned IP.
            self.sock = self.tls_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def request_public(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    maximum: int = 262144,
) -> httpx.Response:
    parsed = urlsplit(url)
    if parsed.path not in {"/a2a", "/.well-known/agent-card.json"}:
        raise ValueError("Unapproved provider resource")
    endpoint_origin(f"https://{parsed.netloc}/a2a")
    if parsed.query or parsed.fragment or parsed.scheme != "https":
        raise ValueError("Invalid provider URL")
    host = parsed.hostname
    assert host is not None
    addresses = public_addresses(host)
    connection = PinnedTLS(host, addresses[0])
    deadline: threading.Timer | None = None
    try:
        connection.connect()
        connected = connection.sock

        def interrupt() -> None:
            if connected is not None:
                try:
                    connected.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        deadline = threading.Timer(20, interrupt)
        deadline.daemon = True
        deadline.start()
        connection.request(method, parsed.path, body=body, headers=headers or {})
        response = connection.getresponse()
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise ValueError("Compressed provider responses are not accepted")
        content = response.read(maximum + 1)
        if len(content) > maximum or 300 <= response.status < 400:
            raise ValueError("Provider response is oversized or redirects")
        return httpx.Response(
            response.status,
            content=content,
            headers=dict(response.getheaders()),
            request=httpx.Request(method, url),
        )
    finally:
        if deadline is not None:
            deadline.cancel()
        connection.close()


class PublicProviderTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        return await asyncio.to_thread(
            request_public, request.method, str(request.url), dict(request.headers), body
        )


def fetch_card(endpoint: str) -> dict[str, Any]:
    origin = endpoint_origin(endpoint)
    response = request_public("GET", origin + "/.well-known/agent-card.json", maximum=32768)
    response.raise_for_status()
    value: dict[str, Any] = response.json()
    return value
