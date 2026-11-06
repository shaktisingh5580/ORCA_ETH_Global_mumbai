"""Purpose: mount the isolated synthetic-data demonstration API in a labelled hackathon image.
Behavior: retain real Supabase authentication/project checks; keep fixture code out of production releases.
Refs: H05 decision; Phase: H05; demonstration tooling only.
"""

import os
from pathlib import Path

from disagreement_demo import router

from orca.api.app import create_app
from orca.config import Settings


def create_application():
    app = create_app(Settings())
    root = Path(os.environ["ORCA_DEMO_STATE_DIR"])
    app.state.disagreement_state = str(root)
    app.include_router(router)
    return app
