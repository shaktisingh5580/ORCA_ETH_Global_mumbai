"""Purpose: render P06 scientific report views from verified evidence lineage.
Behavior: emit escaped HTML and JSON with explicit status and limitations; never create unsupported numeric claims.
Refs: main_implementation plan.md P06.2; api design.md §§3.7, 6.6; Phase: P06.
"""

import html
import json
import textwrap
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


def render_report_pdf(view: dict[str, object]) -> bytes:
    """Render a bounded text-only PDF without remote assets or executable document content."""
    lines = [
        str(view["title"]),
        "Scientific status: " + str(view["status"]),
        "Source mode: " + str(view["source_mode"]),
        "Evidence manifest: " + str(view["evidence_manifest_digest"]),
        "Method: " + str(view["method_id"]) + " / " + str(view["method_version"]),
        "Result digest: " + str(view["result_digest"]),
        "Limitations:",
    ]
    lines.extend("- " + str(item) for item in cast(list[str], view["limitations"]))
    lines.append("Research report only. Not an official advisory or causal attribution.")
    wrapped = [
        fragment
        for line in lines
        for fragment in (textwrap.wrap(line, width=88, break_long_words=True) or [""])
    ]
    if len(wrapped) > 250:
        raise ValueError("report text exceeds bounded PDF size")
    pages = [wrapped[index : index + 50] for index in range(0, len(wrapped), 50)]
    page_ids = [5 + 2 * index for index in range(len(pages))]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{id} 0 R" for id in page_ids)
            + f"] /Count {len(pages)} >>"
        ).encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for page in pages:
        commands = ["BT /F1 10 Tf 48 790 Td 13 TL"]
        for line in page:
            ascii_line = line.encode("ascii", "replace").decode("ascii")
            safe = ascii_line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"({safe}) Tj T*")
        commands.append("ET")
        content = "\n".join(commands).encode("ascii")
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream"
        )
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {4 + 2 * (len(objects) - 4) // 2} 0 R >>".encode(
                "ascii"
            )
        )
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, item in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii") + item + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(output)
