"""Purpose: protect P00 decisions against invented source readiness and unsafe scope.
Behavior: run the twelve named plan assertions plus negative policy cases offline.
Refs: main_implementation plan.md section 6 P00.1-P00.4; ADR-000 through ADR-004.
Phase: P00. No production adapters or synthetic runtime results are introduced.
"""
import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path
import re
import unittest
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]


def read_yaml(relative):
    """Read our deliberately JSON-compatible YAML subset without new dependencies."""
    text = (ROOT / relative).read_text(encoding='utf-8')
    return json.loads('\n'.join(line for line in text.splitlines() if not line.startswith('#')))


def assert_unknown_rights_closed(entry):
    if entry['status'] == 'UNKNOWN' and any(entry[k] for k in
            ('ingest_allowed', 'preserve_allowed', 'export_allowed')):
        raise ValueError('Unknown rights must deny ingest, preservation and export')


def assert_baseline_closed(baseline):
    required = {'units', 'depth', 'calendar', 'measurement_semantics', 'seasonal_alignment'}
    if not required.issubset(baseline['required_compatibility']):
        raise ValueError('Missing baseline compatibility requirements')
    if baseline['status'] == 'UNKNOWN' and baseline['allowed_for_execution']:
        raise ValueError('Unknown baseline cannot execute')


