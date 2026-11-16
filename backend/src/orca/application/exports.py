"""Purpose: assemble P06 portable evidence exports from verified immutable input bytes.
Behavior: validate rights, content digests and snapshot identity before writing a canonical manifest and ZIP.
Refs: main_implementation plan.md P06.3; api design.md §§3.7, 4.4; Phase: P06.
"""

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import rfc8785

from orca.contracts.science_evidence import ScienceEvidenceManifest
from orca.science.manifests import source_bytes_digest, verify_manifest


@dataclass(frozen=True, slots=True)
class ExportInput:
    snapshot_id: UUID
    license_ref: str
    may_redistribute: bool
    content: bytes | None
    object_version_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExportPackage:
    manifest: dict[str, object]
    manifest_digest: str
    archive: bytes
    archive_digest: str


def build_evidence_export(
    evidence: ScienceEvidenceManifest, inputs: tuple[ExportInput, ...]
) -> ExportPackage:
    """Export exact approved input versions; restricted inputs remain references only."""
    verify_manifest(evidence)
    supplied = {item.snapshot_id: item for item in inputs}
    required = {item.snapshot_id for item in evidence.inputs}
    if len(supplied) != len(inputs) or set(supplied) != required:
        raise ValueError("export inputs must match every evidence snapshot exactly")

    entries: list[dict[str, object]] = []
    included: dict[str, bytes] = {}
    for source in evidence.inputs:
        item = supplied[source.snapshot_id]
        if not item.license_ref.strip():
            raise ValueError("every exported source requires a license reference")
        if item.object_version_id != source.object_version_id:
            raise ValueError("snapshot object version differs from immutable evidence")
        if item.may_redistribute:
            if item.content is None or len(item.content) != source.byte_count:
                raise ValueError("redistributable input bytes are missing or incomplete")
            if source_bytes_digest(item.content) != source.raw_content_digest:
                raise ValueError("input bytes differ from the evidence digest")
            path = f"inputs/{source.snapshot_id}.bin"
            included[path] = item.content
        else:
            if item.content is not None:
                raise PermissionError("restricted source bytes must not enter an export")
            path = None
        entries.append(
            {
                "snapshot_id": str(source.snapshot_id),
                "role": source.role,
                "year": source.year,
                "digest": source.raw_content_digest,
                "object_version_id": source.object_version_id,
                "license_ref": item.license_ref,
                "redistributed": item.may_redistribute,
                "path": path,
            }
        )

    manifest: dict[str, object] = {
        "schema_version": "orca.export/1.0",
        "evidence_manifest_id": str(evidence.id),
        "evidence_manifest_digest": evidence.manifest_digest,
        "method_id": evidence.method_id,
        "method_version": evidence.method_version,
        "method_code_digest": evidence.method_code_digest,
        "recipe_digest": evidence.recipe_digest,
        "result_digest": evidence.result_digest,
        "result_status": evidence.result_status,
        "source_mode": evidence.source_mode,
        "inputs": entries,
    }
    encoded = rfc8785.dumps(cast(Any, manifest))
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True, indent=2))
        for path, content in included.items():
            archive.writestr(path, content)
    archive_bytes = buffer.getvalue()
    return ExportPackage(
        manifest, digest, archive_bytes, "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
    )
