"""Purpose: check agent signature domains and Python/Node canonical public-record interoperability.
Behavior: reject tampered payloads, chain/audience/key substitution, duplicate JSON and forged owners.
Refs: canonical signing contract; H04 gate; Phase: H04; synthetic keys only in tests.
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from network_setup import identity

from orca.infrastructure.federation.public_events import public_event
from orca.infrastructure.federation.runtime import ROOT, verify_chain_record
from orca.infrastructure.federation.signing import canonical, sign, strict_json, verify
from orca.network_worker import AssignmentConflict, reject_conflicting_assignments


def test_detached_signature_binds_audience_chain_generation_key_and_payload():
    binding, private = identity("unit", "sst_reproduction", 8123, "0x" + "11" * 20)
    payload = {
        "domain": "orca.agent-coordination/hackathon-v1",
        "chain_id": "7794095",
        "audience": "coordinator",
        "task_id": str(uuid4()),
        "assignment_generation": 1,
        "binding_version": str(binding.binding_version),
        "value": 1.2,
    }
    envelope = sign(payload, private, binding.key_id)
    verify(envelope, binding.public_key, binding.key_id, "coordinator")
    for field, value in (
        ("chain_id", "1"),
        ("audience", "attacker"),
        ("assignment_generation", 2),
        ("value", 1.3),
    ):
        altered = envelope.model_copy(deep=True)
        altered.payload[field] = value
        with pytest.raises(ValueError):
            verify(altered, binding.public_key, binding.key_id, "coordinator")
    with pytest.raises(ValueError):
        verify(envelope, binding.public_key, "unregistered", "coordinator")
    with pytest.raises(ValueError):
        strict_json('{"a":1,"a":2}')
    with pytest.raises(ValueError):
        strict_json('{"a":NaN}')
    assert canonical({"z": 1.0, "a": -0.0}) == b'{"a":0,"z":1}'


def test_real_node_bridge_accepts_same_canonical_signed_public_event_and_rejects_tamper():
    binding, private = identity("unit", "sst_reproduction", 8123, "0x" + "11" * 20)
    now = datetime.now(timezone.utc)
    envelope = public_event(
        task={
            "id": str(uuid4()),
            "recipe_digest": "sha256:" + "a" * 64,
            "deadline": (now + timedelta(hours=1)).isoformat(),
        },
        kind="task",
        actor=str(binding.id),
        binding_version=str(binding.binding_version),
        subject=str(uuid4()),
        generation=0,
        parent_key="",
        reference_digest="sha256:" + "a" * 64,
        operation_id=str(uuid4()),
        issued_at=now.isoformat(),
        private_key=private,
        key_id=binding.key_id,
    )
    data = {
        "command": "validate",
        "envelope": envelope.model_dump(),
        "public_key": binding.public_key,
    }
    output = subprocess.run(
        ["node", str(ROOT / "arkiv/src/coordination.mjs")],
        input=json.dumps(data),
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert output.returncode == 0, output.stderr
    assert json.loads(output.stdout)["digest"] == envelope.content_digest
    record = {
        "envelope": envelope.model_dump(),
        "public_key": binding.public_key,
        "owner": binding.wallet,
    }
    verify_chain_record(record, binding, envelope)
    with pytest.raises(ValueError):
        verify_chain_record({**record, "owner": "0x" + "22" * 20}, binding, envelope)
    data["envelope"]["payload"]["kind"] = "assignment"
    tampered = subprocess.run(
        ["node", str(ROOT / "arkiv/src/coordination.mjs")],
        input=json.dumps(data),
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert tampered.returncode != 0


def test_conflicting_requester_signed_assignment_is_not_resolved_by_query_order():
    binding, private = identity("requester", "sst_reproduction", 8123, "0x" + "11" * 20)
    now = datetime.now(timezone.utc)
    task = {
        "id": str(uuid4()),
        "recipe_digest": "sha256:" + "a" * 64,
        "deadline": (now + timedelta(hours=1)).isoformat(),
        "generation": 1,
        "publication_mode": "public_reference",
    }

    def event(reference):
        return public_event(
            task=task,
            kind="assignment",
            actor=str(binding.id),
            binding_version=str(binding.binding_version),
            subject=str(binding.id),
            generation=1,
            parent_key="",
            reference_digest=reference,
            operation_id=str(uuid4()),
            issued_at=now.isoformat(),
            private_key=private,
            key_id=binding.key_id,
        )

    expected = event("sha256:" + "a" * 64)
    conflicting = event("sha256:" + "b" * 64)
    records = [
        {"owner": binding.wallet, "envelope": item.model_dump()} for item in (expected, conflicting)
    ]
    gateway = SimpleNamespace(call=lambda _: {"records": records, "truncated": False})
    runtime = SimpleNamespace(gateway=gateway, configuration=SimpleNamespace(coordinator=binding))
    for order in (records, list(reversed(records))):
        gateway.call = lambda _: {"records": order, "truncated": False}
        with pytest.raises(AssignmentConflict):
            reject_conflicting_assignments(runtime, task, expected)
