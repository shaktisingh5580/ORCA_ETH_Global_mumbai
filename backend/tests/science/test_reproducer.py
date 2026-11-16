"""Purpose: verify P07 input-bound numerical reproduction does not trust origin output.
Behavior: use test-only bytes and an injected approved test method to exercise agreement and failure paths.
Refs: main_implementation plan.md P07.1; Phase: P07.
"""

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from orca.application.reproduction import PinnedInput, reproduce_numeric_result
from orca.contracts.science_evidence import ScienceEvidenceManifest, ScienceSnapshotInput
from orca.science.manifests import source_bytes_digest
from orca.science.registry import MethodRegistry, MethodVersion


def material() -> tuple[ScienceEvidenceManifest, tuple[PinnedInput, ...], bytes, MethodRegistry]:
    raw = (b"28.0", b"27.0")
    original = json.dumps({"anomaly_celsius": 1.0}).encode()
    ids = (uuid4(), uuid4())
    method = MethodVersion(
        method_id="test.recompute",
        version="1",
        code_digest="a" * 64,
        approval_status="approved",
        required_baseline_years=1,
        measurement_semantics="test-only Celsius difference",
        qc_policy_id="test-qc",
        qc_policy_digest="b" * 64,
    )
    manifest = ScienceEvidenceManifest.build(
        id=uuid4(),
        tenant_id=uuid4(),
        project_id=uuid4(),
        created_by="origin",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_mode="synthetic_test",
        method_id=method.method_id,
        method_version=method.version,
        method_code_digest=method.code_digest,
        method_approval_status="approved",
        qc_policy_id=method.qc_policy_id,
        qc_policy_digest=method.qc_policy_digest,
        recipe_digest="sha256:" + "c" * 64,
        result_digest="sha256:" + hashlib.sha256(original).hexdigest(),
        result_status="insufficient_evidence",
        source_qualified=False,
        baseline_qualified=False,
        inputs=(
            ScienceSnapshotInput(
                snapshot_id=ids[0],
                role="current",
                year=2025,
                raw_content_digest=source_bytes_digest(raw[0]),
                byte_count=len(raw[0]),
                object_version_id="v-current",
            ),
            ScienceSnapshotInput(
                snapshot_id=ids[1],
                role="baseline",
                year=2003,
                raw_content_digest=source_bytes_digest(raw[1]),
                byte_count=len(raw[1]),
                object_version_id="v-baseline",
            ),
        ),
    )
    inputs = (PinnedInput(ids[0], raw[0], "v-current"), PinnedInput(ids[1], raw[1], "v-baseline"))
    return manifest, inputs, original, MethodRegistry((method,))


def recalculate(inputs: tuple[PinnedInput, ...]) -> bytes:
    value = float(inputs[0].content) - float(inputs[1].content)
    return json.dumps({"anomaly_celsius": value}).encode()


def reproduce(**changes: object):
    manifest, inputs, original, registry = material()
    values = {
        "manifest": manifest,
        "inputs": inputs,
        "origin_result": original,
        "calculator": recalculate,
        "registry": registry,
        "implementation_digest": "d" * 64,
        "environment_digest": "e" * 64,
        "tolerance_celsius": 1e-9,
    }
    values.update(changes)
    return reproduce_numeric_result(**values)  # type: ignore[arg-type]


def test_repro_of_known_correct_result() -> None:
    result = reproduce()
    assert result.verdict == "reproduced" and result.difference == 0
    assert result.verified_input_count == 2
    assert result.institutional_independence_verified is False


def test_reproducer_not_reuse_origin_output() -> None:
    calls = []

    def different(inputs: tuple[PinnedInput, ...]) -> bytes:
        calls.append(inputs)
        return b'{"anomaly_celsius": 3.0}'

    result = reproduce(calculator=different)
    assert len(calls) == 1 and result.verdict == "mismatch"
    assert result.actual_value == 3.0 and result.original_value == 1.0


def test_digest_mismatch() -> None:
    manifest, inputs, original, _ = material()
    bad = (PinnedInput(inputs[0].snapshot_id, b"99.0", "v-current"), inputs[1])
    with pytest.raises(ValueError, match="input digest"):
        reproduce(manifest=manifest, inputs=bad, origin_result=original)
    with pytest.raises(ValueError, match="origin result digest"):
        reproduce(manifest=manifest, inputs=inputs, origin_result=b'{"anomaly_celsius": 7}')


def test_method_removed_blocks_reproduce() -> None:
    with pytest.raises(ValueError, match="not registered"):
        reproduce(registry=MethodRegistry())
