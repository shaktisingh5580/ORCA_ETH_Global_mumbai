"""Purpose: verify private companion memory selection and freshness.
Behavior: reject cross-scope, stale, ambiguous and unconsented context.
Refs: main implementation plan P12.1; A-API §3.13; W-MEM6 §§4.1,11.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from orca.domain.companion.memory import MemoryItem, Plane, Scope, context_packet

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def item(plane: Plane = Plane.USER, *, scope: Scope | None = None) -> MemoryItem:
    return MemoryItem(
        uuid4(),
        scope or Scope(uuid4(), "researcher", uuid4()),
        plane,
        "Historical evidence" if plane == Plane.EVIDENCE_REFERENCE else "preferred units",
        NOW,
        NOW + timedelta(hours=1),
        reference=uuid4() if plane == Plane.EVIDENCE_REFERENCE else None,
    )


def test_user_preferences_not_marine_truth() -> None:
    owned = item()
    records = [item(plane, scope=owned.scope) for plane in Plane]
    packet = context_packet(owned.scope, records, now=NOW)
    assert {entry.item.plane for entry in packet} == set(Plane)
    assert not any(entry.usable_as_live_observation for entry in packet)
    assert (
        next(entry.provenance for entry in packet if entry.item.reference) == "historical_reference"
    )
    with pytest.raises(ValueError, match="reference"):
        replace(records[list(Plane).index(Plane.EVIDENCE_REFERENCE)], reference=None)


def test_cross_tenant_memory_no_leak() -> None:
    owned = item()
    scope = owned.scope
    outsiders = [
        item(scope=replace(scope, tenant=uuid4())),
        item(scope=replace(scope, subject="another")),
        item(scope=replace(scope, session=uuid4())),
    ]
    assert [entry.item for entry in context_packet(scope, [*outsiders, owned], now=NOW)] == [owned]


def test_memory_supersede_revision() -> None:
    original = item()
    updated = original.supersede(expected_revision=1, label="updated units", now=NOW)
    with pytest.raises(ValueError, match="stale"):
        updated.supersede(expected_revision=1, label="old units", now=NOW)
    assert context_packet(original.scope, [updated, original], now=NOW)[0].item == updated
    assert not context_packet(
        original.scope, [updated, replace(updated, label="conflict")], now=NOW
    )
    assert not context_packet(
        original.scope,
        [original, replace(updated, expires_at=NOW + timedelta(minutes=1))],
        now=NOW + timedelta(minutes=1),
    )


def test_saved_location_consent() -> None:
    saved = replace(item(), contains_saved_location=True, label="Saved location")
    assert context_packet(saved.scope, [saved], now=NOW, saved_location_consent=True)
    assert not context_packet(saved.scope, [saved], now=NOW)


def test_working_memory_checkpoint() -> None:
    checkpoint = item(Plane.WORKING)
    assert context_packet(checkpoint.scope, [checkpoint], now=NOW)[0].item == checkpoint
    assert not context_packet(checkpoint.scope, [checkpoint], now=checkpoint.expires_at)
    with pytest.raises(ValueError, match="session"):
        replace(checkpoint, scope=replace(checkpoint.scope, session=None))
    with pytest.raises(ValueError, match="24 hours"):
        replace(checkpoint, expires_at=NOW + timedelta(days=2))
