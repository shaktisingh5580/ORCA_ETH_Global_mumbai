"""Purpose: verify public discovery excludes unsafe or already assigned tasks.
Behavior: use signed local records and reject incomplete histories and unrelated closure records.
Refs: H05 public discovery gate; Phase: H05; no cloud writes.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from network_setup import configuration

from orca.infrastructure.federation.discovery import discover_open
from orca.infrastructure.federation.signing import sign


@pytest.mark.enable_socket
def test_discovery_honors_complete_history_task_identity_and_region(tmp_path, monkeypatch):
    runtime, _, _ = configuration(tmp_path)
    coordinator = runtime.configuration.coordinator
    identifiers = [str(uuid4()) for _ in range(4)]

    def record(task, kind="task", region="north_arabian_sea"):
        envelope = sign(
            {
                "kind": kind,
                "audience": "public",
                "assignment_generation": "0",
                "binding_version": str(coordinator.binding_version),
                "task_id": task,
                "region": region,
                "source_mode": "real",
                "capability": "sst_reproduction",
                "deadline": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            },
            runtime.private_key,
            coordinator.key_id,
        )
        return {"owner": coordinator.wallet, "envelope": envelope.model_dump(mode="json")}

    records = [record(identifier) for identifier in identifiers]
    records[-1] = record(identifiers[-1], region="other_region")

    def call(body):
        if body["command"] == "discover":
            return {"records": records, "truncated": False, "observed_at": "test"}
        task = body["task_id"]
        return {
            "records": [record(task if task == identifiers[1] else str(uuid4()), "assignment")],
            "truncated": task == identifiers[2],
        }

    monkeypatch.setattr(
        "orca.infrastructure.federation.discovery.verify_chain_record", lambda *args: None
    )
    found = discover_open(SimpleNamespace(call=call), coordinator, "sst_reproduction")
    assert [row["envelope"]["payload"]["task_id"] for row in found["records"]] == [identifiers[0]]
    assert found["requires_explicit_requester_assignment"] is True
