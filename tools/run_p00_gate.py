"""Purpose: compute G00 from actual offline tests and preserved source checksums.
Behavior: fail on missing/failed/skipped tests; write hashed local gate evidence.
Refs: main_implementation plan.md sections 6 P00 and 9; ADR-000.
Phase: P00 bootstrap only. The general backend gate runner belongs to P01.
"""
import ast
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/gates/P00'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test


def main():
    requirements = json.loads((OUT / 'requirements.json').read_text(encoding='utf-8'))
    required = [name for names in requirements['subphase_tests'].values() for name in names]
    test_path = ROOT / requirements['test_file']
    spec = importlib.util.spec_from_file_location('p00_tests', test_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    collected = list(flatten(suite))
    names = [test._testMethodName for test in collected]
    checks = []
    if not names or len(set(names)) != len(names) or not set(required).issubset(names):
        checks.append('Missing, duplicate or zero tests')
    for path in [Path(__file__), test_path]:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        header = ast.get_docstring(tree) or ''
        if not all(field in header for field in ['Purpose:', 'Behavior:', 'Refs:', 'Phase:']):
            checks.append(f'Missing purpose header: {path.name}')
        compile(tree, str(path), 'exec')
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    log = stream.getvalue()
    (OUT / 'test-results.txt').write_text(log, encoding='utf-8')
    failures = {test.id(): error for test, error in result.failures + result.errors}
    skipped = {test.id(): reason for test, reason in result.skipped}
    success = result.wasSuccessful() and not skipped and not checks and result.testsRun >= len(required)
    xml = ET.Element('testsuite', name='G00', tests=str(result.testsRun),
                     failures=str(len(result.failures)), errors=str(len(result.errors)), skipped=str(len(skipped)))
    for test in collected:
        case = ET.SubElement(xml, 'testcase', name=test._testMethodName, classname=type(test).__name__)
        if test.id() in failures:
            ET.SubElement(case, 'failure').text = failures[test.id()]
        if test.id() in skipped:
            ET.SubElement(case, 'skipped').text = skipped[test.id()]
    ET.ElementTree(xml).write(OUT / 'test-results.xml', encoding='utf-8', xml_declaration=True)
    source_register = module.read_yaml('docs/sources/source_register.yaml')
    input_paths = {ROOT / x['path'] for x in source_register['sources']}
    input_paths.update((ROOT / 'docs/adr').glob('ADR-00*.md'))
    input_paths.update((ROOT / 'docs/sources').glob('*'))
    input_paths.update((ROOT / 'docs/releases').glob('*'))
    input_paths.update([test_path, Path(__file__), OUT / 'requirements.json', OUT / 'decisions.yaml', OUT / 'acceptance_matrix.csv'])
    hashes = {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(input_paths) if p.is_file()}
    tree_hash = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    status = 'PASS' if success else 'FAIL'
    report = {**requirements, 'status': status, 'source_commit': None,
        'source_tree_sha256': tree_hash, 'source_hash_scope': 'registered inputs and P00 artifacts; excludes generated reports',
        'input_hashes': hashes, 'generated_at': datetime.now(timezone.utc).isoformat(),
        'runner_version': 'p00-bootstrap/1', 'environment': {'python': sys.version, 'execution': 'local, not CI'},
        'counts': {'required': len(required), 'collected': len(names), 'run': result.testsRun,
                   'passed': result.testsRun-len(failures)-len(skipped), 'failed': len(failures), 'skipped': len(skipped)},
        'subphase_results': {gate: status if checks else ('PASS' if all(
            n in names and not any(t._testMethodName == n and (t.id() in failures or t.id() in skipped) for t in collected)
            for n in tests) else 'FAIL') for gate, tests in requirements['subphase_tests'].items()},
        'static_checks': checks, 'test_output_sha256': digest(OUT / 'test-results.txt'),
        'unresolved_historical_links': len(source_register['unresolved_local_links']),
        'next_phase': 'P01' if success else None}
    (OUT / 'manifest.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    (OUT / 'report.md').write_text(
        f'# P00 gate report\n\nStatus: **{status}**. Local execution; no CI run claimed.\n\n'
        f'Command: `python tools/run_p00_gate.py`\n\n'
        f'Tests: {result.testsRun} run; {len(required)} named required cases; {len(failures)} failures/errors; {len(skipped)} skips.\n\n'
        f'Input tree SHA-256: `{tree_hash}`. No Git commit exists in this workspace.\n\n'
        '## Evidence\n\n[Computed manifest](manifest.json), [text test output](test-results.txt), '
        '[JUnit XML](test-results.xml), [requirements](requirements.json), [acceptance matrix](acceptance_matrix.csv).\n\n'
        '## Limits and next action\n\nP00 establishes recorded scope, source provenance and engineering decisions only. '
        f'{len(source_register["unresolved_local_links"])} historical companion links are explicitly unresolved under ADR-000; '
        'they were not silently repaired or called verified. New P00 ADR links resolve. '
        'No provider dataset, baseline, rights, dependency lock, backend endpoint, migration or chain transaction has been validated. '
        'G03 source/baseline/rights prerequisites remain unsatisfied. P01 is the next phase only if this report is PASS. '
        'All later implementation phases remain unstarted. Rerun this command after any input change.\n', encoding='utf-8')
    print(log)
    print(json.dumps({'gate': 'G00', 'status': status, 'counts': report['counts'], 'static_errors': checks}, indent=2))
    return 0 if success else 1


if __name__ == '__main__':
    # Invalidate previous success before discovery/import: a missing input must
    # never leave an old green manifest as the latest gate result.
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'manifest.json').write_text(json.dumps({
        'phase': 'P00', 'gate_id': 'G00', 'status': 'IN_PROGRESS',
        'generated_at': datetime.now(timezone.utc).isoformat()
    }, indent=2)+'\n', encoding='utf-8')
    (OUT / 'report.md').write_text('# P00 gate report\n\nStatus: IN_PROGRESS. Prior result invalidated.\n', encoding='utf-8')
    try:
        exit_code = main()
    except Exception as exc:
        (OUT / 'manifest.json').write_text(json.dumps({
            'phase': 'P00', 'gate_id': 'G00', 'status': 'FAIL',
            'reason': 'Gate bootstrap could not load or validate required inputs',
            'error_type': type(exc).__name__,
            'generated_at': datetime.now(timezone.utc).isoformat()
        }, indent=2)+'\n', encoding='utf-8')
        (OUT / 'report.md').write_text('# P00 gate report\n\nStatus: **FAIL**. Required input/bootstrap error; prior PASS invalidated.\n', encoding='utf-8')
        print(f'G00 FAIL: {type(exc).__name__}', file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)
