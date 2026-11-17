"""Purpose: define bounded P08 operational status responses.
Behavior: expose tenant queue counts without worker secrets or unrecorded metrics.
Refs: main_implementation plan.md P08.1; api design.md §§3.9, 14; Phase: P08.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from orca.contracts.common import Contract, Money, UTCDateTime


class JobHealthSummary(Contract):
    organization_id: UUID
    queue: Literal["research_run"]
    observed_at: UTCDateTime
    since: UTCDateTime
    queued: int
    leased: int
    overdue_leases: int
    completed: int
    cancelled: int
    expired_attempts_since: int
    retry_attempts_since: int
    fencing_failures: None = None
    worker_status: Literal["unknown"] = "unknown"

    @classmethod
    def from_counts(
        cls,
        organization_id: UUID,
        observed_at: datetime,
        since: datetime,
        queue_counts: dict[str, int],
        attempt_counts: dict[str, int],
    ) -> "JobHealthSummary":
        return cls(
            organization_id=organization_id,
            queue="research_run",
            observed_at=observed_at,
            since=since,
            queued=queue_counts["queued"],
            leased=queue_counts["leased"],
            overdue_leases=queue_counts["overdue_leases"],
            completed=queue_counts["completed"],
            cancelled=queue_counts["cancelled"],
            expired_attempts_since=attempt_counts["expired_attempts_since"],
            retry_attempts_since=attempt_counts["retry_attempts_since"],
        )


class SourceHealthView(Contract):
    organization_id: UUID
    provider_id: str
    source_product_id: str
    distribution_id: UUID
    catalog_status: Literal["candidate", "probe_passed", "validated"]
    probe_status: Literal["never_checked", "queued", "running", "succeeded", "failed"]
    last_probe_at: UTCDateTime | None
    error_code: Literal["SOURCE_PROBE_REJECTED", "SOURCE_UNAVAILABLE"] | None
    metadata_access_passed: bool
    science_validated: Literal[False] = False


class UsageSummary(Contract):
    organization_id: UUID
    window: Literal["24h", "7d", "30d"]
    window_start: UTCDateTime
    observed_at: UTCDateTime
    run_reservations_created: int
    active_reservations_created: int
    reserved_budget_ceiling_usd: Money
    released_budget_ceiling_usd: Money
    settled_budget_ceiling_usd: Money
    actual_spend_usd: None = None
    model_tokens: None = None
    source_bytes: None = None
    accounting_status: Literal["budget_reservations_only"] = "budget_reservations_only"


class AuditEvent(Contract):
    id: UUID
    organization_id: UUID
    created_at: UTCDateTime
    actor_subject: str
    event_type: Literal[
        "organization_member", "project_member", "file_download", "invitation", "run_quota_updated"
    ]
    resource_id: UUID
    project_id: UUID | None


class RunQuotaSet(Contract):
    organization_id: UUID
    max_runs_per_minute: int
    version: int
    source_quota_status: Literal["not_enforced"] = "not_enforced"
    model_quota_status: Literal["not_enforced"] = "not_enforced"
    remote_task_quota_status: Literal["not_enforced"] = "not_enforced"


class PatchRunQuotaSet(Contract):
    max_runs_per_minute: int = Field(ge=1, le=100)
