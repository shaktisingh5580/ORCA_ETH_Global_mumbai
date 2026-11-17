"""Purpose: expose P07 frozen benchmark metadata without disclosing held-out answer values.
Behavior: model only test-set identity, reference policy and synthetic disclosure.
Refs: main_implementation plan.md P07.4; api design.md §§3.8, 4.5; Phase: P07.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from orca.contracts.common import Contract, Digest, UTCDateTime


class BenchmarkDefinitionView(Contract):
    id: UUID
    project_id: UUID
    title: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=1, max_length=80)
    method_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=80)
    frozen_test_set_digest: Digest
    reference_policy_digest: Digest
    reference_kind: Literal["independent_observation", "synthetic_fixture"]
    synthetic: bool
    created_at: UTCDateTime
