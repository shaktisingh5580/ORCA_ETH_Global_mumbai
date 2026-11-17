"""Purpose: define bounded P08 operational status responses.
Behavior: expose tenant queue counts without worker secrets or unrecorded metrics.
Refs: main_implementation plan.md P08.1; api design.md §§3.9, 14; Phase: P08.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from orca.contracts.common import Contract, UTCDateTime


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