class P00Decisions(unittest.TestCase):
    def test_source_register_has_origin_checksum_and_classification(self):
        register = read_yaml('docs/sources/source_register.yaml')
        checksums = (ROOT / 'docs/sources/source_checksums.sha256').read_text(encoding='utf-8')
        for row in register['sources']:
            with self.subTest(source=row['path']):
                self.assertTrue(row['origin'])
                self.assertTrue(row['classification'])
                self.assertRegex(row['captured_date'], r'^\d{4}-\d{2}-\d{2}$')
                digest = hashlib.sha256((ROOT / row['path']).read_bytes()).hexdigest()
                self.assertEqual(digest, row['sha256'])
                self.assertIn(f"{digest}  {row['path']}\n", checksums)
                if row['origin'] == 'user_attachment':
                    self.assertEqual(digest, row['origin_sha256'])

    def test_docs_all_local_links_resolve(self):
        # ADR-000 applies the plan's explicit 'verified or unresolved' exception
        # only to preserved historical inputs. New P00 links must all resolve.
        register = read_yaml('docs/sources/source_register.yaml')
        declared = {(x['source'], x['target']): x for x in register['unresolved_local_links']}
        observed = set()
        paths = [ROOT / x['path'] for x in register['sources']]
        paths += list((ROOT / 'docs/adr').glob('ADR-00*.md'))
        for path in paths:
            for target in re.findall(r'\]\(([^\n)]+)\)', path.read_text(encoding='utf-8-sig')):
                target = target.strip().strip('<>')
                if re.match(r'^[a-z][a-z0-9+.-]*:', target, re.I) or target.startswith('#'):
                    continue
                if not (path.parent / unquote(target.split('#')[0])).exists():
                    key = (path.relative_to(ROOT).as_posix(), target)
                    self.assertIn(key, declared, f'Undeclared broken link: {key}')
                    self.assertTrue(declared[key]['reason'])
                    self.assertEqual(declared[key]['status'], 'UNRESOLVED')
                    self.assertTrue((ROOT / declared[key]['resolution_adr']).is_file())
                    observed.add(key)
        self.assertEqual(observed, set(declared), 'Stale/unnecessary unresolved-link exception')

    def test_source_conflicts_have_adrs(self):
        decision = read_yaml('docs/gates/P00/decisions.yaml')
        self.assertGreaterEqual(len(decision['conflicts']), 7)
        for conflict in decision['conflicts']:
            self.assertTrue((ROOT / conflict['resolution']).is_file())
        register = read_yaml('docs/sources/source_register.yaml')
        for name in ['design.md', 'docs/04_database.md', 'docs/07_scientific_engine.md']:
            self.assertIn(name, register['embedded_design_markers'])

    def test_scope_bounded_area_and_time(self):
        scope = read_yaml('docs/releases/release_profiles.yaml')['scope']
        west, south, east, north = scope['bbox']
        self.assertTrue(-180 <= west < east <= 180 and east - west <= 1)
        self.assertTrue(-90 <= south < north <= 90 and north - south <= 1)
        start = datetime.fromisoformat(scope['start'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(scope['end'].replace('Z', '+00:00'))
        self.assertTrue(0 < (end - start).days <= 31)
        self.assertEqual(scope['coverage_status'], 'UNVERIFIED')

    def test_each_outcome_has_acceptance_metric(self):
        with (ROOT / 'docs/gates/P00/acceptance_matrix.csv').open(encoding='utf-8', newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 4)
        for row in rows:
            for field in ['id', 'outcome', 'metric', 'owner', 'required_gate']:
                self.assertTrue(row[field])
            self.assertEqual(row['status'], 'NOT_IMPLEMENTED')
        profiles = read_yaml('docs/releases/release_profiles.yaml')['profiles']
        self.assertEqual({p['acceptance_id'] for p in profiles}, {r['id'] for r in rows})
        for profile in profiles:
            self.assertEqual(profile['status'], 'NOT_RELEASED')
            self.assertEqual(profile['gates'], [f'G{i:02}' for i in range(len(profile['gates']))])

    def test_no_claim_all_legacy_demos_complete(self):
        scope = read_yaml('docs/releases/release_profiles.yaml')['scope']
        self.assertIs(scope['all_legacy_demos_complete'], False)
        adr = (ROOT / 'docs/adr/ADR-001-initial-sst-scope.md').read_text(encoding='utf-8')
        self.assertIn('Non-goals', adr)
        self.assertIn('not completed capabilities', adr)

    def test_product_registry_unknown_ids_not_invented(self):
        candidates = read_yaml('docs/sources/candidate_products.yaml')
        self.assertEqual(candidates['selection_status'], 'UNSELECTED')
        self.assertEqual(len({c['key'] for c in candidates['candidates']}), len(candidates['candidates']))
        for candidate in candidates['candidates']:
            source = (ROOT / candidate['source_ref']).read_text(encoding='utf-8')
            if candidate['provider_product_id'] is not None:
                self.assertIn(candidate['provider_product_id'], source)
            self.assertEqual(candidate['access_status'], 'UNKNOWN')
            self.assertIsNone(candidate['live_probe'])
            self.assertFalse(candidate['coverage_verified'])
            self.assertTrue(candidate['missing'])

    def test_baseline_requires_compatible_units_depth_calendar(self):
        baseline = read_yaml('docs/sources/scientific_baselines.yaml')
        assert_baseline_closed(baseline)
        self.assertIsNone(baseline['selected_product_id'])
        self.assertIsNone(baseline['baseline_years'])
        self.assertEqual(baseline['approval_gate'], 'G03')

    def test_license_unknown_is_not_allowed(self):
        rights = read_yaml('docs/sources/rights_register.yaml')
        self.assertEqual(rights['default'], 'DENY_UNTIL_VERIFIED')
        candidates = read_yaml('docs/sources/candidate_products.yaml')['candidates']
        self.assertEqual({r['candidate_key'] for r in rights['entries']}, {c['key'] for c in candidates})
        for entry in rights['entries']:
            assert_unknown_rights_closed(entry)
            self.assertIsNone(entry['evidence'])

    def test_adr_has_alternatives_and_rejected_options(self):
        paths = list((ROOT / 'docs/adr').glob('ADR-00*.md'))
        self.assertEqual(len(paths), 5)
        for path in paths:
            body = path.read_text(encoding='utf-8')
            self.assertIn('## Decision', body)
            self.assertIn('## Alternatives and rejected options', body)
            self.assertIn('Rejected:', body)

    def test_runtime_version_unverified_until_installed(self):
        runtime = read_yaml('docs/gates/P00/decisions.yaml')['runtime']
        self.assertFalse(runtime['installed_and_locked'])
        self.assertEqual(runtime['compatibility_status'], 'UNVERIFIED')
        self.assertIsNone(runtime['exact_patch'])
        self.assertEqual(runtime['lock_gate'], 'G01')

    def test_safety_no_production_mock(self):
        policy = read_yaml('docs/gates/P00/decisions.yaml')['policy']
        self.assertFalse(policy['production_mock_allowed'])
        self.assertFalse(policy['unknown_rights_allowed'])
        self.assertFalse(policy['science_by_llm'])
        # Later phases may add runtime code; the no-test-data invariant persists.
        analyzer = ROOT / 'backend/scripts/check_import_boundaries.py'
        if (ROOT / 'backend/src').exists():
            import importlib.util
            spec = importlib.util.spec_from_file_location('boundaries', analyzer)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.violations(ROOT / 'backend/src'), [])

    def test_unknown_license_cannot_gain_export_permission(self):
        entry = read_yaml('docs/sources/rights_register.yaml')['entries'][0]
        entry['export_allowed'] = True
        with self.assertRaises(ValueError):
            assert_unknown_rights_closed(entry)

    def test_unknown_license_cannot_gain_ingest_permission(self):
        entry = read_yaml('docs/sources/rights_register.yaml')['entries'][0]
        entry['ingest_allowed'] = True
        with self.assertRaises(ValueError):
            assert_unknown_rights_closed(entry)

    def test_unknown_baseline_cannot_be_enabled(self):
        baseline = read_yaml('docs/sources/scientific_baselines.yaml')
        baseline['allowed_for_execution'] = True
        with self.assertRaises(ValueError):
            assert_baseline_closed(baseline)

    def test_baseline_without_calendar_policy_is_rejected(self):
        baseline = read_yaml('docs/sources/scientific_baselines.yaml')
        baseline['required_compatibility'].remove('calendar')
        with self.assertRaises(ValueError):
            assert_baseline_closed(baseline)


if __name__ == '__main__':
    unittest.main()
