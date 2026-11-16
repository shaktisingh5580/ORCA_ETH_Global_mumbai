# G04 scientific method review request — pending

**Purpose:** Ask an independent, qualified human scientist to review ORCA's proposed sea surface temperature (SST) method and numerical acceptance tolerances. This document is a request, not an approval or a gate PASS. Phase: P04/G04. References: `main_implementation plan.md` P04.1–P04.4, `api design.md` science contracts, `docs/adr/ADR-R02-native-mur-inputs.json`, `docs/gates/P03/baseline-qualification.json`, and `docs/gates/P04/real-reference-replay.json`.

## Who can review

Seek an oceanographer, marine data scientist, satellite SST researcher, or research software scientist with experience in gridded SST and baseline comparisons. A university marine science department, oceanographic institute, or project collaborator is a practical place to ask. Record the person's name, affiliation, relevant experience, date, and any conflict of interest. The author of the implementation and an AI assistant must not self-certify this review. A paid reviewer is not required by the plan; expertise and an inspectable decision are required.

## Exact proposal to assess

- **Source:** NASA JPL MUR SST v4.1, distributed by NOAA CoastWatch ERDDAP as `jplMURSST41`. Source identity, licensing, and byte digests are recorded in `docs/gates/P03/baseline-qualification.json` and `docs/gates/P03/native-inputs-cloud.json`.
- **Scope:** latitude 20.45–20.55°N, longitude 68.45–68.55°E; January 2025 current period; matching January days from 2003–2022 for the baseline. The selected 11 × 11 native grid and 20 same-product January years are input compatibility evidence, not proof that the baseline is a scientifically valid climate normal.
- **Proposed method:** `orca.sst-area-time-anomaly`, version `0.1.0-reference`, using QC-eligible cells, Celsius conversion, clipped spherical cell area, daily duration weighting, and a baseline matched by grid cell and January day. Implementation: `backend/src/orca/science/methods/sst_anomaly.py`, plus `spatial.py`, `temporal.py`, `units.py`, `quality.py`, and `registry.py` in the same directory.
- **Proposed QC:** `mur-open-ocean-valid-error-v1`: finite SST, open-sea mask = 1, finite `analysis_error` in [0, 327.67] °C. The exact policy digest and calculation code digest are pinned in `docs/gates/P04/real-reference-replay.json`; the reviewer must verify they still match the files at signoff.
- **Recorded replay:** 3,751 current observations; weighted mean 26.24750604815608 °C; baseline weighted mean 25.624008769016 °C; difference +0.6234972791400754 °C. This is a numerical replay on preserved real input bytes and is currently labelled `reference_only_not_approved`.
- **Numerical reproduction tolerance proposed by developers:** absolute difference ≤ 1e-9 °C against the separately implemented reference calculation. This tests code reproducibility, **not** observational uncertainty or scientific significance. Aggregate uncertainty is currently unknown.
- **Coverage threshold examples in tests:** spatial 0.8, temporal 0.9, minimum valid sample count in `backend/tests/science/test_coverage_uncertainty.py`. These are test fixtures, not approved policy values. The reviewer must propose or reject production thresholds, denominator definitions, temporal handling, and the response when uncertainty remains unknown.

## Review steps

1. Check product semantics: MUR foundation/skin interpretation, units, grid, mask, analysis error, temporal sampling, spatial bounds, source version, and attribution. Explain any exclusion or transformation required.
2. Decide whether the 2003–2022 matching-January baseline and day/cell matching support the intended **descriptive local comparison**. State explicitly what the result cannot claim about regional climate, trends, causality, or significance.
3. Inspect the area and time weighting, missing-cell behavior, QC eligibility, leap/calendar handling, antimeridian handling, and baseline incompatibility rejection. Recompute at least one small grid by hand or independent code and the pinned real subset where possible.
4. Set scientifically justified spatial and temporal coverage thresholds, minimum sample count, QC thresholds, and abstention rules. Document how they were chosen. Evaluate whether an uncertainty estimate can be supported; if not, require the result to say `unknown` and limit claims accordingly.
5. Set an **independent numerical acceptance tolerance** for reference cases with its rationale. Separate floating-point replay tolerance from sensor uncertainty and from a threshold for scientific significance.
6. Record any required changes. Approve only an exact method ID/version, source/product version, QC policy digest, implementation digest, coverage policy, tolerance values, and allowed claim language. A code or policy change after approval requires a new review.

## Decision record to return

Copy the following into a signed document or reply from an identifiable institutional address, and attach any independent calculation. Do not record a PASS from an informal conversation.

```
Reviewer name / affiliation / relevant expertise:
Conflict of interest:
Review date (UTC):
Method ID and version:
Implementation SHA-256 and reviewed commit:
Source product/version and subset manifest digest:
QC policy ID and SHA-256:
Baseline suitability and allowed claim:
Spatial coverage threshold and denominator:
Temporal coverage threshold and denominator:
Minimum valid samples:
Uncertainty treatment:
Independent reference values and numerical tolerance (°C), with rationale:
Required limitations and abstention rules:
Decision: APPROVE / APPROVE WITH CONDITIONS / REJECT
Conditions or required changes:
Signature or identifiable written attestation:
```

**Current decision:** PENDING. ORCA's production `METHOD_REGISTRY` remains empty and fails closed until a review and explicit method/policy decision are accepted. G03 must separately pass before G04 can pass. G05–G07 remain downstream acceptance gates.
