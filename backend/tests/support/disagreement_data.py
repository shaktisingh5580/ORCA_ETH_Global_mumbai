"""Purpose: intentionally bias one synthetic input copy for the live disagreement demonstration.
Behavior: generate test-only captures, preserve clean/biased hashes and configure isolated A2A agents.
Refs: H05 cloud/disagreement decision; Phase: H05; excluded from production wheel and image.
"""

import copy
import csv
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid5

from network_setup import source_recipe

from orca.cloud_runtime import CloudBundle, env_text, private_file
from orca.infrastructure.federation.runtime import NetworkConfiguration


def prepare(
    bundle: CloudBundle, state: Path, ports: tuple[int, int] = (8201, 8202)
) -> list[tuple[str, list[str]]]:
    import sys

    root = state / "demonstration"
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "inputs.json"
    if manifest.exists():
        inputs = json.loads(manifest.read_text())
    else:
        clean_parent = root / "clean"
        clean_parent.mkdir()
        clean, result = source_recipe(clean_parent)
        biased = root / "biased"
        biased.mkdir()
        for path in clean.iterdir():
            (biased / path.name).write_bytes(path.read_bytes())
        rows = list(csv.reader(io.StringIO((biased / "2025-01.csv").read_text())))
        for row in rows[2:]:
            row[3] = str(float(row[3]) + 1.0)
        stream = io.StringIO()
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerows(rows)
        raw = stream.getvalue().encode()
        (biased / "2025-01.csv").write_bytes(raw)
        wrong = copy.deepcopy(result)
        wrong["current_mean"] += 1.0
        wrong["anomaly"] += 1.0
        for day in wrong["daily"]:
            day["current_mean"] += 1.0
            day["anomaly"] += 1.0
        for member in wrong["input_snapshots"]:
            if member["year"] == 2025:
                member["sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
        result["source_mode"] = wrong["source_mode"] = "synthetic_demo"
        inputs = {
            "source_mode": "synthetic_demo",
            "scenario_id": "biased-current-sst-v1",
            "injection": "Intentionally add 1.0 Celsius to every current-month synthetic observation in the producer copy",
            "clean_folder": str(clean),
            "biased_folder": str(biased),
            "clean": result,
            "biased": wrong,
        }
        private_file(manifest, json.dumps(inputs, indent=2))
    agents = []
    commands = []
    for role, port in (("ocean", ports[0]), ("reviewer", ports[1])):
        original = next(
            agent
            for agent in bundle.bindings.agents
            if agent.capability == ("sst_reproduction" if role == "ocean" else "sst_review")
        )
        binding = original.model_copy(
            update={
                "id": uuid5(original.id, "synthetic-demo"),
                "binding_version": uuid5(original.binding_version, "synthetic-demo"),
                "endpoint": f"http://127.0.0.1:{port}/a2a",
                "name": "DEMO " + original.name,
            }
        )
        agents.append(binding)
        binding_file = root / f"{role}-binding.json"
        private_file(binding_file, binding.model_dump_json())
        private_file(
            root / f"{role}.env",
            env_text(
                {
                    "ORCA_PROVIDER_BINDING_FILE": str(binding_file),
                    "ORCA_PROVIDER_COORDINATOR_FILE": str(root / "bindings.json"),
                    "ORCA_PROVIDER_PRIVATE_KEY": bundle.providers[role]["service_private_key"],
                    "ORCA_PROVIDER_WALLET_ENV_FILE": str(state / f"{role}-wallet.env"),
                    "ORCA_PROVIDER_WALLET_STATE_DIR": str(state / role),
                    "ORCA_PROVIDER_STATE_DIR": str(root / role),
                    "ORCA_PROVIDER_CAPTURE_FOLDER": inputs["biased_folder"]
                    if role == "ocean"
                    else inputs["clean_folder"],
                    "ORCA_PROVIDER_SOURCE_MODE": "synthetic_demo",
                    "ORCA_PROVIDER_CAPTURE_VARIANTS": json.dumps(
                        {"biased": inputs["biased_folder"], "clean": inputs["clean_folder"]}
                    ),
                }
            ),
        )
        commands.append(
            (
                "demo-" + role,
                [
                    sys.executable,
                    "-m",
                    "orca.agent_provider",
                    "--env-file",
                    str(root / f"{role}.env"),
                ],
            )
        )
    config = NetworkConfiguration(coordinator=bundle.bindings.coordinator, agents=tuple(agents))
    config.validate_endpoints()
    private_file(root / "bindings.json", config.model_dump_json(indent=2))
    return commands
