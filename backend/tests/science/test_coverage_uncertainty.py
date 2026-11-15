"""Purpose: verify explicit coverage thresholds, uncertainty state and claim limits.
Behavior: test-only evidence fixtures establish abstention and reject unsupported claims.
Refs: main_implementation plan.md P04.3; api design.md §§4.4,7; docs/07_scientific_engine.md; Phase: P04.
"""

import pytest

from orca.science.quality import (
    EvidenceMetrics,
    EvidenceQualificationPolicy,
    qualify_evidence,
)

POLICY = EvidenceQualificationPolicy(
    minimum_spatial_coverage=0.8,
    minimum_temporal_coverage=0.9,
    minimum_valid_samples=10,
)


def metrics(**changes) -> EvidenceMetrics:
    values = {
        "spatial_coverage": 0.95,
        "temporal_coverage": 1.0,
        "valid_samples": 31,
        "method_approved": True,
        "source_qualified": True,
        "baseline_qualified": True,
        "uncertainty_celsius": None,
    }
    values.update(changes)
    return EvidenceMetrics(**values)


def test_below_spatial_coverage_abstains():
    assert (
        qualify_evidence(metrics(spatial_coverage=0.5), POLICY).outcome == "insufficient_evidence"
    )


def test_below_temporal_coverage_abstains():
    assert (
        qualify_evidence(metrics(temporal_coverage=0.5), POLICY).outcome == "insufficient_evidence"
    )


def test_uncertainty_unknown_not_zero():
    report = qualify_evidence(metrics(), POLICY)
    assert report.uncertainty_celsius is None
    assert report.uncertainty_status == "unknown"
    assert any("not treated as zero" in item for item in report.limitations)


def test_sufficient_evidence_supported_only():
    assert qualify_evidence(metrics(), POLICY).outcome == "supported"
    assert qualify_evidence(metrics(method_approved=False), POLICY).outcome == "invalid"


def test_correlation_not_causation():
    report = qualify_evidence(metrics(claim_kind="causal"), POLICY)
    assert report.outcome == "inconclusive"
    assert any("does not establish a causal" in item for item in report.limitations)


def test_unknown_coverage_never_defaults_to_complete():
    report = qualify_evidence(metrics(spatial_coverage=None), POLICY)
    assert report.outcome == "insufficient_evidence"
    with pytest.raises(ValueError, match="coverage values"):
        qualify_evidence(metrics(temporal_coverage=1.1), POLICY)
