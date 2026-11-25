"""Purpose: expose a private, evidence-grounded companion reply.
Behavior: authorize within the conversation's tenant before and after the one optional model call.
Refs: main implementation plan P12.2; A-API §§3.13,17.
"""

from functools import partial
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import SecretStr

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.companion import service
from orca.api.routers.evidence import _artifact_row, _authorize, _verified
from orca.application.companion_reply import Reply, ReplyRequest, answer, unavailable_evidence
from orca.contracts.science_evidence import ScienceEvidenceManifest
from orca.domain.identity import Principal
from orca.infrastructure.gemini_companion import classify

router = APIRouter(prefix="/v1/conversations", tags=["companion"])


def authorized_manifest(
    request: Request,
    principal: Principal,
    organization_id: UUID,
    conversation_id: UUID,
    artifact_id: UUID | None,
) -> ScienceEvidenceManifest | None:
    with service(request, principal, organization_id) as conversations:
        if conversations.get(conversation_id).archived:
            raise HTTPException(409, "Conversation is archived")
        if artifact_id is None:
            return None
        try:
            row = _artifact_row(conversations.session, conversations.tenant, artifact_id)
            _authorize(conversations.session, conversations.tenant, row["project_id"], principal)
            manifest, _ = _verified(row)
        except HTTPException as exc:
            if exc.status_code in (403, 404, 409):
                return None
            raise
        except ValueError:
            return None
        if manifest.tenant_id != conversations.tenant or manifest.project_id != row["project_id"]:
            return None
        return manifest


@router.post("/{conversation_id}/reply")
def reply(
    conversation_id: UUID,
    body: ReplyRequest,
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
) -> Reply:
    """Classify only the explicitly shared question; leave private research data on ORCA."""
    manifest = authorized_manifest(
        request, principal, organization_id, conversation_id, body.artifact_id
    )
    key = getattr(request.app.state, "companion_model_key", None)
    model = getattr(request.app.state, "companion_model_name", None)
    select = (
        partial(classify, key.get_secret_value(), model)
        if (isinstance(key, SecretStr) and isinstance(model, str))
        else None
    )
    result = answer(body, manifest, select)
    if result.status == "answered":
        current = authorized_manifest(
            request, principal, organization_id, conversation_id, body.artifact_id
        )
        if (
            manifest is None
            or current is None
            or current.manifest_digest != manifest.manifest_digest
        ):
            return unavailable_evidence()
    return result
