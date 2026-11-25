"""Purpose: answer narrow research questions using verified artifact metadata.
Behavior: the model selects a reply type; ORCA supplies every claim and citation.
Refs: main implementation plan P12.2; A-API §§3.13,17.
"""

from collections.abc import Callable
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from orca.contracts.common import Contract
from orca.contracts.science_evidence import ScienceEvidenceManifest

Intent = Literal["coverage", "method", "limitations", "unsupported"]
Selector = Callable[[str], Intent | None]


class ReplyRequest(Contract):
    artifact_id: UUID | None = None
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)
    ]
    share_question_with_model: Annotated[bool, Field(strict=True)] = False


class Citation(Contract):
    artifact_id: UUID
    manifest_digest: str
    href: str


class Reply(Contract):
    status: Literal["answered", "abstained"]
    reason: (
        Literal[
            "evidence_unavailable",
            "model_consent_required",
            "model_unavailable",
            "question_unsupported",
        ]
        | None
    ) = None
    text: str
    citation: Citation | None = None


def unavailable_evidence() -> Reply:
    return Reply(
        status="abstained",
        reason="evidence_unavailable",
        text="I do not have an accessible, verified research artifact that supports an answer.",
    )


def answer(
    body: ReplyRequest, manifest: ScienceEvidenceManifest | None, select: Selector | None
) -> Reply:
    if body.artifact_id is None or manifest is None or manifest.result_status != "supported":
        return unavailable_evidence()
    if not body.share_question_with_model:
        return Reply(
            status="abstained",
            reason="model_consent_required",
            text="Allow the model to process this question to request a reply.",
        )
    intent = select(body.question) if select else None
    if intent is None:
        return Reply(
            status="abstained",
            reason="model_unavailable",
            text="The reply service is temporarily unavailable. Your verified artifact is still available.",
        )
    if intent == "coverage":
        current = next(row.year for row in manifest.inputs if row.role == "current")
        years = sorted(row.year for row in manifest.inputs if row.role == "baseline")
        text = (
            f"The artifact compares one current input from {current} with "
            f"{len(years)} baseline input years between {years[0]} and {years[-1]}. "
            "The manifest lists the exact inputs. This is historical coverage, not a forecast."
        )
    elif intent == "method":
        text = (
            "The cited manifest identifies the approved method and pins its version, "
            "code digest, quality policy and input snapshots. Open the citation for the exact identifiers."
        )
    elif intent == "limitations":
        text = (
            "The result applies to the inputs and method pinned in the citation. "
            "It does not establish current ocean conditions, a causal explanation, or safe navigation."
        )
    else:
        return Reply(
            status="abstained",
            reason="question_unsupported",
            text="I can explain the cited artifact's method, input years, or limitations. "
            "This artifact does not let me answer that question here.",
        )
    artifact = body.artifact_id
    return Reply(
        status="answered",
        text=text,
        citation=Citation(
            artifact_id=artifact,
            manifest_digest=manifest.manifest_digest,
            href=f"/v1/research/artifacts/{artifact}/manifest",
        ),
    )
