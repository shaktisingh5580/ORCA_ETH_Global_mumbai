"""Purpose: retrieve bounded P03 ERDDAP metadata and subsets from approved hosts.
Behavior: pin public DNS addresses, validate TLS, reject redirects and cap time/bytes.
Refs: main plan P03.2; API 7; research-only guide 5; Phase: P03.2.
"""

import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import quote


class SourceError(Exception):
    def __init__(self, code: str, status: int | None = None, retry_after: str | None = None):
        super().__init__(code)
        self.code, self.status, self.retry_after = code, status, retry_after


def public_addresses(host: str) -> tuple[str, ...]:
    addresses = tuple(
        dict.fromkeys(str(x[4][0]) for x in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    )
    if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):
        raise SourceError("UNSAFE_SOURCE_ADDRESS")
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float):
        self.tls_context = ssl.create_default_context()
        super().__init__(host, timeout=timeout, context=self.tls_context)
        self.address = address

    def connect(self) -> None:
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self.tls_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class ERDDAP:
    def __init__(
        self,
        host: str,
        allowed_hosts: frozenset[str],
        *,
        max_bytes: int = 8_388_608,
        timeout: float = 20.0,
    ):
        if host not in allowed_hosts or not re.fullmatch(r"[a-z0-9.-]+", host):
            raise SourceError("SOURCE_NOT_APPROVED")
        if not 1 <= max_bytes <= 64 * 1024 * 1024 or not 0 < timeout <= 60:
            raise ValueError("invalid source limits")
        self.host, self.max_bytes, self.timeout = host, max_bytes, timeout

    def _get(self, path: str) -> bytes:
        started = time.monotonic()
        addresses = public_addresses(self.host)
        remaining = self.timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise SourceError("SOURCE_TIMEOUT")
        connection = PinnedHTTPS(self.host, addresses[0], remaining)
        try:
            connection.request("GET", path, headers={"Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status != 200:
                raise SourceError(
                    "SOURCE_HTTP_ERROR", response.status, response.getheader("Retry-After")
                )
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise SourceError("ENCODED_RESPONSE_REJECTED")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > self.max_bytes):
                raise SourceError("SOURCE_OVERSIZE", 413)
            result = bytearray()
            while True:
                remaining = self.timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise SourceError("SOURCE_TIMEOUT")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, self.max_bytes + 1 - len(result)))
                if not chunk:
                    return bytes(result)
                result.extend(chunk)
                if len(result) > self.max_bytes:
                    raise SourceError("SOURCE_OVERSIZE", 413)
        except (OSError, http.client.HTTPException) as exc:
            raise SourceError("SOURCE_TRANSPORT_ERROR") from exc
        finally:
            connection.close()

    @staticmethod
    def _identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", value):
            raise ValueError("invalid ERDDAP identifier")
        return value

    def metadata(self, product: str) -> object:
        content = self._get(f"/erddap/info/{self._identifier(product)}/index.json")
        try:
            return json.loads(content)
        except (ValueError, UnicodeError) as exc:
            raise SourceError("MALFORMED_SOURCE_METADATA") from exc

    def subset(
        self, product: str, variable: str, slices: tuple[tuple[int, int, int], ...]
    ) -> bytes:
        if not 1 <= len(slices) <= 5:
            raise ValueError("metadata-derived dimensions required")
        cells = 1
        for start, stride, stop in slices:
            if start < 0 or stride <= 0 or stop < start:
                raise ValueError("invalid index slice")
            cells *= (stop - start) // stride + 1
        if cells > 1_000_000:
            raise SourceError("SUBSET_CELL_LIMIT", 413)
        query = self._identifier(variable) + "".join(f"[{a}:{b}:{c}]" for a, b, c in slices)
        return self._get(f"/erddap/griddap/{self._identifier(product)}.nc?{quote(query, safe=':')}")
