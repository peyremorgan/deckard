"""Behavioral tests for report math, coverage, determinism and unsafe names."""
import base64
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from deckard import engine as core
from deckard.report.model import Report, build, common_folder, json_bytes


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='duplicate-report-test-')
        self.base = Path(self.temp.name)
        self.index = self.base / 'index.sqlite3'
        self.output = self.base / 'report'
        self.db = core.connect(core.raw(str(self.index)))
        for key, value in {'phase':'complete', 'root':core.b64(b'/source'), 'epoch':'1',
                           'inventory_time':'12345', 'last_min_size':'0',
                           'report_inventory_version':'1'}.items():
            core.setmeta(self.db, key, value)
        self.db.execute('INSERT INTO dirs(path,state) VALUES (?,1)', (b'',))
        self.paths = {b''}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add(self, path, logical, allocated=None, content=None, error=None):
        path = path.encode() if isinstance(path, str) else path
        directory = path.rpartition(b'/')[0]
        while directory not in self.paths:
            self.paths.add(directory)
            self.db.execute('INSERT INTO dirs(path,state) VALUES (?,1)', (directory,))
            directory = directory.rpartition(b'/')[0]
        digest = hashlib.sha256(content.encode()).hexdigest() if content is not None else None
        self.db.execute('''INSERT INTO files(path,size,mtime,ctime,dev,ino,nlink,epoch,full,error,allocated)
            VALUES (?,?, 'a','b','1',?,1,1,?,?,?)''',
                        (path, logical, str(len(self.paths)), digest, error,
                         logical if allocated is None else allocated))

    def generate(self, max_items=64):
        self.db.commit()
        build(self.index, self.output, progress=lambda _: None)
        return Report(self.output / 'report.sqlite3', max_items=max_items)

    @staticmethod
    def at(view, a, b):
        x = next(i for i, item in enumerate(view['x']['items']) if item['name'] == a)
        y = next(i for i, item in enumerate(view['y']['items']) if item['name'] == b)
        return view['cells'][x][y]

    def test_requested_geometric_similarity_and_diagonal(self):
        self.add('A/shared', 50, content='shared')
        self.add('A/unique', 50)
        self.add('B/shared', 50, content='shared')
        self.add('B/unique', 450)
        view = self.generate().view()
        cell = self.at(view, 'A', 'B')
        self.assertEqual(cell['shared'], 50)
        self.assertAlmostEqual(cell['similarity'], (50 * 50 / (100 * 500)) ** .5)
        self.assertFalse(cell['x_in_y'])
        self.assertTrue(self.at(view, 'A', 'A')['overlap'])
        self.assertEqual(self.at(view, 'A', 'A')['similarity'], 0)
        with contextlib.closing(sqlite3.connect(self.output / 'report.sqlite3')) as db:
            stored = db.execute('SELECT logical,hex(sha256) FROM contents').fetchone()
            self.assertEqual(stored, (50, hashlib.sha256(b'shared').hexdigest().upper()))

    def test_single_counterpart_and_distributed_coverage_are_distinct(self):
        self.add('A/x', 10, content='x')
        self.add('B/y', 20, content='y')
        self.add('D/x', 10, content='x')
        self.add('D/y', 20, content='y')
        self.add('C/x', 10, content='x')
        model = self.generate()
        view = model.view()
        items = {item['name']: item for item in view['x']['items']}
        self.assertTrue(items['D']['covered_elsewhere'])
        self.assertFalse(self.at(view, 'D', 'A')['x_in_y'])
        self.assertFalse(self.at(view, 'D', 'B')['x_in_y'])
        self.assertTrue(self.at(view, 'C', 'A')['x_in_y'])
        self.assertTrue(self.at(view, 'C', 'A')['y_in_x'])
        self.assertTrue(model.keepers(items['D']['token'])['distributed_only'])
        self.assertEqual([i['path'] for i in model.keepers(items['C']['token'])['keepers']], ['A', 'D'])
        self.assertEqual({i['path'] for i in model.covered()['items']}, {'A', 'B', 'C', 'D'})

    def test_internal_duplicates_do_not_establish_external_coverage(self):
        self.add('A/nested/a', 20, content='x')
        self.add('A/other/b', 20, content='x')
        view = self.generate().view()
        self.assertFalse(view['x']['items'][0]['covered_elsewhere'])
        token = view['x']['items'][0]['token']
        nested = self.generate().view(token, token)
        self.assertTrue(self.at(nested, 'nested', 'other')['x_in_y'])
        inner = next(i for i in nested['x']['items'] if i['name'] == 'nested')
        self.assertEqual([i['path'] for i in self.generate().keepers(inner['token'])['keepers']], ['A/other'])

    def test_empty_file_and_skipped_entries_block_false_removal_claims(self):
        self.add('A/x', 20, content='x')
        self.add('A/empty', 0, content='empty')
        self.add('B/x', 20, content='x')
        model = self.generate()
        self.assertFalse(self.at(model.view(), 'A', 'B')['x_in_y'])
        self.add('B/empty', 0, content='empty')
        self.db.execute('INSERT INTO skipped VALUES (?,?,?)', (b'A/link', b'A', 'symlink'))
        model = self.generate()
        view = model.view()
        self.assertFalse(self.at(view, 'A', 'B')['x_in_y'])
        self.assertTrue(self.at(view, 'A', 'B')['y_in_x'])
        self.assertFalse(next(i for i in view['x']['items'] if i['name'] == 'A')['covered_elsewhere'])

    def test_allocation_sparse_files_and_duplicate_multiplicity(self):
        self.add('A/a', 100, 5, 'x')
        self.add('A/b', 100, 5, 'x')
        self.add('B/c', 100, 80, 'x')
        model = self.generate()
        cell = self.at(model.view(), 'A', 'B')
        self.assertEqual(cell['shared'], 10)
        self.assertTrue(cell['x_in_y'])
        logical = self.at(model.view(metric='logical'), 'A', 'B')
        self.assertEqual(logical['shared'], 100)
        self.assertAlmostEqual(logical['similarity'], 1 / 2**.5)

    def test_unhashed_hardlink_identity_establishes_content_coverage(self):
        self.add('A/file', 10)
        self.add('B/alias', 10)
        self.add('C/unique', 10)
        self.db.execute("UPDATE files SET nlink=2,ino='123' WHERE path IN (?,?)", (b'A/file', b'B/alias'))
        model = self.generate()
        cell = self.at(model.view(), 'A', 'B')
        self.assertTrue(cell['x_in_single'])
        self.assertTrue(cell['y_in_single'])
        self.assertEqual(cell['similarity'], 1)
        self.assertEqual(model.manifest()['hardlink_paths'], 2)
        with contextlib.closing(sqlite3.connect(self.output / 'report.sqlite3')) as db:
            proof = db.execute('SELECT sha256,source_identity FROM contents').fetchone()
            self.assertIsNone(proof[0])
            self.assertEqual(json.loads(proof[1])[-1], '123')

    def test_nested_axes_and_files_do_not_match_themselves(self):
        self.add('A/one/x', 10, content='x')
        self.add('B/y', 10, content='x')
        model = self.generate()
        root = model.view()
        token = next(i['token'] for i in root['x']['items'] if i['name'] == 'A')
        view = model.view(token, 'd:1')
        self.assertTrue(self.at(view, 'one', 'A')['overlap'])
        self.assertTrue(self.at(view, 'one', 'B')['x_in_y'])
        one = view['x']['items'][0]['token']
        leaf = model.view(one, one)
        file_token = leaf['x']['items'][0]['token']
        self.assertTrue(model.view(file_token, file_token)['cells'][0][0]['overlap'])
        self.assertTrue(model.view(file_token, file_token)['cells'][0][0]['x_covered_files'])

    def test_deterministic_artifacts_and_raw_names(self):
        self.add(b'A/<img onerror="x">\xff', 10, content='x')
        self.add(b'B/copy', 10, content='x')
        model = self.generate()
        first = {name: (self.output / name).read_bytes() for name in ('manifest.json', 'root.json', 'report.sqlite3')}
        before = self.index.read_bytes()
        again = self.base / 'again'
        build(self.index, again, progress=lambda _: None)
        for name, data in first.items():
            self.assertEqual(data, (again / name).read_bytes(), name)
        self.assertEqual(self.index.read_bytes(), before)
        token = next(i['token'] for i in model.view()['x']['items'] if i['name'] == 'A')
        item = model.view(token, token)['x']['items'][0]
        self.assertEqual(base64.b64decode(item['path_base64']), b'A/<img onerror="x">\xff')
        self.assertNotIn(b'</script>', json_bytes(model.manifest()))

    def test_level_of_detail_preserves_weights_and_remainder_can_open(self):
        for i in range(6):
            self.add('folder{}/a'.format(i), 10, content='x')
        model = self.generate(max_items=3)
        view = model.view()
        self.assertEqual(view['x']['total'], 60)
        other = view['x']['items'][-1]
        self.assertEqual(other['kind'], 'other')
        self.assertEqual(other['allocated'], 40)
        self.assertFalse(view['cells'][-1][-1]['x_in_y'])
        next_view = model.view(xo=other['offset'])
        self.assertEqual(next_view['x']['total'], 40)
        self.assertEqual(sum(i['weight'] for i in next_view['x']['items']), 40)

    def test_missing_inventory_metadata_and_filtered_scan_rejected(self):
        self.add('A/a', 1)
        core.setmeta(self.db, 'report_inventory_version', '0')
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'refresh'):
            build(self.index, self.output, progress=lambda _: None)
        core.setmeta(self.db, 'report_inventory_version', '1')
        core.setmeta(self.db, 'last_min_size', '10')
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'min-size'):
            build(self.index, self.output, progress=lambda _: None)

    def test_common_folder_uses_components_not_string_prefix(self):
        self.assertEqual(common_folder(b'a/b', b'a/bc'), b'a')
        self.assertEqual(common_folder(b'a/b', b'a/b/c'), b'a/b')
        self.assertEqual(common_folder(b'a', b'b'), b'')

    def test_generation_failure_preserves_existing_report(self):
        self.add('A/a', 10, content='x')
        self.add('B/b', 10, content='x')
        self.generate()
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        with patch.object(Report, 'view', side_effect=RuntimeError('matrix preparation failed')):
            with self.assertRaisesRegex(RuntimeError, 'preparation'):
                build(self.index, self.output, progress=lambda _: None)
        self.assertEqual({p.name: p.read_bytes() for p in self.output.iterdir()}, before)

    def test_http_api_assets_and_loopback_host_validation(self):
        self.add('A/a', 10, content='x')
        self.add('B/b', 10, content='x')
        self.generate()
        # Archived UI assets stay untouched; opening its data uses the current
        # application viewer, including newly introduced controls.
        archived_html = b'<title>Archived UI</title>'
        (self.output / 'index.html').write_bytes(archived_html)
        proc = subprocess.Popen([sys.executable, '-u', '-m', 'deckard.report', 'serve',
                                 '--report-dir', str(self.output), '--port', '0'],
                                cwd=str(Path(__file__).resolve().parents[1]),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
            self.assertTrue(line.startswith('Deckard report: '), line)
            url = line.strip().split(' ', 2)[2]
            with urlopen(url + 'api/view') as response:
                self.assertEqual(json.load(response)['format_version'], 1)
                self.assertIn('report;dur=', response.headers['Server-Timing'])
            with urlopen(url + 'api/covered') as response:
                self.assertEqual(json.load(response)['total'], 2)
            with urlopen(url) as response:
                html = response.read()
                self.assertIn(b'Deckard', html)
                self.assertIn(b'id="metric"', html)
                self.assertIn(b'id="settings"', html)
                self.assertIn(b'id="sectors" type="checkbox" checked', html)
                self.assertNotIn(b'Archived UI', html)
                self.assertIn("default-src 'self'", response.headers['Content-Security-Policy'])
            self.assertEqual((self.output / 'index.html').read_bytes(), archived_html)
            for suffix, status in (('report.sqlite3', 404), ('api/view?metric=invalid', 400)):
                with self.assertRaises(HTTPError) as error:
                    urlopen(url + suffix)
                self.assertEqual(error.exception.code, status)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url, headers={'Host': 'untrusted.example'}))
            self.assertEqual(error.exception.code, 403)
        finally:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
            proc.stdout.close(); proc.stderr.close()


if __name__ == '__main__':
    unittest.main()
