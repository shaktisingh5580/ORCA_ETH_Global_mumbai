"""Purpose: render P06 scientific report views from verified evidence lineage.
Behavior: emit escaped HTML and JSON with explicit status and limitations; never create unsupported numeric claims.
Refs: main_implementation plan.md P06.2; api design.md §§3.7, 6.6; Phase: P06.
"""

import html
import json
from typing import cast

from orca.contracts.science_evidence import ScienceEvidenceManifest
from orca.science.manifests import verify_manifest


def report_view(
    evidence: ScienceEvidenceManifest, *, title: str, limitations: tuple[str, ...]
) -> dict[str, object]:
    verify_manifest(evidence)
    if not title.strip() or len(title) > 200:
        raise ValueError("report title is required and must be bounded")
    if evidence.result_status != "supported" and not limitations:
        raise ValueError("non-supported reports must explain their limitations")
    return {
        "schema_version": "orca.report/1.0",
        "title": title,
        "status": evidence.result_status,
        "source_mode": evidence.source_mode,
        "evidence_manifest_id": str(evidence.id),
        "evidence_manifest_digest": evidence.manifest_digest,
        "method_id": evidence.method_id,
        "method_version": evidence.method_version,
        "result_digest": evidence.result_digest,
        "limitations": list(limitations),
        "official_advisory": False,
    }


def render_report_json(view: dict[str, object]) -> bytes:
    return json.dumps(view, sort_keys=True, separators=(",", ":")).encode("utf-8")


def render_report_html(view: dict[str, object]) -> bytes:
    title = html.escape(str(view["title"]))
    status = html.escape(str(view["status"]))
    digest = html.escape(str(view["evidence_manifest_digest"]))
    limitations = "".join(
        f"<li>{html.escape(str(item))}</li>" for item in cast(list[str], view["limitations"])
    )
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        f"<title>{title}</title></head><body><main><h1>{title}</h1>"
        f"<p>Scientific status: {status}</p><p>Evidence manifest: {digest}</p>"
        f"<h2>Limitations</h2><ul>{limitations}</ul>"
        "<p>This is a research report, not an official advisory.</p></main></body></html>"
    )
    return document.encode("utf-8")
