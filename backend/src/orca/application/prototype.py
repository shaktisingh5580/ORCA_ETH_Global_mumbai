"""Purpose: implement the bounded hackathon investigation, approval and durable-run lifecycle.
Behavior: require current project authority, pin source bundles and preserve completed result integrity.
Refs: HACKATHON_IMPLEMENTATION_PLAN.md H02; prototype API contracts; Phase: H02.
"""

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.projects import IdempotencyConflict
from orca.application.workspace_access import authorize_project
from orca.contracts.prototype import CreateInvestigation, Investigation, PrototypePlan, PrototypeRun
from orca.domain.identity import Principal


def digest_result(result: dict[str, Any]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )


def guided_plan(bundle_digest: str, summary: str | None = None) -> PrototypePlan:
    return PrototypePlan(
        bundle_digest=bundle_digest,
        planner="gemini" if summary else "guided",
        summary=summary
        or "Compare the available January 2025 sea surface temperature with the same seasonal days in a 20-year reference, then show the difference and its evidence.",
        scope="68.45–68.55°E, 20.45–20.55°N · January 2025 · native MUR grid",
        baseline="Matching January days, 2003–2022 (20 years)",
        steps=[
            "Load the preserved current and reference data",
            "Check the ocean mask, units and seasonal alignment",
            "Calculate an area-weighted daily and monthly difference",
            "Save the chart, report and source hashes",
        ],
        limitations=[
            "This is the available live recipe; other regions, periods or variables require additional inputs.",
            "Gemini, when configured, explains the fixed plan; deterministic code computes the result.",
            "Research prototype; independent scientific qualification is deferred.",
        ],
    )


