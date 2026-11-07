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


def inspect_name(name: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+){1,5}\.eth", name) or len(name) > 253:
        raise ValueError("Enter an agent subname such as analyst.orca-research-lab.eth")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("ENS_VERIFIER_UNAVAILABLE")
    environment = {
        k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP"}
    }
    try:
        result = subprocess.run(
            [node, str(ROOT / "arkiv/src/ens.mjs"), "inspect", name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=35,
            env=environment,
        )
        if len(result.stdout) <= 200_000 and result.returncode:
            try:
                failure = json.loads(result.stdout)
            except ValueError:
                failure = {}
            if failure.get("code") == "ENS_NAME_INACTIVE":
                return {
                    "name": name,
                    "status": "ENS_NAME_INACTIVE",
                    "authority_verified": False,
                    "records": {},
                    "message": "The name is not active at every ENSv2 hierarchy level on finalized Sepolia state. Complete the subname registration and wait for finality before retrying.",
                    "research_dispatch_enabled": False,
                }
        if result.returncode or len(result.stdout) > 200_000:
            raise RuntimeError("ENS_READ_UNAVAILABLE_OR_UNSUPPORTED")
        value: dict[str, Any] = json.loads(result.stdout)
        if value.get("authority_verified") is not False or value.get("name") != name:
            raise RuntimeError("ENS_OBSERVATION_INVALID")
        return value
    except (subprocess.TimeoutExpired, OSError, ValueError) as error:
        raise RuntimeError("ENS_READ_UNAVAILABLE_OR_UNSUPPORTED") from error
