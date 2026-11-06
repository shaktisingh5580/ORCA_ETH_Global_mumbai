"""Purpose: prove intentional input bias causes an actual A2A disagreement and successful correction.
Behavior: run separate real agent processes on synthetic captures and retain append-only stage history.
Refs: H05 demonstration gate; Phase: H05; isolated test-only fixtures, no cloud/chain writes.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from disagreement_data import prepare
from disagreement_store import DemoStore
from disagreement_worker import delegate, recipe_for
from network_setup import configuration, unused_port

from orca.cloud_runtime import CloudBundle, await_provider
from orca.infrastructure.federation.runtime import NetworkConfiguration, NetworkRuntime
from orca.infrastructure.federation.signing import strict_json


@pytest.mark.enable_socket
def test_intentional_data_bias_causes_challenge_then_corrected_input_resolves_it(tmp_path):
    runtime, _, secrets = configuration(tmp_path)
    bundle = CloudBundle(
        schema_version="orca.cloud-runtime-bundle/1.0",
        settings={},
        bindings=runtime.configuration,
        coordinator_wallet={},
        providers={
            role: {"service_private_key": private}
            for role, (_, private) in zip(("ocean", "reviewer"), secrets, strict=True)
        },
    )
    commands = prepare(bundle, tmp_path, (unused_port(), unused_port()))
    configured = NetworkConfiguration.model_validate(
        strict_json((tmp_path / "demonstration/bindings.json").read_bytes())
    )
    selected = NetworkRuntime(configured, runtime.private_key, runtime.gateway)
    children = []
    logs = []
    try:
        for name, command in commands:
            log = (tmp_path / (name + ".log")).open("w")
            logs.append(log)
            child = subprocess.Popen(
                command,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                env=dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1"),
            )
            children.append(child)
            await_provider(command, child)
        with httpx.Client(trust_env=False, timeout=1) as client:
            for binding in configured.agents:
                for _ in range(100):
                    try:
                        if (
                            client.get(
                                binding.endpoint.removesuffix("/a2a")
                                + "/.well-known/agent-card.json"
                            ).status_code
                            == 200
                        ):
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise AssertionError("demo provider did not start")
        inputs = strict_json((tmp_path / "demonstration/inputs.json").read_bytes())
        run = {
            "id": str(uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "public_reference": False,
        }
        stages = []
        for phase in range(4):
            stage = delegate(selected, run, phase, recipe_for(inputs, phase, stages))
            stages.append(stage)
        assert [stage["anomaly_celsius"] for stage in stages] == pytest.approx([2, 1, 1, 1])
        assert [stage["verdict"] for stage in stages] == [
            "reproduced",
            "challenged",
            "reproduced",
            "reproduced",
        ]
        assert stages[1]["difference_celsius"] == pytest.approx(1)
        assert stages[2]["corrects_digest"] == stages[0]["artifact"]["content_digest"]
        assert stages[3]["review_of_digest"] == stages[2]["artifact"]["content_digest"]
        assert all(
            stage["artifact"]["payload"]["results"]["source_mode"] == "synthetic_demo"
            for stage in stages
        )
        assert all(
            record["receipt"] is None
            and record["envelope"]["payload"]["source_mode"] == "synthetic_demo"
            for stage in stages
            for record in stage["chain_records"]
        )
        assert (
            inputs["biased"]["input_snapshots"][0]["sha256"]
            != inputs["clean"]["input_snapshots"][0]["sha256"]
        )
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=15)
        for log in logs:
            log.close()


def test_correction_requires_challenge_version_and_preserves_original_evidence(tmp_path):
    import sqlite3

    store = DemoStore(tmp_path)
    tenant, project, subject = map(str, (uuid4(), uuid4(), uuid4()))
    initial = store.create(tenant, project, subject, "same", False)
    assert store.create(tenant, project, subject, "same", False)["id"] == initial["id"]
    with pytest.raises(ValueError):
        store.create(tenant, project, subject, "same", True)
    with pytest.raises(LookupError):
        store.read(initial["id"], str(uuid4()), project)
    with pytest.raises(ValueError):
        store.correct(initial["id"], tenant, project, 1, "correct")
    with store.connect() as db:
        db.execute("UPDATE runs SET state='challenged',version=3 WHERE id=?", (initial["id"],))
        db.execute(
            "INSERT INTO stages VALUES(?,?,?)",
            (initial["id"], 1, json.dumps({"sequence": 1, "anomaly_celsius": 2})),
        )
    with pytest.raises(ValueError):
        store.correct(initial["id"], tenant, project, 2, "stale")
    corrected = store.correct(initial["id"], tenant, project, 3, "correct")
    assert corrected["state"] == "correcting" and corrected["stages"][0]["anomaly_celsius"] == 2
    assert store.correct(initial["id"], tenant, project, 3, "correct") == corrected
    with pytest.raises(sqlite3.IntegrityError):
        with store.connect() as db:
            db.execute("UPDATE stages SET data='{}' WHERE run_id=?", (initial["id"],))