class Prototype:
    def __init__(self, session: Session, tenant: UUID, project: UUID, principal: Principal):
        self.session, self.tenant, self.project, self.principal = (
            session,
            tenant,
            project,
            principal,
        )

    def authorize(self, scope: str = "project:read") -> None:
        authorize_project(self.session, self.tenant, self.project, self.principal, scope)

    @staticmethod
    def investigation(row: Any) -> Investigation:
        return Investigation(
            id=row["id"],
            project_id=row["project_id"],
            question=row["question"],
            state=row["state"],
            plan=PrototypePlan.model_validate(row["plan"]),
            created_at=row["created_at"],
        )

    def create(self, body: CreateInvestigation, plan: PrototypePlan) -> Investigation:
        self.authorize("project:write")
        row = (
            self.session.execute(
                text("""INSERT INTO research.prototype_investigations
          (id,tenant_id,project_id,created_by,question,plan) VALUES(:id,:t,:p,:s,:q,CAST(:plan AS jsonb)) RETURNING *"""),
                {
                    "id": uuid4(),
                    "t": self.tenant,
                    "p": self.project,
                    "s": self.principal.subject,
                    "q": body.question,
                    "plan": plan.model_dump_json(),
                },
            )
            .mappings()
            .one()
        )
        return self.investigation(row)

    def investigations(self) -> list[Investigation]:
        self.authorize()
        rows = self.session.execute(
            text(
                "SELECT * FROM research.prototype_investigations WHERE tenant_id=:t AND project_id=:p ORDER BY created_at DESC LIMIT 30"
            ),
            {"t": self.tenant, "p": self.project},
        ).mappings()
        return [self.investigation(row) for row in rows]

    def approve(self, identifier: UUID) -> Investigation:
        self.authorize("project:write")
        row = (
            self.session.execute(
                text("""UPDATE research.prototype_investigations SET state='approved',approved_by=:s,
          updated_at=clock_timestamp() WHERE tenant_id=:t AND project_id=:p AND id=:id RETURNING *"""),
                {
                    "t": self.tenant,
                    "p": self.project,
                    "id": identifier,
                    "s": self.principal.subject,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("investigation not found")
        return self.investigation(row)

    def submit(self, investigation_id: UUID, key: str) -> PrototypeRun:
        self.authorize("project:write")
        investigation = (
            self.session.execute(
                text(
                    "SELECT * FROM research.prototype_investigations WHERE tenant_id=:t AND project_id=:p AND id=:id FOR UPDATE"
                ),
                {"t": self.tenant, "p": self.project, "id": investigation_id},
            )
            .mappings()
            .one_or_none()
        )
        if investigation is None:
            raise LookupError("investigation not found")
        if investigation["state"] != "approved":
            raise ValueError("Review and approve the plan before starting a run")
        identifier = uuid4()
        inserted = self.session.execute(
            text("""INSERT INTO research.prototype_runs
          (id,tenant_id,project_id,investigation_id,created_by,idempotency_key,bundle_digest)
          VALUES(:id,:t,:p,:inv,:s,:key,:bundle)
          ON CONFLICT(tenant_id,investigation_id,idempotency_key) DO NOTHING RETURNING id"""),
            {
                "id": identifier,
                "t": self.tenant,
                "p": self.project,
                "inv": investigation_id,
                "s": self.principal.subject,
                "key": key,
                "bundle": investigation["plan"]["bundle_digest"],
            },
        ).scalar_one_or_none()
        if inserted is None:
            previous = self.session.execute(
                text(
                    "SELECT id,created_by FROM research.prototype_runs WHERE tenant_id=:t AND investigation_id=:inv AND idempotency_key=:key"
                ),
                {"t": self.tenant, "inv": investigation_id, "key": key},
            ).one()
            if previous.created_by != self.principal.subject:
                raise IdempotencyConflict()
            identifier = previous.id
        return self.run(identifier)

    def run_row(self, identifier: UUID) -> Any:
        self.authorize()
        row = (
            self.session.execute(
                text(
                    "SELECT * FROM research.prototype_runs WHERE tenant_id=:t AND project_id=:p AND id=:id"
                ),
                {"t": self.tenant, "p": self.project, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("run not found")
        if row["status"] == "succeeded" and (
            row["result"] is None or digest_result(row["result"]) != row["result_digest"]
        ):
            raise ValueError("stored result integrity check failed")
        return row

    def run(self, identifier: UUID) -> PrototypeRun:
        row = self.run_row(identifier)
        return PrototypeRun.model_validate(
            {field: row[field] for field in PrototypeRun.model_fields}
        )


def render_report(question: str, result: dict[str, Any], result_digest: str) -> str:
    sign = "warmer" if result["anomaly"] >= 0 else "cooler"
    lines = [
        "# ORCA research report",
        "",
        "## Research question",
        question,
        "",
        "## Result",
        f"January 2025 was **{abs(result['anomaly']):.3f} °C {sign}** than the matching 2003–2022 January reference in the selected region.",
        f"Current area-weighted mean: {result['current_mean']:.3f} °C. Reference mean: {result['baseline_mean']:.3f} °C.",
        "",
        "## Scope and evidence",
        f"Bounds: {result['bbox']} (west, south, east, north; EPSG:4326).",
        f"Method: `{result['method']}`. Current cells: {result['current_cells']}; baseline cells: {result['baseline_cells']}.",
        f"Source bundle: `{result['bundle_digest']}`.",
        f"Result digest: `{result_digest}`.",
        "Source: [NASA JPL MUR via NOAA ERDDAP](https://coastwatch.pfeg.noaa.gov/erddap/info/jplMURSST41/index.html).",
        "Acknowledgment: JPL data produced with support from NASA MEaSUREs.",
        "",
        "## Interpretation",
        "The difference describes this bounded seasonal comparison. It does not establish a cause or a broader ocean trend.",
        "",
        "## Limitations",
        *["- " + value for value in result["limitations"]],
        "- Aggregate uncertainty remains unknown; no calibrated confidence score is assigned.",
    ]
    return "\n\n".join(lines) + "\n"
