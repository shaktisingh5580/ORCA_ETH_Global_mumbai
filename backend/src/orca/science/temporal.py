"""Purpose: enforce P04 calendar coverage and P07 archived-input historical cutoffs.
Behavior: preserve seasonal day alignment while excluding future issuance and later reanalysis.
Refs: main_implementation plan.md P04.1/P04.2, P07.3; api design.md §§3.8, 7; Phase: P04/P07.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import UUID


@dataclass(frozen=True, slots=True)
class HistoricalSnapshot:
    id: UUID
    source_id: UUID
    object_version_id: str
    published_at: datetime
    retrieved_at: datetime
    valid_at: datetime
    issued_at: datetime | None
    archived_original: bool
    supersedes_id: UUID | None = None
    available_at: datetime | None = None
    archived_at: datetime | None = None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("historical timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def select_historical_snapshot(
    snapshots: tuple[HistoricalSnapshot, ...], *, source_id: UUID, as_of: datetime
) -> HistoricalSnapshot | None:
    """Use publication proof, never a newly retrieved replacement as historical evidence."""
    cutoff = _utc(as_of)
    eligible: list[HistoricalSnapshot] = []
    for item in snapshots:
        published, retrieved, valid = (
            _utc(item.published_at),
            _utc(item.retrieved_at),
            _utc(item.valid_at),
        )
        available = _utc(item.available_at) if item.available_at is not None else None
        archived = _utc(item.archived_at) if item.archived_at is not None else None
        issued = _utc(item.issued_at) if item.issued_at is not None else None
        if not item.object_version_id or retrieved < published:
            raise ValueError("snapshot requires a pinned version and ordered source times")
        if issued is not None and issued > valid:
            raise ValueError("forecast issuance cannot follow its valid time")
        if item.source_id != source_id:
            continue
        if available is None or archived is None:
            continue
        if not item.archived_original or item.supersedes_id is not None:
            continue
        if (
            published > cutoff
            or available > cutoff
            or retrieved > cutoff
            or archived > cutoff
            or (issued is not None and issued > cutoff)
        ):
            continue
        eligible.append(item)
    if not eligible:
        return None
    return max(eligible, key=lambda item: (_utc(item.published_at), str(item.id)))


def seasonal_day_key(value: date) -> tuple[int, int]:
    return value.month, value.day


def require_baseline_calendar_coverage(
    current_dates: tuple[date, ...], baseline_dates: tuple[date, ...]
) -> None:
    if not current_dates or not baseline_dates:
        raise ValueError("current and baseline dates are required")
    baseline_keys = {seasonal_day_key(value) for value in baseline_dates}
    missing = sorted({seasonal_day_key(value) for value in current_dates} - baseline_keys)
    if missing:
        formatted = ", ".join(f"{month:02d}-{day:02d}" for month, day in missing)
        raise ValueError(f"baseline does not cover current Gregorian calendar days: {formatted}")
