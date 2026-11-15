"""Purpose: provide canonical hashing for P04 immutable science evidence.
Behavior: hash source bytes and verify a manifest before persistence or replay.
Refs: main_implementation plan.md P04.4; docs/04_database.md §§3,7; Phase: P04.
"""

import hashlib

import rfc8785

from orca.contracts.science_evidence import ScienceEvidenceManifest


def source_bytes_digest(content: bytes) -> str:
    if not content:
        raise ValueError("scientific source input cannot be empty")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def verify_manifest(manifest: ScienceEvidenceManifest) -> None:
    expected = (
        "sha256:"
        + hashlib.sha256(
            rfc8785.dumps(manifest.model_dump(mode="json", exclude={"manifest_digest"}))
        ).hexdigest()
    )
    if expected != manifest.manifest_digest:
        raise ValueError("science evidence manifest digest does not match its content")
