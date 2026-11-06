"""Purpose: publish the opt-in research demo contract separately from the gated core profile.
Behavior: export route schemas without constructing cloud clients or embedding runtime settings.
Refs: accelerated-research-arkiv-demo.json; Phase: accelerated P04/P06/P11 demo.
"""

import json
from pathlib import Path

from fastapi import FastAPI

from orca.api.routers.demo import router


def main() -> None:
    app = FastAPI(title="ORCA accelerated descriptive research demo", version="0.1.0")
    app.include_router(router)
    destination = Path(__file__).resolve().parents[2] / "contracts/openapi/orca-demo.json"
    destination.write_text(
        json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    main()
