"""Purpose: bound the ORCA companion context by identity, provenance and time.
Behavior: select only current private memory revisions and never promote them to observations.
Refs: W-MEM6 §§4.1,8,11,17; A-API §3.13; main implementation plan P12.1.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID


class Plane(StrEnum):
    WORKING = "working"
    CONVERSATION = "conversation"
    USER = "user"
    INVESTIGATION = "investigation"
    EVIDENCE_REFERENCE = "evidence_reference"
    SCENARIO = "scenario"
    RELATIONSHIP = "relationship"


@dataclass(frozen=True, slots=True)
class Scope:
    tenant: UUID
    subject: str
    session: UUID | None = None


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: UUID
    scope: Scope
    plane: Plane
    label: str
    updated_at: datetime
    expires_at: datetime
    revision: int = 1
    reference: UUID | None = None
    contains_saved_location: bool = False

    def __post_init__(self) -> None:
        if any(time.utcoffset() is None for time in (self.updated_at, self.expires_at)):
            raise ValueError("Memory timestamps require a time zone")
        if self.revision < 1 or not self.updated_at < self.expires_at:
            raise ValueError("Memory revision or lifetime is invalid")
        if not self.scope.subject.strip() or not self.label.strip() or len(self.label) > 512:
            raise ValueError("Memory labels and owner are required and labels are bounded")
        if self.plane in (Plane.WORKING, Plane.CONVERSATION) and self.scope.session is None:
            raise ValueError("Session memory requires a session")
        if self.plane == Plane.WORKING and self.expires_at - self.updated_at > timedelta(hours=24):
            raise ValueError("A working checkpoint cannot live longer than 24 hours")
        if self.plane == Plane.EVIDENCE_REFERENCE and (
            self.reference is None or self.label != "Historical evidence"
        ):
            raise ValueError(
                "Evidence memory requires a reference and the historical evidence label"
            )

    def supersede(self, *, expected_revision: int, label: str, now: datetime) -> "MemoryItem":
        if expected_revision != self.revision:
            raise ValueError("Memory revision is stale")
        if now.utcoffset() is None or not self.updated_at <= now < self.expires_at:
            raise ValueError("Memory is not eligible for revision")
        return replace(self, label=label, revision=self.revision + 1, updated_at=now)


@dataclass(frozen=True, slots=True)
class ContextItem:
    item: MemoryItem
    provenance: str
    usable_as_live_observation: bool = False


def context_packet(
    scope: Scope,
    items: list[MemoryItem],
    *,
    now: datetime,
    saved_location_consent: bool = False,
) -> tuple[ContextItem, ...]:
    """Select private references only. Authoritative evidence is retrieved separately."""
    if now.utcoffset() is None:
        raise ValueError("Context retrieval requires a time zone")
    latest: dict[UUID, MemoryItem] = {}
    conflicts: set[UUID] = set()
    for item in items:
        owner = item.scope
        if (
            owner.tenant != scope.tenant
            or owner.subject != scope.subject
            or (owner.session is not None and owner.session != scope.session)
        ):
            continue
        previous = latest.get(item.id)
        if previous is None or item.revision > previous.revision:
            latest[item.id] = item
            conflicts.discard(item.id)
        elif item.revision == previous.revision and item != previous:
            conflicts.add(item.id)
    visible = [
        item
        for item in latest.values()
        if item.id not in conflicts
        and item.updated_at <= now < item.expires_at
        and (saved_location_consent or not item.contains_saved_location)
    ]
    ordered = sorted(visible, key=lambda item: (item.updated_at, item.id), reverse=True)[:32]
    return tuple(
        ContextItem(
            item,
            "historical_reference" if item.plane == Plane.EVIDENCE_REFERENCE else "private_memory",
        )
        for item in ordered
    )
