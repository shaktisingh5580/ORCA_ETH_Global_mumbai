"""Purpose: configure offline P01 tests and isolated test tooling imports.
Behavior: expose scripts/support only to tests; never modify production bootstrap.
Refs: main plan P01.1-P01.4; Phase: P01.
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "scripts"))
sys.path.insert(0, str(BACKEND / "tests/support"))
