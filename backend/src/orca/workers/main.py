"""Purpose: provide the P05 worker process entry boundary.
Behavior: requires explicit runtime composition; does not create mock sources or approved methods.
Refs: main_implementation plan.md P05.3; Phase: P05.
"""

from orca.workers.runner import RunWorker

__all__ = ["RunWorker"]
