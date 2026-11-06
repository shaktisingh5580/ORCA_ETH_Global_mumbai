"""Purpose: launch the Render hackathon image with isolated intentional-disagreement tooling.
Behavior: add synthetic-profile provider processes without modifying real research data.
Refs: H05 cloud/disagreement decision; Phase: H05; not a production wheel entrypoint.
"""

import os
import sys
from pathlib import Path

from disagreement_data import prepare

from orca.cloud_runtime import main


def extra(bundle, state, api_env):
    root = state / "demonstration"
    os.environ["ORCA_DEMO_STATE_DIR"] = str(root)
    commands = prepare(bundle, state)
    commands.append(
        (
            "intentional-disagreement-worker",
            [
                sys.executable,
                str(Path(__file__).with_name("disagreement_worker.py")),
                "--env-file",
                str(api_env),
                "--state",
                str(root),
            ],
        )
    )
    return commands


if __name__ == "__main__":
    try:
        main(
            extra_setup=extra,
            api_factory="demo_bootstrap:create_application",
            app_dir=str(Path(__file__).parent),
        )
    except Exception as error:
        raise SystemExit(
            "Hackathon cloud startup failed ("
            + type(error).__name__
            + "); no credentials are printed"
        ) from None
