"""Purpose: validate preserved originals alongside explicitly recorded source revisions.
Behavior: verify revision hashes; allow only the explicitly approved README archive gap.
Refs: original-scope-recovery; source-revisions.json; Phase: P00 recovery.
"""

import hashlib
from pathlib import Path


def verify_source(
    root: Path, path: str, original_hash: str, revisions: dict[str, dict[str, str]]
) -> None:
    """Accept an unchanged source or a fully evidenced, specifically pinned revision."""
    current = root / path
    actual = hashlib.sha256(current.read_bytes()).hexdigest()
    revision = revisions.get(path)
    if revision is None:
        if actual != original_hash:
            raise ValueError(f"Unreconciled source change: {path}")
        return
    if revision["original_sha256"] != original_hash or revision["current_sha256"] != actual:
        raise ValueError(f"Source revision mismatch: {path}")
    if revision.get("exception") == "user-approved-readme-baseline-2026-11-13":
        if (
            path != "README.md"
            or original_hash != "7e3b554247f95e54159f495df5d8f70aff2adbfd9aad5f4a7c76da6e20351d78"
            or actual != "cb70fac7cfc56fca35f84c4206577dcc30303ba80923f2a7ddef8d441bcc74f4"
        ):
            raise ValueError("README exception cannot apply to other source revisions")
        return
    archive = (root / revision["archive"]).resolve()
    if not archive.is_relative_to(root.resolve()):
        raise ValueError("Archive must remain within workspace")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != original_hash:
        raise ValueError(f"Original archive mismatch: {path}")
    if not revision.get("reason") or not revision.get("decision"):
        raise ValueError("Revision must identify its reason and decision")
