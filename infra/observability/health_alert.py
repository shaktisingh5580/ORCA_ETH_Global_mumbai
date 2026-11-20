"""Purpose: check ORCA health and send redacted alerts to operator-configured receivers.
Behavior: route failed probes by role, keep readiness tokens out of alerts, and fail on delivery errors.
Refs: main_implementation plan.md P14.5; api design.md §14. Phase: P14.
"""

import json
import os
import sys
from http.client import HTTPSConnection
from urllib.parse import urlsplit

ROUTES = {
    "/livez": ("orca_liveness_failed", "platform"),
    "/readyz": ("orca_readiness_failed", "platform"),
}


def https_endpoint(url):
    endpoint = urlsplit(url)
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.fragment
        or endpoint.query
    ):
        raise ValueError("Use a dedicated HTTPS URL without embedded credentials")
    return endpoint


def request(url, method, headers, body=None):
    endpoint = https_endpoint(url)
    client = HTTPSConnection(endpoint.hostname, endpoint.port, timeout=5)
    try:
        client.request(method, endpoint.path or "/", body=body, headers=headers)
        return client.getresponse().status
    finally:
        client.close()


def check(origin, monitor_token, receivers, transport=request):
    endpoint = https_endpoint(origin)
    if endpoint.path not in ("", "/"):
        raise ValueError("Use an ORCA HTTPS origin")
    if not monitor_token or not receivers.get("platform"):
        raise ValueError("Monitoring token and platform receiver are required")
    https_endpoint(receivers["platform"])
    failures = []
    for path, (alert, owner) in ROUTES.items():
        headers = (
            {"Authorization": f"Bearer {monitor_token}"} if path == "/readyz" else {}
        )
        try:
            observed = transport(origin.rstrip("/") + path, "GET", headers)
        except (OSError, ValueError):
            observed = None
        if observed == 200:
            continue
        payload = {"alert": alert, "owner": owner, "http_status": observed}
        try:
            status = transport(
                receivers[owner],
                "POST",
                {"Content-Type": "application/json"},
                json.dumps(payload).encode("utf-8"),
            )
            delivered = 200 <= status < 300
        except (OSError, ValueError):
            delivered = False
        failures.append({**payload, "delivered": delivered})
    return failures


def main():
    try:
        failures = check(
            os.environ["ORCA_MONITOR_ORIGIN"],
            os.environ["ORCA_MONITORING_TOKEN"],
            {"platform": os.environ["ORCA_PLATFORM_ALERT_URL"]},
        )
    except (KeyError, ValueError):
        print('{"status":"monitor_configuration_invalid"}', file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"status": "unhealthy" if failures else "healthy", "alerts": failures}
        )
    )
    return 2 if any(not item["delivered"] for item in failures) else int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
