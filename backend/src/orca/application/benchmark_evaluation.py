"""Purpose: score P07 frozen held-out references without exposing their values in API metadata.
Behavior: read digest-pinned private bytes, align exact sample IDs and persist honest immutable outcomes.
Refs: main_implementation plan.md P07.4; api design.md §4.5; Phase: P07.
"""

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.science.benchmarks import evaluate_benchmark


class PrivateReferenceStore(Protocol):
    def read(self, tenant: UUID, digest: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class FrozenBenchmarkResult:
    id: UUID
    status: Literal["evaluated", "not_evaluable", "blocked"]
    count: int
    mae: float | None
    rmse: float | None
    bias: float | None
    synthetic: bool
    reference_independence_verified: bool
    reason: str | None
    limitations: tuple[str, ...]


def _reference_pairs(raw: bytes) -> dict[str, float]:
    def unique_pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate reference JSON key")
            result[key] = value
        return result

    try:
        document = json.loads(raw, object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("reference set is not JSON") from error
    if not isinstance(document, dict) or set(document) != {"schema", "samples"}:
        raise ValueError("reference set schema mismatch")
    if document["schema"] != "orca.benchmark.reference.v1":
        raise ValueError("reference set version mismatch")
    samples = document["samples"]
    if not isinstance(samples, list) or not 0 < len(samples) <= 10000:
        raise ValueError("reference set must contain bounded samples")
    aligned: dict[str, float] = {}
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != {"id", "observed"}:
            raise ValueError("reference sample schema mismatch")
        key, value = sample["id"], sample["observed"]
        if (
            not isinstance(key, str)
            or not 0 < len(key) <= 128
            or key in aligned
            or isinstance(value, bool)
            or not isinstance(value, (float, int))
            or abs(value) > 1e12
            or not math.isfinite(value)
        ):
            raise ValueError("reference sample ID or value invalid")
        aligned[key] = float(value)
    return aligned


class BenchmarkEvaluator:
    """Internal worker service; caller must authorize project before opening its tenant session."""

    def __init__(self, session: Session, tenant: UUID, store: PrivateReferenceStore):
        self.session, self.tenant, self.store = session, tenant, store

    def evaluate(
        self, *, project_id: UUID, benchmark_id: UUID, predictions: dict[str, float]
    ) -> FrozenBenchmarkResult:
        if (
            not predictions
            or len(predictions) > 10000
            or any(
                not isinstance(key, str)
                or not 0 < len(key) <= 128
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or abs(value) > 1e12
                or not math.isfinite(value)
                for key, value in predictions.items()
            )
        ):
            raise ValueError("predictions require bounded finite keyed values")
        definition = (
            self.session.execute(
                text("""SELECT frozen_test_set_digest,reference_kind,synthetic
              FROM research.benchmark_definitions
              WHERE tenant_id=:tenant AND project_id=:project AND id=:benchmark"""),
                {"tenant": self.tenant, "project": project_id, "benchmark": benchmark_id},
            )
            .mappings()
            .one_or_none()
        )
        if definition is None:
            raise LookupError("benchmark not found")
        prediction_bytes = json.dumps(
            sorted(predictions.items()), separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        prediction_digest = "sha256:" + hashlib.sha256(prediction_bytes).hexdigest()
        reference_digest = definition["frozen_test_set_digest"]
        synthetic = bool(definition["synthetic"])
        limitations = ["Reference source independence has not been externally verified."]
        if synthetic:
            limitations.append("Synthetic benchmark; scores do not establish field accuracy.")
        reason: str | None
        try:
            raw = self.store.read(self.tenant, reference_digest)
        except (OSError, FileNotFoundError):
            status: Literal["evaluated", "not_evaluable", "blocked"] = "not_evaluable"
            reason = "FROZEN_REFERENCE_UNAVAILABLE"
            metrics = None
        except ValueError:
            status = "blocked"
            reason = "FROZEN_REFERENCE_CORRUPT"
            metrics = None
        else:
            if "sha256:" + hashlib.sha256(raw).hexdigest() != reference_digest:
                status, reason, metrics = "blocked", "FROZEN_REFERENCE_DIGEST_MISMATCH", None
            else:
                try:
                    observed = _reference_pairs(raw)
                except ValueError:
                    status, reason, metrics = "blocked", "FROZEN_REFERENCE_SCHEMA_INVALID", None
                else:
                    if set(predictions) != set(observed):
                        status, reason, metrics = "not_evaluable", "SAMPLE_ALIGNMENT_MISMATCH", None
                    else:
                        keys = sorted(observed)
                        metrics = evaluate_benchmark(
                            tuple(float(predictions[key]) for key in keys),
                            tuple(observed[key] for key in keys),
                        )
                        status, reason = "evaluated", None
        identifier = uuid4()
        self.session.execute(
            text("""INSERT INTO research.benchmark_evaluations
              (id,tenant_id,project_id,benchmark_id,prediction_digest,reference_digest,
               metric_algorithm,status,sample_count,mae,rmse,bias,synthetic,reason,limitations)
              VALUES(:id,:tenant,:project,:benchmark,:prediction,:reference,
               'mae_rmse_bias_v1',:status,:count,:mae,:rmse,:bias,:synthetic,:reason,
               CAST(:limitations AS jsonb))"""),
            {
                "id": identifier,
                "tenant": self.tenant,
                "project": project_id,
                "benchmark": benchmark_id,
                "prediction": prediction_digest,
                "reference": reference_digest,
                "status": status,
                "count": metrics.count if metrics else 0,
                "mae": metrics.mae if metrics else None,
                "rmse": metrics.rmse if metrics else None,
                "bias": metrics.bias if metrics else None,
                "synthetic": synthetic,
                "reason": reason,
                "limitations": json.dumps(limitations),
            },
        )
        return FrozenBenchmarkResult(
            identifier,
            status,
            metrics.count if metrics else 0,
            metrics.mae if metrics else None,
            metrics.rmse if metrics else None,
            metrics.bias if metrics else None,
            synthetic,
            False,
            reason,
            tuple(limitations),
        )
