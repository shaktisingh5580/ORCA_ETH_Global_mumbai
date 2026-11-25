"""Purpose: manage user-visible companion preferences and consented location memory.
Behavior: serialize versioned writes and erase coordinates atomically on consent withdrawal.
Refs: main implementation plan P12.1; A-API §3.13; W-MEM6 §§24–25,41.
"""

from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import text

from orca.application.companion import ConversationChanged, Conversations
from orca.contracts.common import Contract

Units = Literal["metric", "nautical"]
COLUMNS = "units,location_consent,location_id,latitude,longitude,version"


class Location(Contract):
    latitude: Annotated[float, Field(ge=-90, le=90, strict=True)]
    longitude: Annotated[float, Field(ge=-180, le=180, strict=True)]


class SavedLocation(Location):
    id: UUID
    provenance: Literal["private_memory"] = "private_memory"
    usable_as_live_observation: Literal[False] = False


class Preferences(Contract):
    units: Units = "metric"
    location_consent: bool = False
    location: SavedLocation | None = None
    version: int = 1


class MemoryPage(Contract):
    items: list[SavedLocation]
    next_cursor: None = None


class PatchPreferences(Contract):
    units: Units | None = None
    location_consent: Annotated[bool, Field(strict=True)] | None = None
    location: Location | None = None

    @model_validator(mode="after")
    def require_change(self) -> "PatchPreferences":
        if self.units is None and self.location_consent is None and self.location is None:
            raise ValueError("Supply a preference, consent decision, or location")
        if self.location_consent is False and self.location is not None:
            raise ValueError("Cannot save a location while withdrawing consent")
        return self


class ConsentRequired(Exception):
    pass


class PreferenceChanged(ConversationChanged):
    pass


class PreferencesRepository:
    def __init__(self, repo: Conversations) -> None:
        self.session = repo.session
        self.identity = {
            "tenant": repo.tenant,
            "subject": repo.principal.subject,
            "issuer": repo.principal.issuer,
        }

    def get(self, *, locked: bool = False) -> Preferences:
        if locked:
            self.session.execute(
                text("""INSERT INTO research.companion_preferences
                (tenant_id,owner_subject,owner_issuer) VALUES (:tenant,:subject,:issuer)
                ON CONFLICT (tenant_id,owner_subject,owner_issuer) DO NOTHING"""),
                self.identity,
            )
        suffix = " FOR UPDATE" if locked else ""
        row = (
            self.session.execute(
                text(f"""SELECT {COLUMNS} FROM research.companion_preferences
          WHERE tenant_id=:tenant AND owner_subject=:subject AND owner_issuer=:issuer{suffix}"""),
                self.identity,
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return Preferences()
        location = None
        if row["location_id"] is not None:
            location = SavedLocation(
                id=row["location_id"], latitude=row["latitude"], longitude=row["longitude"]
            )
        return Preferences(
            units=row["units"],
            location_consent=row["location_consent"],
            location=location,
            version=row["version"],
        )

    def _current(self, etag: str) -> Preferences:
        current = self.get(locked=True)
        if etag != f'"{current.version}"':
            raise PreferenceChanged
        return current

    def _write(
        self, current: Preferences, units: Units, consent: bool, location: SavedLocation | None
    ) -> Preferences:
        if (units, consent, location) == (
            current.units,
            current.location_consent,
            current.location,
        ):
            return current
        version = current.version + 1
        self.session.execute(
            text("""UPDATE research.companion_preferences
          SET units=:units,location_consent=:consent,location_id=:location_id,
              latitude=:latitude,longitude=:longitude,version=:version
          WHERE tenant_id=:tenant AND owner_subject=:subject AND owner_issuer=:issuer"""),
            {
                **self.identity,
                "units": units,
                "consent": consent,
                "version": version,
                "location_id": location.id if location else None,
                "latitude": location.latitude if location else None,
                "longitude": location.longitude if location else None,
            },
        )
        return Preferences(
            units=units, location_consent=consent, location=location, version=version
        )

    def patch(self, body: PatchPreferences, etag: str) -> Preferences:
        current = self._current(etag)
        consent = (
            current.location_consent if body.location_consent is None else body.location_consent
        )
        location = current.location if consent else None
        if body.location is not None:
            if not consent:
                raise ConsentRequired
            location = SavedLocation(id=uuid4(), **body.location.model_dump())
        return self._write(current, body.units or current.units, consent, location)

    def forget(self, identifier: UUID, etag: str) -> Preferences:
        current = self._current(etag)
        if current.location is None or identifier != current.location.id:
            raise LookupError("Memory not found")
        return self._write(current, current.units, current.location_consent, None)
