"""Purpose: expose bounded read-only ENSv2 observations for institutional onboarding.
Behavior: invoke the pinned verifier without secrets, distinguish unavailability from authority.
Refs: institutional plan F03.1; contracts/ens/sepolia.json; Phase: F03 observation increment.
"""

import json
import os
import re
import shutil
import subprocess
from typing import Any

from orca.infrastructure.federation.runtime import ROOT


def _validate_agent_name(name: str) -> None:
    if (
        not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+){1,4}\.eth", name)
        or len(name) > 253
        or any(len(label) > 63 for label in name.split("."))
    ):
        raise ValueError("Enter an agent subname such as analyst.orca-research-lab.eth")


def _run_verifier(
    *args: str, timeout: int = 35, program: str = "ens.mjs", payload: str | None = None
) -> dict[str, Any]:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("ENS_VERIFIER_UNAVAILABLE")
    environment = {
        k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP"}
    }
    try:
        result = subprocess.run(
            [node, str(ROOT / "arkiv/src" / program), *args],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env=environment,
        )
        if len(result.stdout) <= 200_000 and result.returncode:
            try:
                failure = json.loads(result.stdout)
            except ValueError:
                failure = {}
            if (
                args[:1] == ("inspect",)
                and isinstance(failure, dict)
                and failure.get("code") == "ENS_NAME_INACTIVE"
            ):
                return {
                    "name": args[1],
                    "status": "ENS_NAME_INACTIVE",
                    "authority_verified": False,
                    "records": {},
                    "message": "The name is not active at every ENSv2 hierarchy level on finalized Sepolia state. Complete the subname registration and wait for finality before retrying.",
                    "research_dispatch_enabled": False,
                }
            if (
                program == "identity-binding.mjs"
                and isinstance(failure, dict)
                and failure.get("status") == "SIGNATURES_REJECTED"
            ):
                return {"status": "SIGNATURES_REJECTED", "authority_verified": False}
        if result.returncode or len(result.stdout) > 200_000:
            raise RuntimeError("ENS_READ_UNAVAILABLE_OR_UNSUPPORTED")
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get("authority_verified") is not False:
            raise RuntimeError("ENS_OBSERVATION_INVALID")
        return value
    except (subprocess.TimeoutExpired, OSError, ValueError) as error:
        raise RuntimeError("ENS_READ_UNAVAILABLE_OR_UNSUPPORTED") from error


def inspect_name(name: str) -> dict[str, Any]:
    _validate_agent_name(name)
    result = _run_verifier("inspect", name)
    if result.get("name") != name:
        raise RuntimeError("ENS_OBSERVATION_INVALID")
    return result


def inspect_resolver_boundary(first: str, second: str) -> dict[str, Any]:
    return _inspect_pair("compare-resolvers", first, second)


def _inspect_pair(command: str, first: str, second: str, *args: str) -> dict[str, Any]:
    _validate_agent_name(first)
    _validate_agent_name(second)
    if first == second:
        raise ValueError("Choose two different agent names")
    result = _run_verifier(command, first, second, *args, timeout=50)
    names = result.get("names")
    if (
        not isinstance(names, list)
        or len(names) != 2
        or not all(isinstance(observation, dict) for observation in names)
        or [observation.get("name") for observation in names] != [first, second]
    ):
        raise RuntimeError("ENS_OBSERVATION_INVALID")
    return result


def inspect_endpoint_permissions(first: str, second: str, delegate: str) -> dict[str, Any]:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", delegate) or int(delegate, 16) == 0:
        raise ValueError("A nonzero delegate address is required")
    result = _inspect_pair("endpoint-permissions", first, second, delegate.lower())
    if (
        not isinstance(result.get("delegate"), str)
        or result["delegate"].lower() != delegate.lower()
    ):
        raise RuntimeError("ENS_OBSERVATION_INVALID")
    return result


def verify_binding_signatures(
    manifest: dict[str, Any], signatures: dict[str, Any]
) -> dict[str, Any]:
    result = _run_verifier(
        timeout=15,
        program="identity-binding.mjs",
        payload=json.dumps({"expected": manifest, "signatures": signatures}),
    )
    if result.get("status") == "SIGNATURES_REJECTED":
        raise PermissionError("Identity binding signatures were rejected")
    if result.get("status") != "SIGNATURES_VALID" or not all(
        isinstance(result.get(field), str) and re.fullmatch(r"0x[0-9a-f]{64}", result[field])
        for field in ("ens_digest", "arkiv_digest")
    ):
        raise RuntimeError("ENS_BINDING_VERIFIER_INVALID")
    return {field: result[field] for field in ("ens_digest", "arkiv_digest")}
