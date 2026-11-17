"""Purpose: package P07 immutable scientific reviews for portable offline inspection.
Behavior: bind evidence and linked review records; verify optional package-only Ed25519 signatures.
Refs: main_implementation plan.md P07.4; api design.md §§3.8, 4.5; Phase: P07.
"""

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.workspace_access import authorize_project
from orca.contracts.science_evidence import ScienceEvidenceManifest
from orca.contracts.scientific_reviews import (
    ChallengeResolutionView,
    ChallengeResponseView,
    ChallengeView,
    IndependentReviewView,
)
from orca.domain.identity import Principal
from orca.science.manifests import verify_manifest

_DOMAIN = b"orca.p07.portable-review-package.v1\0"


@dataclass(frozen=True, slots=True)
class ReviewExportPackage:
    document: bytes
    payload_digest: str
    signed: bool


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _strict_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate portable review key")
        result[key] = value
    return result


def _ordered(items: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in sorted(items, key=lambda item: str(item.id))]


def build_review_export(
    *,
    artifact_id: UUID,
    evidence: ScienceEvidenceManifest,
    reviews: tuple[IndependentReviewView, ...],
    challenges: tuple[ChallengeView, ...],
    responses: tuple[ChallengeResponseView, ...],
    resolutions: tuple[ChallengeResolutionView, ...],
    signing_key: Ed25519PrivateKey | None = None,
    signer_key_id: str | None = None,
) -> ReviewExportPackage:
    """Build a portable record from already authorized, database-verified inputs."""
    verify_manifest(evidence)
    if not reviews and not challenges:
        raise ValueError("review export requires at least one review or challenge")
    if any(item.artifact_id != artifact_id for item in reviews) or any(
        item.artifact_id != artifact_id for item in challenges
    ):
        raise ValueError("review record belongs to another artifact")
    challenge_ids = {item.id for item in challenges}
    if len(challenge_ids) != len(challenges) or len({item.id for item in reviews}) != len(reviews):
        raise ValueError("duplicate review or challenge")
    if any(item.challenge_id not in challenge_ids for item in responses) or any(
        item.challenge_id not in challenge_ids for item in resolutions
    ):
        raise ValueError("orphan challenge response or resolution")
    if len({item.id for item in responses}) != len(responses) or len(
        {item.id for item in resolutions}
    ) != len(resolutions):
        raise ValueError("duplicate response or resolution")
    if len({item.challenge_id for item in resolutions}) != len(resolutions):
        raise ValueError("multiple resolutions for one challenge")
    if (signing_key is None) != (signer_key_id is None):
        raise ValueError("package signing key and key ID must be supplied together")
    if signer_key_id is not None and not 0 < len(signer_key_id) <= 128:
        raise ValueError("invalid package signer key ID")
    payload: dict[str, Any] = {
        "schema": "orca.portable-review.v1",
        "artifact_id": str(artifact_id),
        "evidence_manifest_id": str(evidence.id),
        "evidence_manifest_digest": evidence.manifest_digest,
        "origin_author": evidence.created_by,
        "result_digest": evidence.result_digest,
        "result_status": evidence.result_status,
        "source_mode": evidence.source_mode,
        "method_id": evidence.method_id,
        "method_version": evidence.method_version,
        "method_code_digest": evidence.method_code_digest,
        "reviews": _ordered(reviews),
        "challenges": _ordered(challenges),
        "responses": _ordered(responses),
        "resolutions": _ordered(resolutions),
        "independent_institution_verified": False,
        "signature_scope": "package_integrity_only",
        "limitations": [
            "Linked correction and arbitration artifacts are references; their bytes are not included.",
            "A package signature does not establish reviewer authorship or institutional independence.",
        ],
    }
    digest = "sha256:" + hashlib.sha256(rfc8785.dumps(payload)).hexdigest()
    signature = (
        _b64(signing_key.sign(_DOMAIN + digest.encode("ascii")))
        if signing_key is not None
        else None
    )
    envelope = {
        "payload": payload,
        "payload_digest": digest,
        "signer_key_id": signer_key_id,
        "signature": signature,
    }
    return ReviewExportPackage(rfc8785.dumps(envelope), digest, signature is not None)


def verify_review_export(
    document: bytes,
    *,
    public_key: Ed25519PublicKey | None,
    expected_key_id: str | None,
    expected_artifact_id: UUID,
    expected_manifest_digest: str,
) -> dict[str, Any]:
    """Verify payload bytes and, when present, a trusted out-of-band package signing key."""
    try:
        envelope = json.loads(
            document,
            object_pairs_hook=_strict_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON number")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid portable review JSON") from error
    if not isinstance(envelope, dict) or set(envelope) != {
        "payload",
        "payload_digest",
        "signer_key_id",
        "signature",
    }:
        raise ValueError("invalid portable review envelope")
    payload = envelope["payload"]
    if not isinstance(payload, dict) or payload.get("schema") != "orca.portable-review.v1":
        raise ValueError("invalid portable review payload")
    if (
        payload.get("artifact_id") != str(expected_artifact_id)
        or payload.get("evidence_manifest_digest") != expected_manifest_digest
    ):
        raise ValueError("portable review context mismatch")
    digest = "sha256:" + hashlib.sha256(rfc8785.dumps(payload)).hexdigest()
    if envelope["payload_digest"] != digest:
        raise ValueError("portable review digest mismatch")
    signature = envelope["signature"]
    if signature is None:
        if (
            envelope["signer_key_id"] is not None
            or public_key is not None
            or expected_key_id is not None
        ):
            raise ValueError("unsigned export cannot satisfy a signature requirement")
        return payload
    if (
        not isinstance(signature, str)
        or not isinstance(envelope["signer_key_id"], str)
        or public_key is None
        or expected_key_id is None
        or envelope["signer_key_id"] != expected_key_id
    ):
        raise ValueError("trusted package signing key required")
    try:
        public_key.verify(_unb64(signature), _DOMAIN + digest.encode("ascii"))
    except (InvalidSignature, ValueError) as error:
        raise ValueError("invalid package signature") from error
    return payload


