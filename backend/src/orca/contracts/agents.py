"""Purpose: define institution-owned agent registration and proof contracts.
Behavior: accept public service metadata only, bind proofs to exact immutable revisions.
Refs: ORCA_INSTITUTIONAL_API_AND_DATA_DESIGN.md sections 2–3; Phase: F02.
"""

import base64
from typing import Literal

from pydantic import Field, field_validator

from orca.contracts.common import Contract, Digest


class AgentDraft(Contract):
    name: str = Field(min_length=1, max_length=100)
    endpoint: str = Field(max_length=500)
    public_key: str = Field(min_length=43, max_length=44)
    key_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    card_digest: Digest
    capability: Literal["temperature_summary_v1"] = "temperature_summary_v1"
    operator_mode: Literal["self_declared_external", "team_controlled"]
    disclosure_policy: Literal["task_inputs_only"] = "task_inputs_only"
    discoverable: bool = False

    @field_validator("public_key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        if (
            len(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
            != 32
        ):
            raise ValueError("Ed25519 public key required")
        return value


class AgentVersion(Contract):
    expected_version: int = Field(ge=1)


class AgentProof(AgentVersion):
    challenge_id: str = Field(max_length=36)
    signature: str = Field(min_length=86, max_length=88)


class AgentRevision(AgentVersion):
    registration: AgentDraft
