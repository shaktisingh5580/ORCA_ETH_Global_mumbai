"""Purpose: calculate spherical area weights for bounded EPSG:4326 rectangles.
Behavior: splits antimeridian-crossing bounds and applies the documented spherical Earth approximation.
Refs: main_implementation plan.md P04.2; api design.md §7; docs/07_scientific_engine.md; Phase: P04.
"""

import math

EARTH_MEAN_RADIUS_M = 6_371_008.8


def longitude_intervals(west: float, east: float) -> tuple[tuple[float, float], ...]:
    if not (-180 <= west <= 180 and -180 <= east <= 180) or west == east:
        raise ValueError("longitude bounds must be distinct EPSG:4326 values")
    if west < east:
        return ((west, east),)
    return ((west, 180.0), (-180.0, east))


def spherical_rectangle_area_m2(west: float, south: float, east: float, north: float) -> float:
    if not (-90 <= south < north <= 90):
        raise ValueError("latitude bounds must be ordered EPSG:4326 values")
    longitude_width = math.fsum(
        math.radians(end - start) for start, end in longitude_intervals(west, east)
    )
    area = (
        EARTH_MEAN_RADIUS_M**2
        * longitude_width
        * (math.sin(math.radians(north)) - math.sin(math.radians(south)))
    )
    if not math.isfinite(area) or area <= 0:
        raise ValueError("rectangle must have positive finite spherical area")
    return area
