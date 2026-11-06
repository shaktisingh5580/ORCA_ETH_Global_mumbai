"""Purpose: preserve licensed P03 raw bytes under tenant-scoped content addresses.
Behavior: publish complete fsynced files atomically and verify existing content on reuse.
Refs: main plan P03.3; research-only guide 5.2; Phase: P03.3.
"""

import hashlib
import os
import tempfile
from pathlib import Path
from uuid import UUID


class SnapshotFiles:
    def __init__(self, root: Path, max_bytes: int = 16 * 1024 * 1024) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes

    def preserve(self, tenant: UUID, content: bytes, *, permitted: bool) -> str:
        if not permitted:
            raise PermissionError("source preservation rights are unverified")
        if not content or len(content) > self.max_bytes:
            raise ValueError("empty or oversized snapshot")
        digest = hashlib.sha256(content).hexdigest()
        folder = self.root / str(tenant)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / digest
        descriptor, name = tempfile.mkstemp(dir=folder)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise ValueError("existing immutable snapshot is corrupt") from None
        finally:
            temporary.unlink(missing_ok=True)
        return "sha256:" + digest