def assemble_review_export(
    session: Session,
    tenant: UUID,
    artifact_id: UUID,
    principal: Principal,
    *,
    signing_key: Ed25519PrivateKey | None = None,
    signer_key_id: str | None = None,
) -> ReviewExportPackage:
    """Recheck exact artifact authority before loading immutable rows from a tenant session."""
    row = (
        session.execute(
            text("""SELECT a.project_id,a.evidence_manifest_id,a.scientific_status,
          m.manifest,m.manifest_digest FROM research.run_artifacts a
          JOIN research.science_evidence_manifests m
            ON m.tenant_id=a.tenant_id AND m.project_id=a.project_id
              AND m.id=a.evidence_manifest_id
          WHERE a.tenant_id=:tenant AND a.id=:artifact"""),
            {"tenant": tenant, "artifact": artifact_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise LookupError("review artifact not found")
    try:
        authorize_project(session, tenant, row["project_id"], principal, "evidence:read")
    except (LookupError, PermissionError):
        if "evidence:read" not in principal.token_scopes:
            raise PermissionError("evidence read scope required") from None
        grant = session.execute(
            text("""SELECT 1 FROM research.artifact_review_grants
              WHERE tenant_id=:tenant AND project_id=:project AND artifact_id=:artifact
                AND reviewer_subject=:subject AND revoked_at IS NULL
                AND expires_at>clock_timestamp() FOR SHARE"""),
            {
                "tenant": tenant,
                "project": row["project_id"],
                "artifact": artifact_id,
                "subject": principal.subject,
            },
        ).scalar_one_or_none()
        if grant is None:
            raise LookupError("review artifact not found") from None
    evidence = ScienceEvidenceManifest.model_validate(row["manifest"])
    verify_manifest(evidence)
    if (
        evidence.tenant_id != tenant
        or evidence.project_id != row["project_id"]
        or evidence.id != row["evidence_manifest_id"]
        or evidence.manifest_digest != row["manifest_digest"]
        or evidence.result_status != row["scientific_status"]
    ):
        raise ValueError("review artifact evidence lineage mismatch")
    params = {"tenant": tenant, "project": row["project_id"], "artifact": artifact_id}

    def rows(query: str) -> list[dict[str, Any]]:
        result = session.execute(text(query), params).mappings().all()
        if len(result) > 1000:
            raise ValueError("portable review exceeds bounded record count")
        return [dict(item) for item in result]

    reviews = tuple(
        IndependentReviewView.model_validate(item)
        for item in rows("""SELECT id,artifact_id,reviewer_subject,verdict,statement,
          reproduction_digest,independence_verified,created_at
          FROM research.independent_reviews
          WHERE tenant_id=:tenant AND project_id=:project AND artifact_id=:artifact
          ORDER BY id LIMIT 1001""")
    )
    challenge_rows = rows("""SELECT c.id,c.artifact_id,c.reviewer_subject,c.discrepancy_type,
      c.source_refs,c.statement,c.created_at,
      (SELECT count(*) FROM research.challenge_responses r
        WHERE r.tenant_id=c.tenant_id AND r.project_id=c.project_id AND r.challenge_id=c.id)
        AS response_count,
      (SELECT z.verdict FROM research.challenge_resolutions z
        WHERE z.tenant_id=c.tenant_id AND z.project_id=c.project_id AND z.challenge_id=c.id)
        AS resolution_verdict
      FROM research.scientific_challenges c
      WHERE c.tenant_id=:tenant AND c.project_id=:project AND c.artifact_id=:artifact
      ORDER BY c.id LIMIT 1001""")

    def challenge_view(item: dict[str, Any]) -> ChallengeView:
        verdict = item.pop("resolution_verdict")
        status = (
            "inconclusive"
            if verdict == "inconclusive"
            else "resolved"
            if verdict is not None
            else "responded"
            if item["response_count"]
            else "open"
        )
        return ChallengeView.model_validate({**item, "status": status})

    challenges = tuple(challenge_view(item) for item in challenge_rows)
    responses = tuple(
        ChallengeResponseView.model_validate(item)
        for item in rows("""SELECT r.id,r.challenge_id,r.responder_subject,
          r.response_artifact_id,r.statement,r.created_at
          FROM research.challenge_responses r JOIN research.scientific_challenges c
            ON c.tenant_id=r.tenant_id AND c.project_id=r.project_id AND c.id=r.challenge_id
          WHERE c.tenant_id=:tenant AND c.project_id=:project AND c.artifact_id=:artifact
          ORDER BY r.id LIMIT 1001""")
    )
    resolutions = tuple(
        ChallengeResolutionView.model_validate(item)
        for item in rows("""SELECT z.id,z.challenge_id,z.resolver_subject,z.verdict,
          z.arbitration_refs,z.limitations,z.independence_verified,z.created_at
          FROM research.challenge_resolutions z JOIN research.scientific_challenges c
            ON c.tenant_id=z.tenant_id AND c.project_id=z.project_id AND c.id=z.challenge_id
          WHERE c.tenant_id=:tenant AND c.project_id=:project AND c.artifact_id=:artifact
          ORDER BY z.id LIMIT 1001""")
    )
    return build_review_export(
        artifact_id=artifact_id,
        evidence=evidence,
        reviews=reviews,
        challenges=challenges,
        responses=responses,
        resolutions=resolutions,
        signing_key=signing_key,
        signer_key_id=signer_key_id,
    )
