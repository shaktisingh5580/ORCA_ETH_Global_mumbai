"""Purpose: enforce P07 historical cutoff and archived-version rules.
Behavior: test future forecasts, late reanalysis, missing originals and timezone normalization.
Refs: main_implementation plan.md P07.3; Phase: P07.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from orca.science.temporal import HistoricalSnapshot, select_historical_snapshot

SOURCE = uuid4()
CUTOFF = datetime(2025, 1, 10, tzinfo=timezone.utc)


def snapshot(**changes: object) -> HistoricalSnapshot:
    values = {
        "id": uuid4(),
        "source_id": SOURCE,
        "object_version_id": "immutable-v1",
        "published_at": CUTOFF - timedelta(days=2),
        "retrieved_at": CUTOFF - timedelta(days=1),
        "valid_at": CUTOFF + timedelta(days=1),
        "issued_at": CUTOFF - timedelta(days=2),
        "archived_original": True,
    }
    values.update(changes)
    return HistoricalSnapshot(**values)  # type: ignore[arg-type]


def test_forecast_issued_after_asof_excluded() -> None:
    earlier = snapshot()
    future = snapshot(
        issued_at=CUTOFF + timedelta(hours=1),
        valid_at=CUTOFF + timedelta(days=2),
        published_at=CUTOFF + timedelta(hours=1),
        retrieved_at=CUTOFF + timedelta(hours=2),
    )
    assert select_historical_snapshot((earlier, future), source_id=SOURCE, as_of=CUTOFF) == earlier


def test_late_reanalysis_not_substituted() -> None:
    original = snapshot()
    reanalysis = snapshot(
        published_at=CUTOFF + timedelta(days=3),
        retrieved_at=CUTOFF + timedelta(days=4),
        issued_at=None,
        supersedes_id=original.id,
    )
    assert (
        select_historical_snapshot((original, reanalysis), source_id=SOURCE, as_of=CUTOFF)
        == original
    )


def test_no_archived_original_returns_inconclusive() -> None:
    assert (
        select_historical_snapshot(
            (snapshot(archived_original=False),), source_id=SOURCE, as_of=CUTOFF
        )
        is None
    )


def test_later_retrieval_cannot_be_backdated_as_archived_evidence() -> None:
    late = snapshot(retrieved_at=CUTOFF + timedelta(days=1))
    assert select_historical_snapshot((late,), source_id=SOURCE, as_of=CUTOFF) is None


def test_calendar_timezones() -> None:
    same_in_india = CUTOFF.astimezone(timezone(timedelta(hours=5, minutes=30)))
    item = snapshot()
    assert select_historical_snapshot((item,), source_id=SOURCE, as_of=same_in_india) == item
    with pytest.raises(ValueError, match="timezone"):
        select_historical_snapshot((item,), source_id=SOURCE, as_of=CUTOFF.replace(tzinfo=None))
