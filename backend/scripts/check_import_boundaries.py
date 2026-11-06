"""Purpose: keep test data and transport/framework details out of protected modules.
Behavior: inspect Python imports and literal dynamic imports; fail on boundary violations.
Refs: main plan section 4.1 and P01.4; Phase: P01.
"""

import ast
from pathlib import Path


def violations(root: Path) -> list[str]:
    errors = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        protected = bool({"domain", "science"}.intersection(path.parts))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""] + [item.name for item in node.names]
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name in {"import_module", "__import__"}:
                    if not node.args or not isinstance(node.args[0], ast.Constant):
                        errors.append(f"{path.name}: nonliteral dynamic import")
                    else:
                        modules = [str(node.args[0].value)]
            for module in modules:
                parts = set(module.split("."))
                if parts & {"tests", "fixtures", "support", "mock_server"}:
                    errors.append(f"{path.name}: forbidden import {module}")
                if protected and parts & {"fastapi", "httpx", "sqlalchemy", "infrastructure"}:
                    errors.append(f"{path.name}: protected layer import {module}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(
                    p in node.value.replace("\\", "/").lower()
                    for p in ("fixtures/", "tests/support", "use_dummy_data")
                ):
                    errors.append(f"{path.name}: runtime fixture path or dummy flag")
    return sorted(set(errors))


if __name__ == "__main__":
    found = violations(Path(__file__).resolve().parents[1] / "src")
    print("\n".join(found) if found else "Import boundaries passed")
    raise SystemExit(bool(found))
