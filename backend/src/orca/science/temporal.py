"""Purpose: enforce explicit Gregorian temporal alignment for seasonal SST comparisons.
Behavior: uses month/day keys and rejects a current date absent from the baseline calendar.
Refs: main_implementation plan.md P04.1/P04.2; docs/07_scientific_engine.md; ADR-R02; Phase: P04.
"""

from datetime import date


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
