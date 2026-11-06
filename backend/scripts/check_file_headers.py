"""Purpose: enforce explanatory source headers without corrupting generated formats.
Behavior: check all authored Python modules for purpose, behavior, refs and phase.
Refs: main plan section 5.1; Phase: P01.
"""

import ast
from pathlib import Path


def violations(root: Path) -> list[str]:
    errors = []
    for folder in ("src", "tests", "scripts"):
        for path in (root / folder).rglob("*.py"):
            header = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
            if not all(key in header for key in ("Purpose:", "Behavior:", "Refs:", "Phase:")):
                errors.append(str(path.relative_to(root)))
    return errors


if __name__ == "__main__":
    found = violations(Path(__file__).resolve().parents[1])
    print("\n".join(found) if found else "Purpose headers passed")
    raise SystemExit(bool(found))
