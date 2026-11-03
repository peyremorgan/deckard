# -*- coding: utf-8 -*-
"""Portable functional tests; no dependencies beyond Python 2.7/3 stdlib."""
from __future__ import print_function
import base64
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from deckard import engine as scan


@contextlib.contextmanager
def replaced(obj, name, value):
    previous = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, previous)


class ScannerTests(unittest.TestCase):
    def test_reporting_migration_from_old_schema_preserves_hashes(self):
        self.put('a', b'same')
        self.put('b', b'same')
        self.assertEqual(self.run_scan(), 0)
        with contextlib.closing(sqlite3.connect(os.path.join(self.state, 'index.sqlite3'))) as db:
            columns = 'id,path,size,mtime,ctime,dev,ino,nlink,epoch,prefix,sample,full,error'
            hashes = db.execute('SELECT path,full FROM files ORDER BY path').fetchall()
            db.execute('ALTER TABLE files RENAME TO old_files')
            db.execute('''CREATE TABLE files(id INTEGER PRIMARY KEY,path BLOB UNIQUE NOT NULL,
                size INTEGER,mtime TEXT,ctime TEXT,dev TEXT,ino TEXT,nlink INTEGER,
                epoch INTEGER,prefix TEXT,sample TEXT,full TEXT,error TEXT)''')
            db.execute('INSERT INTO files(' + columns + ') SELECT ' + columns + ' FROM old_files')
            db.execute('DROP TABLE old_files')
            db.execute('DROP TABLE skipped')
            db.execute("DELETE FROM meta WHERE key LIKE 'report_inventory%'")
            db.commit()
        self.assertEqual(self.run_scan('--refresh'), 0)
        with contextlib.closing(sqlite3.connect(os.path.join(self.state, 'index.sqlite3'))) as db:
            self.assertEqual(db.execute('SELECT path,full FROM files ORDER BY path').fetchall(), hashes)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM files WHERE allocated IS NULL').fetchone()[0], 0)

    def test_report_inventory_allocation_skips_and_cache_preservation(self):
        a = self.put('a', b'same')
        self.put('nested/b', b'same')
        os.symlink(a, os.path.join(self.root, 'link'))
        self.assertEqual(self.run_scan(), 0)
        with contextlib.closing(sqlite3.connect(os.path.join(self.state, 'index.sqlite3'))) as db:
            before = db.execute('SELECT path,full FROM files ORDER BY path').fetchall()
            self.assertEqual(db.execute('SELECT allocated FROM files WHERE path=?', (scan.blob(b'a'),)).fetchone()[0],
                             os.lstat(a).st_blocks * 512)
            self.assertEqual(db.execute('SELECT kind FROM skipped').fetchall(), [('symlink',)])
            self.assertEqual(dict(db.execute('SELECT * FROM meta'))['report_inventory_version'], '1')
        os.unlink(os.path.join(self.root, 'link'))
        self.assertEqual(self.run_scan('--refresh'), 0)
        with contextlib.closing(sqlite3.connect(os.path.join(self.state, 'index.sqlite3'))) as db:
            self.assertEqual(db.execute('SELECT path,full FROM files ORDER BY path').fetchall(), before)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM skipped').fetchone()[0], 0)

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='nas-duplicate-test-')
        self.root = os.path.join(self.base, 'source')
        self.state = os.path.join(self.base, 'state')
        os.mkdir(self.root)

    def tearDown(self):
        shutil.rmtree(self.base)

    def put(self, name, data):
        path = os.path.join(scan.raw(self.root), scan.raw(name))
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, 'wb') as stream:
            stream.write(data)
        return path

    def run_scan(self, *options):
        with replaced(scan, 'log', lambda message: None):
            return scan.main(['scan', self.root, '--state-dir', self.state] + list(options))

    def records(self, kind):
        with open(os.path.join(self.state, 'duplicates.jsonl')) as stream:
            return [row for row in (json.loads(line) for line in stream) if row['type'] == kind]

    def metrics(self):
        rows = []
        for name in sorted(os.listdir(self.state)):
            if name.startswith('metrics-'):
                with open(os.path.join(self.state, name)) as stream:
                    rows.extend(json.loads(line) for line in stream)
        return rows

    def test_small_empty_unique_and_raw_names(self):
        self.put(b'a', b'same')
        unusual = b'quote"\n\xff' if sys.platform.startswith('linux') else b'quote"\n\xc3\xa9'
        self.put(unusual, b'same')
        self.put(u'nested/caf\u00e9', b'same')
        self.put(b'empty1', b'')
        self.put(b'empty2', b'')
        self.put(b'unique', b'longer and unique')
        self.assertEqual(self.run_scan(), 0)
        groups = self.records('duplicate_group')
        self.assertEqual(set(row['size'] for row in groups), set([0, 4]))
        group = next(row for row in groups if row['size'] == 4)
        paths = set(base64.b64decode(row['path_base64']) for row in group['paths'])
        self.assertIn(os.path.join(scan.raw(os.path.realpath(self.root)), unusual), paths)
        self.assertEqual(group['distinct_file_count'], 3)

    def test_sample_collision_rejected_by_full_hash(self):
        data = b'a' * (12 * scan.SAMPLE)
        self.put('same1', data)
        self.put('same2', data)
        self.put('same_sample_different_contents', data[:2*scan.SAMPLE] + b'b' + data[2*scan.SAMPLE+1:])
        self.put('same_prefix_different_tail', data[:-1] + b'c')
        self.put('different_prefix', b'd' + data[1:])
        calls, original = [], scan.hash_file
        def tracked(root, row, stage, block):
            calls.append((scan.raw(row['path']), stage))
            return original(root, row, stage, block)
        with replaced(scan, 'hash_file', tracked):
            self.assertEqual(self.run_scan('--sample-min-size', '0'), 0)
        groups = self.records('duplicate_group')
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['path_count'], 2)
        self.assertIn((b'same_sample_different_contents', 'full'), calls)
        self.assertNotIn((b'same_prefix_different_tail', 'full'), calls)
        self.assertNotIn((b'different_prefix', 'sample'), calls)

    def test_sampling_cutoff_changes_work_without_changing_duplicates(self):
        data = b'a' * (4 * scan.SAMPLE)
        self.put('same1', data)
        self.put('same2', data)
        self.put('changed_tail', data[:-1] + b'b')
        self.assertEqual(self.run_scan('--sample-min-size', '0'), 0)
        expected = self.records('duplicate_group')
        with contextlib.closing(sqlite3.connect(os.path.join(self.state, 'index.sqlite3'))) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM files WHERE full IS NOT NULL').fetchone()[0], 2)
        self.assertEqual(self.run_scan('--sample-min-size', '1048576'), 0)
        self.assertEqual(self.records('duplicate_group'), expected)
        with contextlib.closing(sqlite3.connect(os.path.join(self.state, 'index.sqlite3'))) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM files WHERE full IS NOT NULL').fetchone()[0], 3)
        self.assertEqual(self.run_scan('--rehash', '--sample-min-size', '1048576'), 0)
        self.assertEqual(self.records('duplicate_group'), expected)
        self.assertEqual(self.records('file_error'), [])

    def test_cache_resume_refresh_and_rehash(self):
        a = self.put('a', b'original')
        self.put('b', b'original')
        self.assertEqual(self.run_scan(), 0)
        def unexpected(*args):
            raise AssertionError('cached file was read')
        with replaced(scan, 'hash_file', unexpected):
            self.assertEqual(self.run_scan(), 0)
        old = os.stat(a)
        with open(a, 'wb') as stream:
            stream.write(b'modified')
        os.utime(a, (old.st_atime, old.st_mtime))
        self.assertEqual(self.run_scan('--refresh'), 0)
        self.assertEqual(self.records('duplicate_group'), [])
        with open(a, 'wb') as stream:
            stream.write(b'original')
        self.assertEqual(self.run_scan('--refresh', '--rehash'), 0)
        self.assertEqual(len(self.records('duplicate_group')), 1)

    def test_hardlinks_hashed_once_and_alias_only_skipped(self):
        a = self.put('a', b'a' * (12 * scan.SAMPLE))
        os.link(a, os.path.join(self.root, 'alias'))
        self.assertEqual(self.run_scan(), 0)
        self.assertEqual(self.records('duplicate_group'), [])
        finals = [row for row in self.metrics() if row['type'] == 'final']
        self.assertEqual(finals[-1]['counters'].get('bytes_read', 0), 0)
        self.put('copy', b'a' * (12 * scan.SAMPLE))
        self.assertEqual(self.run_scan('--refresh'), 0)
        group = self.records('duplicate_group')[0]
        self.assertEqual(group['path_count'], 3)
        self.assertEqual(group['distinct_file_count'], 2)
        self.assertEqual(group['potential_reclaimable_logical_bytes'], 12 * scan.SAMPLE)
        final = [row for row in self.metrics() if row['type'] == 'final'][-1]
        expected = 2 * scan.SAMPLE + 2 * 12 * scan.SAMPLE
        self.assertEqual(final['counters']['bytes_read'], expected)

    def test_symlink_and_loop_skipped(self):
        a = self.put('a', b'same')
        self.put('b', b'same')
        os.symlink(a, os.path.join(self.root, 'link'))
        os.symlink(self.root, os.path.join(self.root, 'loop'))
        self.assertEqual(self.run_scan(), 0)
        self.assertEqual(self.records('duplicate_group')[0]['path_count'], 2)

    def test_inventory_errors_continue_and_retry(self):
        self.put('a', b'one')
        self.put('b', b'one')
        self.put('denied/a', b'two')
        self.put('denied/b', b'two')
        original = scan.call
        def denied(name, function, *args, **kwargs):
            if name == 'fs.directory.open' and scan.raw(args[0]).endswith(b'/denied'):
                raise OSError('simulated denied directory')
            return original(name, function, *args, **kwargs)
        with replaced(scan, 'call', denied):
            self.assertEqual(self.run_scan(), 2)
        self.assertEqual(len(self.records('duplicate_group')), 1)
        self.assertFalse(self.records('summary')[0]['complete'])
        self.assertEqual(self.run_scan('--retry-errors'), 0)
        self.assertEqual(len(self.records('duplicate_group')), 2)

    def test_failed_reads_reported_then_retried(self):
        self.put('a', b'same')
        self.put('b', b'same')
        original = scan.hash_file
        def denied(root, row, stage, block):
            if scan.raw(row['path']) == b'a':
                raise OSError('simulated unreadable file')
            return original(root, row, stage, block)
        with replaced(scan, 'hash_file', denied):
            self.assertEqual(self.run_scan(), 2)
        self.assertEqual(len(self.records('file_error')), 1)
        self.assertEqual(self.run_scan('--retry-errors'), 0)
        self.assertEqual(len(self.records('duplicate_group')), 1)

    def test_inventory_interruption_checkpoints_and_resumes(self):
        for i in range(100):
            self.put('nested/%s' % i, b'same')
        original, visits = scan.files_upsert_many, []
        def interrupt(db, records, epoch):
            if records and not visits:
                visits.append(1)
                original(db, records[:10], epoch)
                db.commit()
                raise KeyboardInterrupt()
            return original(db, records, epoch)
        with replaced(scan, 'files_upsert_many', interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_scan('--commit-rows', '3')
        self.assertIsNone(scan.PROFILE)
        self.assertEqual(self.run_scan(), 0)
        self.assertEqual(self.records('duplicate_group')[0]['path_count'], 100)

    def test_final_validation_rejects_stale_cached_file(self):
        a = self.put('a', b'same')
        self.put('b', b'same')
        self.assertEqual(self.run_scan(), 0)
        with open(a, 'wb') as stream:
            stream.write(b'edit')
        self.assertEqual(self.run_scan(), 2)
        self.assertEqual(self.records('duplicate_group'), [])

    def test_changes_during_hash_are_rejected(self):
        a = self.put('a', b'same')
        row = dict(zip(('size','mtime','ctime','dev','ino'), scan.fingerprint(os.lstat(a))))
        row['path'], row['prefix'] = b'a', None
        original = scan.read_exact
        def mutate(stream, amount):
            data = original(stream, amount)
            with open(a, 'wb') as changed:
                changed.write(b'changed during read')
            return data
        with replaced(scan, 'read_exact', mutate):
            with self.assertRaises(OSError):
                scan.hash_file(scan.raw(self.root), row, 'full', 4096)

    def test_interrupted_hardlink_batches_leave_representative_pending(self):
        a = self.put('a', b'duplicate')
        for i in range(300):
            os.link(a, os.path.join(self.root, 'alias-%s' % i))
        self.put('copy', b'duplicate')
        self.assertEqual(self.run_scan('--inventory-only'), 0)
        original = scan.Checkpoint.tick
        fired = []
        def interrupt(checkpoint, amount=1, force=False):
            result = original(checkpoint, amount, force)
            if amount == 256 and not fired:
                fired.append(True)
                checkpoint.db.commit()
                raise KeyboardInterrupt()
            return result
        with replaced(scan.Checkpoint, 'tick', interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_scan()
        self.assertTrue(fired)
        self.assertEqual(self.run_scan(), 0)
        self.assertEqual(self.records('duplicate_group')[0]['path_count'], 302)

    def test_refresh_removes_deleted_and_discovers_new_paths(self):
        a = self.put('a', b'same')
        self.put('b', b'same')
        self.assertEqual(self.run_scan(), 0)
        os.unlink(a)
        self.put('c', b'same')
        self.assertEqual(self.run_scan('--refresh'), 0)
        paths = [row['path'] for row in self.records('duplicate_group')[0]['paths']]
        self.assertEqual(set(os.path.basename(path) for path in paths), set(['b', 'c']))

    def test_inventory_only_then_hashes_and_explicit_minimum(self):
        self.put('a', b'same')
        self.put('b', b'same')
        self.assertEqual(self.run_scan('--inventory-only'), 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, 'duplicates.jsonl')))
        self.assertEqual(self.run_scan('--min-size', '5'), 0)
        self.assertEqual(self.records('duplicate_group'), [])
        self.assertEqual(self.run_scan(), 0)
        self.assertEqual(len(self.records('duplicate_group')), 1)

    def test_larger_minimum_excludes_previously_cached_small_duplicates(self):
        self.put('a', b'same')
        self.put('b', b'same')
        self.assertEqual(self.run_scan(), 0)
        self.assertEqual(self.run_scan('--min-size', '5'), 0)
        self.assertEqual(self.records('duplicate_group'), [])
        self.assertEqual(self.run_scan(), 0)
        self.assertEqual(len(self.records('duplicate_group')), 1)

    def test_probe_does_not_modify_index_or_source(self):
        a = self.put('a', b'a' * (2 * 1048576))
        self.put('b', b'a' * (2 * 1048576))
        self.assertEqual(self.run_scan(), 0)
        index = os.path.join(self.state, 'index.sqlite3')
        with open(index, 'rb') as stream:
            before = hashlib.sha256(stream.read()).hexdigest()
        with replaced(scan, 'log', lambda message: None):
            result = scan.main(['probe', self.root, '--state-dir', self.state,
                                '--probe-directories', '2', '--probe-read-mib', '1',
                                '--probe-rows', '100', '--probe-workers', '1,2',
                                '--probe-blocks', '4096,65536'])
        self.assertEqual(result, 0)
        with open(index, 'rb') as stream:
            self.assertEqual(hashlib.sha256(stream.read()).hexdigest(), before)
        self.assertFalse(any(name.startswith('probe-sqlite-') for name in os.listdir(self.state)))
        self.assertEqual(os.stat(a).st_size, 2 * 1048576)
        probes = [name for name in os.listdir(self.state) if name.startswith('probe-')]
        with open(os.path.join(self.state, probes[0])) as stream:
            results = json.load(stream)
        self.assertEqual(len(results['hashing']), 4)
        self.assertEqual(results['sqlite']['rows'], 100)

    def test_heartbeat_survives_blocked_io(self):
        self.put('a', b'same')
        original = scan.call
        def delayed(name, function, *args, **kwargs):
            if name == 'fs.listdir':
                def slow(*a, **k):
                    time.sleep(.1)
                    return function(*a, **k)
                return original(name, slow, *args, **kwargs)
            return original(name, function, *args, **kwargs)
        with replaced(scan, 'call', delayed):
            self.assertEqual(self.run_scan('--inventory-only', '--heartbeat', '.01'), 0)
        active = [active for row in self.metrics() if row['type'] == 'heartbeat' for active in row['active']]
        self.assertTrue(any(row['operation'] == 'fs.listdir' and row['elapsed_seconds'] > .02 for row in active))

    def test_disjoint_state_required(self):
        with replaced(scan, 'log', lambda message: None):
            with self.assertRaises(SystemExit):
                scan.main(['scan', self.root, '--state-dir', os.path.join(self.root, 'state')])
        self.assertFalse(os.path.exists(os.path.join(self.root, 'state')))

    def test_sampled_timings_keep_exact_call_counts(self):
        for i in range(100):
            self.put(str(i), b'a' * i)
        self.assertEqual(self.run_scan('--inventory-only'), 0)
        final = [row for row in self.metrics() if row['type'] == 'final'][-1]
        row = final['metrics']['fs.entry.stat']
        self.assertEqual(row['calls_total'], 100)
        self.assertTrue(1 <= row['calls'] <= 10)
        self.assertTrue(row['estimated_seconds'] >= row['seconds'])
        self.assertEqual(self.run_scan('--refresh', '--inventory-only', '--timing-sample', '1'), 0)
        final = [row for row in self.metrics() if row['type'] == 'final'][-1]
        self.assertEqual(final['metrics']['fs.entry.stat']['calls'], 100)

    def test_native_prefix_hash_batches_handle_tail_and_hardlinks(self):
        first=self.put('file-000',b'same')
        os.link(first,os.path.join(self.root,'alias'))
        for i in range(1,37):self.put('file-%03d'%i,b'same')
        self.assertEqual(self.run_scan('--prefix-hash-batch','16'),0)
        group=self.records('duplicate_group')[0]
        self.assertEqual(group['path_count'],38)
        self.assertEqual(group['distinct_file_count'],37)

    def test_native_batches_enumerate_new_children_and_short_tail(self):
        for i in range(37):
            self.put('branch-%s/child/a' % i,b'same')
            self.put('branch-%s/child/b' % i,b'same')
        self.assertEqual(self.run_scan('--inventory-batch','16'),0)
        self.assertEqual(self.records('duplicate_group')[0]['path_count'],74)

    def test_bulk_resume_reconciles_new_changed_and_cached_rows(self):
        a = self.put('a', b'old')
        b = self.put('b', b'old')
        os.mkdir(self.state)
        db = scan.connect(scan.raw(os.path.join(self.state, 'index.sqlite3')), 8, scan.raw(self.state))
        def record(path):
            st = os.lstat(path)
            return (os.path.basename(path), scan.fingerprint(st), st.st_nlink)
        try:
            scan.files_upsert_many(db, [record(a), record(b)], 1)
            db.execute('UPDATE files SET full=?', ('cached-hash',))
            with open(a, 'wb') as stream:
                stream.write(b'changed')
            c = self.put('c', b'new')
            records = [record(a), record(b), record(c)]
            scan.files_upsert_many(db, records, 1)
            rows = dict((scan.raw(row['path']), row['full']) for row in db.execute('SELECT path,full FROM files'))
            self.assertIsNone(rows[b'a'])
            self.assertEqual(rows[b'b'], 'cached-hash')
            self.assertIsNone(rows[b'c'])
            scan.files_upsert_many(db, records, 2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM files WHERE epoch=2').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT full FROM files WHERE path=?', (scan.blob(b'b'),)).fetchone()[0], 'cached-hash')
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
