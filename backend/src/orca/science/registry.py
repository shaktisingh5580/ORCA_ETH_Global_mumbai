"""Purpose: resolve immutable, explicitly approved scientific method versions.
Behavior: exact method/version/digest lookup fails closed; no method is approved by default.
Refs: main_implementation plan.md P04.1; api design.md §§3.5,4.3; ADR-R02; Phase: P04.
"""

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class MethodVersion:
    method_id: str
    version: str
    code_digest: str
    approval_status: str
    required_baseline_years: int
    measurement_semantics: str
    qc_policy_id: str
    qc_policy_digest: str
    calendar: str = "gregorian"

    def __post_init__(self) -> None:
        if not self.method_id or not self.version:
            raise ValueError("method identifier and version are required")
        if not _DIGEST.fullmatch(self.code_digest):
            raise ValueError("method code digest must be a lowercase SHA-256 hex digest")
        if self.approval_status not in {"draft", "approved", "retired"}:
            raise ValueError("method approval status is invalid")
        if self.required_baseline_years < 1:
            raise ValueError("at least one baseline year is required")
        if not self.measurement_semantics:
            raise ValueError("measurement semantics are required")
        if not self.qc_policy_id or not _DIGEST.fullmatch(self.qc_policy_digest):
            raise ValueError("pinned QC policy identifier and SHA-256 digest are required")


class MethodRegistry:
    """Small immutable-by-interface registry; approved entries come from reviewed config."""

    def __init__(self, methods: tuple[MethodVersion, ...] = ()) -> None:
        entries: dict[tuple[str, str], MethodVersion] = {}
        for method in methods:
            key = (method.method_id, method.version)
            if key in entries:
                raise ValueError("duplicate method version")
            entries[key] = method
        self._entries: Mapping[tuple[str, str], MethodVersion] = MappingProxyType(entries)

    def resolve(self, method_id: str, version: str, code_digest: str) -> MethodVersion:
        method = self._entries.get((method_id, version))
        if method is None:
            raise ValueError("scientific method version is not registered")
        if method.code_digest != code_digest:
            raise ValueError("scientific method code digest does not match the pinned version")
        if method.approval_status != "approved":
            raise ValueError("scientific method version is not approved")
        return method

    def approved_versions(self) -> tuple[MethodVersion, ...]:
        """Expose only versions accepted by the independent scientific review gate."""
        return tuple(
            sorted(
                (
                    method
                    for method in self._entries.values()
                    if method.approval_status == "approved"
                ),
                key=lambda method: (method.method_id, method.version),
            )
        )


# Intentionally empty until the method/tolerance decision and independent review are accepted.
METHOD_REGISTRY = MethodRegistry()
