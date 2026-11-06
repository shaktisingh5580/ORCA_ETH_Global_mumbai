"""Purpose: compose the validated P01 application at explicit startup.
Behavior: loads settings, never connects providers or injects mock adapters.
Refs: main_implementation plan.md P01.1; Phase: P01.
"""

from fastapi import FastAPI

from orca.api.app import create_app
from orca.config import Settings


def create_application() -> FastAPI:
    return create_app(Settings())  # type: ignore[call-arg]
