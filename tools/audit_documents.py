"""Read the complete source corpus; emit reproducible structural review evidence.

This is a documentation linter, not a SQL executor or a scientific validator.
Run from the repository root: python tools/audit_documents.py
"""
from pathlib import Path
import hashlib
import json
import re
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

def audit():
    files = []
    for path in sorted(ROOT.glob('*.md')):
        if path.name == 'README.md':
            continue
        raw = path.read_bytes()
        text = raw.decode('utf-8-sig')
        lines = text.splitlines()
        headings = [{'line': n, 'text': line} for n, line in enumerate(lines, 1)
                    if re.match(r'^#{1,6} ', line)]
        files.append({'path': path.name, 'sha256': hashlib.sha256(raw).hexdigest(),
                      'bytes': len(raw), 'lines': len(lines), 'headings': headings,
                      'sql_blocks': len(re.findall(r'^```sql\s*$', text, re.M))})
    hashes = {}
    for item in files:
        hashes.setdefault(item['sha256'], []).append(item['path'])
    report = {'scope': 'Full-file structural scan; human review focuses on architecture contracts, SQL, and conflicting rules.',
              'files': files, 'exact_duplicates': [v for v in hashes.values() if len(v) > 1]}
    out = ROOT / 'docs' / 'review'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'document_inventory.json').write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    checked = [ROOT / 'README.md', ROOT / 'design.md',
               ROOT / 'ORCA_ENSv2_Arkiv_Integration_Blueprint_2026-11-05.md',
               *sorted((ROOT / 'docs').glob('*.md'))]
    errors = []
    link_count = 0
    json_count = 0
    for path in checked:
        body = path.read_text(encoding='utf-8-sig')
        for target in re.findall(r'\]\(([^\n)]+)\)', body):
            target = target.strip().strip('<>')
            if re.match(r'^[a-z][a-z0-9+.-]*:', target, re.I) or target.startswith('#'):
                continue
            relative = unquote(target.split('#', 1)[0])
            link_count += 1
            if not (path.parent / relative).exists():
                errors.append(f'{path.name}: missing local link {target}')
        # Only new canonical guides promise well-formed illustrative JSON.
        if path.parent == ROOT / 'docs':
            for block in re.findall(r'^```json\s*\n(.*?)^```', body, re.M | re.S):
                json_count += 1
                try:
                    json.loads(block)
                except json.JSONDecodeError as exc:
                    errors.append(f'{path.name}: invalid JSON illustration: {exc}')
    db = next(ROOT.glob('ORCA Architecture Document 4*.md')).read_text(encoding='utf-8-sig')
    if re.search(r'\bevidence_id\s+UUID\s+.*REFERENCES evidence_records', db):
        errors.append('Database document: UUID FK still targets TEXT evidence ID')
    if re.search(r'\b(?:evidence_ids|evidence_refs|trigger_evidence_ids)\s+UUID\[\]', db):
        errors.append('Database document: evidence projection still uses UUID[]')
    for name in ('ORCA_Core_Agentic_Layer.md', 'ORCA_Combined_Architecture.md',
                 'ORCA_Complete_System_Diagram.md', 'ORCA_Presentation_Diagram.md'):
        if re.search(r'\b(?:Fast|DEC)\s*-->\s*Syn\b', (ROOT / name).read_text(encoding='utf-8')):
            errors.append(f'{name}: direct factual synthesis bypass remains')
    checks = {'scope': 'Static documentation checks only; not runtime or SQL validation.',
              'source_files': len(files), 'source_bytes': sum(f['bytes'] for f in files),
              'exact_duplicate_groups': report['exact_duplicates'],
              'local_links_checked': link_count, 'canonical_json_examples_checked': json_count,
              'errors': errors}
    (out / 'validation_report.json').write_text(json.dumps(checks, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(checks, indent=2))
    if errors:
        raise SystemExit(1)

if __name__ == '__main__':
    audit()
