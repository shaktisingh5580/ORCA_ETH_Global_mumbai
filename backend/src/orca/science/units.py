"""Purpose: convert supported absolute temperature units to canonical Celsius.
Behavior: accepts only explicit Celsius/Kelvin spellings and rejects non-finite values.
Refs: main_implementation plan.md P04.1/P04.2; docs/07_scientific_engine.md; Phase: P04.
"""

import math

_CELSIUS_UNITS = {"degree_C", "degree_Celsius", "degrees_Celsius", "degC", "°C"}
_KELVIN_UNITS = {"K", "kelvin"}


def to_celsius(value: float, unit: str) -> float:
    if not math.isfinite(value):
        raise ValueError("temperature must be finite")
    if unit in _CELSIUS_UNITS:
        return value
    if unit in _KELVIN_UNITS:
        return value - 273.15
    raise ValueError("temperature unit is not an approved Celsius/Kelvin spelling")


def is_temperature_unit(unit: str) -> bool:
    return unit in _CELSIUS_UNITS or unit in _KELVIN_UNITS
