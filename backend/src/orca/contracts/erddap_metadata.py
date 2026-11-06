"""Purpose: interpret provider metadata without guessing scientific dimensions or units.
Behavior: validate ERDDAP table shape and retain native attributes for explicit mapping.
Refs: API 4.2; main plan P03.2; research-only guide 5; Phase: P03.2.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Variable:
    name: str
    dimensions: tuple[str, ...]
    native_units: str | None
    standard_name: str | None


def parse_metadata(document: object) -> tuple[Variable, ...]:
    if not isinstance(document, dict) or not isinstance(document.get("table"), dict):
        raise ValueError("missing ERDDAP table")
    table = document["table"]
    expected = ["Row Type", "Variable Name", "Attribute Name", "Data Type", "Value"]
    if table.get("columnNames") != expected or not isinstance(table.get("rows"), list):
        raise ValueError("unexpected ERDDAP columns")
    rows = table["rows"]
    attributes: dict[tuple[str, str], str] = {}
    variables: dict[str, tuple[str, ...]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 5 or not all(isinstance(x, str) for x in row):
            raise ValueError("malformed ERDDAP row")
        kind, name, attribute, _, value = row
        if kind == "attribute":
            key = (name, attribute)
            if key in attributes:
                raise ValueError("duplicate metadata attribute")
            attributes[key] = value
        elif kind == "variable":
            if name in variables:
                raise ValueError("duplicate variable")
            variables[name] = tuple(x.strip() for x in value.split(",") if x.strip())
    return tuple(
        Variable(
            name,
            dimensions,
            attributes.get((name, "units")),
            attributes.get((name, "standard_name")),
        )
        for name, dimensions in variables.items()
    )
