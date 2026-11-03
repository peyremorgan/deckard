#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared read-only duplicate-detection engine. Python 2.7 / 3.x.

The Mac controller owns SQLite, checkpoints, reports and diagnostics. The
stateless NAS worker reuses the local filesystem routines in this library.
Use controller.py for the supported command-line interface.

Scan resumes its inventory and completed file hashes by default. Use
--refresh to discover new/deleted files after a completed inventory, and
--rehash to discard cached hashes. A file interrupted mid-hash restarts.
Use a quiet tree or snapshot: this is not a live-filesystem snapshot.

Only regular-file data is compared. Symlinks and other filesystems are skipped.
No source files are deleted or modified. SHA-256 matches are not byte-by-byte
comparisons; xattrs/resource forks/ACLs are outside scope. Cache validity uses
native stat metadata, with Python 2's floating-point timestamp precision.
Use --rehash for metadata-preserving edits or untrusted/coarse timestamps.
Space estimates are logical, not guaranteed physical savings.

SQLite, sort files, diagnostics, and reports live on the Mac's local disk.
The native nas-1 index can be imported; the old SMB index is incompatible.
Unreadable paths are recorded; readable paths are
still processed and the report is marked incomplete (exit 2). --retry-errors
retries failed directories and files. --strict stops after inventory errors.

Diagnostics: independent heartbeat, active paths/calls, interval and cumulative
timings, latency buckets, CPU, peak RSS, queue sizes, database/WAL sizes. Timed
operations overlap across threads; don't sum them as wall time. Probe mode
uses bounded source reads and a disposable scratch database, never the index
for writes. Probe results include cache/order caveats and measured rates.
"""
from __future__ import print_function

import argparse
import base64
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import random
import re
import resource
import signal
import sqlite3
import stat
import sys
import threading
import time
try:
    import queue
except ImportError:
    import Queue as queue

try:
    text_type = unicode
except NameError:
    text_type = str

SCHEMA = 'nas-1'
IMPLEMENTATION = 'deckard-0.1'
SOURCE_EXCLUDES = ()
CANCEL_EVENT = None
SAMPLE = 65536
PROFILE = None
SQL_LABELS = {}


class SourceUnavailable(RuntimeError):
    """Transport failed; replay the unfinished job, never mark a file bad."""


source_root_stat = os.lstat
pool_cancel = lambda threads: None


def raise_worker_error(event):
    if len(event) > 2 and event[2]:
        raise SourceUnavailable(event[1])
    raise RuntimeError(event[1])


def monotonic_clock():
    if hasattr(time, 'monotonic'):
        return time.monotonic
    # Python 2 on milk has no monotonic clock; Linux 3.2 has clock_gettime.
    try:
        library = ctypes.CDLL('librt.so.1', use_errno=True)
        class Timespec(ctypes.Structure):
            _fields_ = [('seconds', ctypes.c_long), ('nanos', ctypes.c_long)]
        clock_gettime = library.clock_gettime
        clock_gettime.argtypes = [ctypes.c_int, ctypes.POINTER(Timespec)]
        clock_gettime.restype = ctypes.c_int
        def now():
            value = Timespec()
            if clock_gettime(1, ctypes.byref(value)):
                raise OSError(ctypes.get_errno(), 'clock_gettime')
            return value.seconds + value.nanos / 1e9
        now()
        return now
    except (AttributeError, OSError):
        return time.time


NOW = monotonic_clock()
def process_cpu():
    # Python 2 time.clock wraps on this 32-bit NAS after ~36 CPU minutes.
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


CPU = getattr(time, 'process_time', None) or process_cpu


def raw(value):
    if isinstance(value, text_type):
        if hasattr(os, 'fsencode'):
            return os.fsencode(value)
        # SSH sessions on DSM often report an ASCII locale despite UTF-8 names.
        return value.encode('utf-8')
    return bytes(value)


def display(path):
    return raw(path).decode('utf-8', 'replace')


def b64(path):
    return base64.b64encode(raw(path)).decode('ascii')


def blob(path):
    return sqlite3.Binary(raw(path))


def log(message):
    line = time.strftime('%H:%M:%S') + ' ' + message
    if sys.version_info[0] < 3 and isinstance(line, text_type):
        line = line.encode('utf-8', 'backslashreplace')
    print(line, file=sys.stderr)
    sys.stderr.flush()


class Profile(object):
    def __init__(self, state, interval, enabled=True, sample_every=16):
        self.enabled, self.interval = enabled, interval
        self.sample_every = sample_every
        self.local = threading.local()
        self.call_counts = {}
        self.state, self.phase = state, 'startup'
        self.lock, self.write_lock = threading.Lock(), threading.Lock()
        self.stop = threading.Event()
        self.stats, self.counters, self.active, self.gauges = {}, {}, {}, {}
        self.started, self.cpu_started = NOW(), CPU()
        self.previous_time, self.previous_cpu = self.started, self.cpu_started
        self.previous, self.previous_counts = {}, {}
        self.previous_io = {}
        self.last_slow = {}
        name = 'metrics-%s-%s-%s.jsonl' % (time.strftime('%Y%m%dT%H%M%S'), os.getpid(), int(time.time() * 1000000))
        self.path = os.path.join(state, raw(name))
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.output = os.fdopen(fd, 'w')
        self.thread = threading.Thread(target=self.heartbeat, name='metrics')
        self.thread.daemon = True

    def emit(self, record):
        record.update(time_unix=time.time(), pid=os.getpid())
        with self.write_lock:
            if self.output is not None:
                try:
                    self.output.write(json.dumps(record, ensure_ascii=True) + '\n')
                    self.output.flush()
                except (IOError, OSError) as exc:
                    log('Metrics output failed: %s' % exc)
                    with contextlib.suppress(IOError) if hasattr(contextlib, 'suppress') else ignored_io():
                        self.output.close()
                    self.output = None

    def add(self, name, value=1):
        with self.lock:
            self.counters[name] = self.counters.get(name, 0) + value

    def gauge(self, name, value):
        with self.lock:
            self.gauges[name] = value

    def set_phase(self, value):
        with self.lock:
            self.phase = value
        log('Phase: ' + value)
        self.emit({'type': 'phase', 'phase': value})

    def should_time(self, name):
        """Count all calls, avoiding context-manager allocation between samples."""
        if not self.enabled:
            return False
        ident = threading.current_thread().ident
        if not hasattr(self.local, 'calls'):
            with self.lock:
                self.local.calls = self.call_counts.setdefault(ident, {})
        calls = self.local.calls
        if name not in calls:
            with self.lock:
                calls[name] = 0
        calls[name] += 1
        sampled = (name.startswith(('fs.', 'wire.', 'queue.')) and name != 'fs.listdir') or name in (
            'cpu.sha256', 'sql.fetchone', 'sql.fetchall', 'sql.next', 'report.write',
            'sql.insert.files', 'sql.update.files', 'sql.select.files',
            'sql.insert.dirs', 'sql.update.dirs', 'sql.select.dirs')
        return not (sampled and (calls[name] - 1) % self.sample_every)

    @contextlib.contextmanager
    def span(self, name, path=None, detail=None, selected=False):
        if not self.enabled or (not selected and not self.should_time(name)):
            yield
            return
        ident = threading.current_thread().ident
        started = NOW()
        with self.lock:
            stack = self.active.setdefault(ident, [])
            inherited = stack[-1].get('path') if stack else None
            item = {'operation': name, 'path': display(path) if path is not None else inherited,
                    'detail': detail, 'started': started, 'thread': threading.current_thread().name}
            stack.append(item)
        failed = False
        try:
            yield
        except BaseException as exc:
            failed = not isinstance(exc, (StopIteration, queue.Empty, queue.Full))
            raise
        finally:
            elapsed = NOW() - started
            with self.lock:
                stack.pop()
                if not stack:
                    del self.active[ident]
                row = self.stats.setdefault(name, {'calls': 0, 'seconds': 0., 'max_seconds': 0.,
                                                    'errors': 0, 'latency_buckets': [0] * 8})
                row['calls'] += 1
                row['seconds'] += elapsed
                row['errors'] += int(failed)
                row['max_seconds'] = max(row['max_seconds'], elapsed)
                for i, ceiling in enumerate((.0001, .001, .01, .1, 1, 10, 60, float('inf'))):
                    if elapsed <= ceiling:
                        row['latency_buckets'][i] += 1
                        break
                slow = elapsed >= 1 and NOW() - self.last_slow.get(name, -1e20) >= self.interval
                if slow:
                    self.last_slow[name] = NOW()
            if slow:
                self.emit(dict(item, type='slow_operation', seconds=elapsed, error=failed))

    def snapshot(self, final=False):
        now, cpu = NOW(), CPU()
        with self.lock:
            stats = dict((key, dict(value, latency_buckets=list(value['latency_buckets'])))
                         for key, value in self.stats.items())
            totals = {}
            for values in self.call_counts.values():
                for key, value in list(values.items()):
                    totals[key] = totals.get(key, 0) + value
            for key, value in stats.items():
                value['calls_total'] = totals.get(key, value['calls'])
                value['estimated_seconds'] = value['seconds'] * value['calls_total'] / max(1, value['calls'])
            counters, gauges, phase = dict(self.counters), dict(self.gauges), self.phase
            active = [dict(stack[-1], elapsed_seconds=max(0, now - stack[-1]['started']))
                      for stack in self.active.values() if stack]
        seconds = max(now - self.previous_time, .000001)
        intervals = {}
        for key, value in stats.items():
            old = self.previous.get(key, {'calls': 0, 'seconds': 0})
            calls = value['calls'] - old['calls']
            if calls:
                elapsed = value['seconds'] - old['seconds']
                total_calls = value['calls_total'] - old.get('calls_total', 0)
                intervals[key] = {'calls': calls, 'calls_total': total_calls, 'seconds': elapsed,
                                  'estimated_seconds': elapsed * total_calls / calls,
                                  'mean_ms': 1000 * elapsed / calls}
        rates = dict((key, (value - self.previous_counts.get(key, 0)) / seconds)
                     for key, value in counters.items())
        for suffix, name in ((b'', 'index_bytes'), (b'-wal', 'wal_bytes')):
            try:
                gauges[name] = os.stat(os.path.join(self.state, b'index.sqlite3') + suffix).st_size
            except OSError:
                pass
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = rss if sys.platform == 'darwin' else rss * 1024
        physical_io, io_rates = {}, {}
        if sys.platform.startswith('linux'):
            try:
                with open('/proc/self/io') as stream:
                    for line in stream:
                        key, value = line.split(':', 1)
                        physical_io[key] = int(value.strip())
                io_rates = dict((key, (value - self.previous_io.get(key, value)) / seconds)
                                for key, value in physical_io.items())
                with open('/proc/self/statm') as stream:
                    gauges['current_rss_bytes'] = int(stream.read().split()[1]) * os.sysconf('SC_PAGESIZE')
                gauges['open_fds'] = len(os.listdir('/proc/self/fd'))
            except (IOError, OSError, ValueError):
                pass
        record = {'type': 'final' if final else 'heartbeat', 'phase': phase,
                  'elapsed_seconds': now - self.started, 'cpu_seconds': cpu - self.cpu_started,
                  'interval_seconds': seconds, 'cpu_percent': 100 * (cpu - self.previous_cpu) / seconds,
                  'peak_rss_bytes': peak_bytes, 'metrics': stats, 'interval_metrics': intervals,
                  'frequent_call_timing_sample_every': self.sample_every,
                  'process_io_totals': physical_io, 'process_io_rates': io_rates,
                  'counters': counters, 'rates': rates, 'gauges': gauges, 'active': active,
                  'latency_bucket_upper_seconds': [.0001, .001, .01, .1, 1, 10, 60, None]}
        self.emit(record)
        top = sorted(((key, value) for key, value in intervals.items() if not key.startswith('task.')),
                     key=lambda pair: pair[1]['estimated_seconds'], reverse=True)[:3]
        waits = '; '.join('%s %.1fs %r' % (item['operation'], item['elapsed_seconds'], item['path'])
                          for item in sorted(active, key=lambda row: row['elapsed_seconds'], reverse=True)[:4])
        log('PERF %s cpu=%.0f%% rss=%.1fMiB entries/s=%.1f read=%.2fMiB/s %s active=%s' % (
            phase, record['cpu_percent'], peak_bytes / 1048576., rates.get('entries', 0),
            rates.get('bytes_read', 0) / 1048576.,
            '; '.join('%s est=%.2fs samples=%s/%s' % (key, value['estimated_seconds'], value['calls'], value['calls_total']) for key, value in top),
            waits or 'idle'))
        self.previous, self.previous_counts = stats, counters
        self.previous_io = physical_io
        self.previous_time, self.previous_cpu = now, cpu
        return record

    def heartbeat(self):
        while not self.stop.wait(self.interval):
            self.snapshot()

    def start(self):
        self.emit({'type': 'start', 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
                   'timings_enabled': self.enabled, 'timing_sample_every': self.sample_every,
                   'note': 'Frequent timings are sampled; estimated_seconds extrapolates observations. Nested/concurrent elapsed timings overlap.'})
        log('Metrics: ' + display(self.path))
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join()
        self.snapshot(final=True)
        with self.write_lock:
            if self.output is not None:
                self.output.close()
                self.output = None


@contextlib.contextmanager
def ignored_io():
    try:
        yield
    except (IOError, OSError):
        pass


@contextlib.contextmanager
def span(name, path=None, detail=None):
    if PROFILE is None:
        yield
    else:
        with PROFILE.span(name, path, detail):
            yield


def call(name, function, *args, **kwargs):
    trace_path = kwargs.pop('_path', None)
    profiler = PROFILE
    if profiler is not None and profiler.should_time(name):
        with profiler.span(name, trace_path, selected=True):
            return function(*args, **kwargs)
    return function(*args, **kwargs)


def count(name, value=1):
    if PROFILE is not None:
        PROFILE.add(name, value)


def phase(value):
    if PROFILE is not None:
        PROFILE.set_phase(value)


class Cursor(sqlite3.Cursor):
    def execute(self, statement, parameters=()):
        cached = SQL_LABELS.get(statement)
        if cached is None:
            normalized = ' '.join(statement.split())
            match = re.search(r'\b(?:INTO|UPDATE|FROM|TABLE|INDEX)\s+([\w.]+)', normalized, re.I)
            label = 'sql.%s.%s' % (normalized.split(' ', 1)[0].lower(), match.group(1) if match else 'other')
            cached = label, normalized[:400]
            if len(SQL_LABELS) < 128:
                SQL_LABELS[statement] = cached
        label, self.detail = cached
        with span(label, detail=self.detail):
            return super(Cursor, self).execute(statement, parameters)

    def fetchone(self):
        with span('sql.fetchone', detail=getattr(self, 'detail', None)):
            return super(Cursor, self).fetchone()

    def fetchall(self):
        with span('sql.fetchall', detail=getattr(self, 'detail', None)):
            return super(Cursor, self).fetchall()

    def __next__(self):
        with span('sql.next', detail=getattr(self, 'detail', None)):
            if sys.version_info[0] < 3:
                return super(Cursor, self).next()
            return super(Cursor, self).__next__()

    next = __next__


class Connection(sqlite3.Connection):
    def execute(self, statement, parameters=()):
        return self.cursor(factory=Cursor).execute(statement, parameters)

    def executemany(self, statement, parameters):
        normalized = ' '.join(statement.split())
        match = re.search(r'\b(?:INTO|UPDATE)\s+([\w.]+)', normalized, re.I)
        label = 'sql.batch_%s.%s' % (normalized.split(' ', 1)[0].lower(), match.group(1) if match else 'other')
        with span(label, detail=normalized[:400]):
            return super(Connection, self).executemany(statement, parameters)

    def commit(self):
        with span('sql.commit'):
            return super(Connection, self).commit()

    def close(self):
        with span('sql.close'):
            return super(Connection, self).close()


def connect(path, cache_mib=8, temp_directory=None):
    db = call('sql.connect', sqlite3.connect, display(path), factory=Connection, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    db.execute('PRAGMA cache_size=-%d' % (cache_mib * 1024))
    db.execute('PRAGMA mmap_size=0')
    db.execute('PRAGMA temp_store=FILE')
    if temp_directory is not None:
        # Old SQLite must not spill multi-GB sorts into the NAS system partition.
        directory = display(temp_directory).replace("'", "''")
        db.execute("PRAGMA temp_store_directory='%s'" % directory)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY,path BLOB UNIQUE NOT NULL,
            size INTEGER,mtime TEXT,ctime TEXT,dev TEXT,ino TEXT,nlink INTEGER,
            epoch INTEGER,prefix TEXT,sample TEXT,full TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS dirs(id INTEGER PRIMARY KEY,path BLOB UNIQUE,state INTEGER);
        CREATE INDEX IF NOT EXISTS dirs_pending ON dirs(state,id);
        CREATE TABLE IF NOT EXISTS issues(path BLOB PRIMARY KEY,parent BLOB,message TEXT);
        CREATE INDEX IF NOT EXISTS issues_parent ON issues(parent);
        CREATE INDEX IF NOT EXISTS hardlinks ON files(dev,ino,id) WHERE nlink>1;
        CREATE TABLE IF NOT EXISTS skipped(path BLOB PRIMARY KEY,parent BLOB,kind TEXT);
    ''')
    # Additive migration: allocation is reporting metadata, not a content
    # fingerprint. Collecting it must not invalidate existing SHA-256 caches.
    if 'allocated' not in [row[1] for row in db.execute('PRAGMA table_info(files)')]:
        db.execute('ALTER TABLE files ADD COLUMN allocated INTEGER')
    return db


def meta(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row is not None else default


def setmeta(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))


def fingerprint(st):
    # The key is intentionally stable between Python 2 and Python 3 on the NAS.
    return (st.st_size, float(st.st_mtime).hex(), float(st.st_ctime).hex(), str(st.st_dev), str(st.st_ino))


def expected(row):
    return tuple(row[key] for key in ('size', 'mtime', 'ctime', 'dev', 'ino'))


def check(st, row):
    if not stat.S_ISREG(st.st_mode) or fingerprint(st) != expected(row):
        raise OSError('file changed since inventory; use --refresh')


class Checkpoint(object):
    def __init__(self, db, rows, seconds):
        self.db, self.rows, self.seconds = db, rows, seconds
        self.pending, self.previous = 0, NOW()

    def tick(self, amount=1, force=False):
        self.pending += amount
        if force or self.pending >= self.rows or NOW() - self.previous >= self.seconds:
            self.db.commit()
            count('checkpoints')
            self.pending, self.previous = 0, NOW()


class Pool(object):
    """Bounded jobs/results. Workers never write SQLite or retain a file tree."""
    def __init__(self, workers, function):
        self.function = function
        self.stop = threading.Event()
        self.jobs, self.results = queue.Queue(workers * 2), queue.Queue(workers * 2)
        self.threads = []
        for i in range(workers):
            thread = threading.Thread(target=self.run, name='io-%d' % i)
            thread.daemon = True
            thread.start()
            self.threads.append(thread)

    def emit(self, result):
        while not self.stop.is_set():
            try:
                call('queue.result_put', self.results.put, result, timeout=.2)
                return True
            except queue.Full:
                pass
        return False

    def run(self):
        while not self.stop.is_set():
            try:
                job = call('queue.job_wait', self.jobs.get, timeout=.2)
            except queue.Empty:
                continue
            if job is None or self.stop.is_set():
                return
            try:
                self.function(job, self.emit, self.stop)
            except BaseException as exc:
                self.emit(('fatal', str(exc), isinstance(exc, SourceUnavailable)))
                return

    def close(self):
        self.stop.set()
        pool_cancel(self.threads)
        for thread in self.threads:
            try:
                self.jobs.put_nowait(None)
            except queue.Full:
                break
        # Heartbeat remains active while an outstanding filesystem call settles.
        with span('workers.join'):
            for thread in self.threads:
                while thread.is_alive():
                    thread.join(.2)


def file_upsert(db, relative, values, nlink, epoch, try_insert=False):
    if try_insert:
        inserted = db.execute('INSERT OR IGNORE INTO files(path,size,mtime,ctime,dev,ino,nlink,epoch) VALUES (?,?,?,?,?,?,?,?)',
                              (blob(relative),) + values + (nlink, epoch))
        if inserted.rowcount:
            count('new_files')
            return
    previous = db.execute('SELECT id,size,mtime,ctime,dev,ino FROM files WHERE path=?', (blob(relative),)).fetchone()
    if previous is None:
        db.execute('INSERT INTO files(path,size,mtime,ctime,dev,ino,nlink,epoch) VALUES (?,?,?,?,?,?,?,?)',
                   (blob(relative),) + values + (nlink, epoch))
        count('new_files')
    elif expected(previous) == values:
        db.execute('UPDATE files SET epoch=?,nlink=? WHERE id=?', (epoch, nlink, previous['id']))
        count('unchanged_files')
    else:
        db.execute('''UPDATE files SET size=?,mtime=?,ctime=?,dev=?,ino=?,nlink=?,epoch=?,
                   prefix=NULL,sample=NULL,full=NULL,error=NULL WHERE id=?''',
                   values + (nlink, epoch, previous['id']))
        count('changed_files')


INSERT_FILE = 'INSERT OR IGNORE INTO files(path,size,mtime,ctime,dev,ino,nlink,epoch) VALUES (?,?,?,?,?,?,?,?)'
UPDATE_CHANGED = '''UPDATE files SET size=?,mtime=?,ctime=?,dev=?,ino=?,nlink=?,epoch=?,
                    prefix=NULL,sample=NULL,full=NULL,error=NULL WHERE id=?'''


def files_upsert_many(db, records, epoch):
    """Bounded batch, preserving hashes only when native metadata agrees.

    Initial inventory normally needs just one C-level executemany call. If a
    resumed directory contains preexisting paths, reconcile their fingerprints
    in one bounded lookup. Refreshes look up first to avoid ignored inserts.
    """
    if not records:
        return
    inserted = 0
    if epoch == 1:
        before = db.total_changes
        db.executemany(INSERT_FILE, [(blob(path),) + values + (links, epoch)
                                    for path, values, links in records])
        inserted = db.total_changes - before
        count('new_files', inserted)
        if inserted == len(records):
            return
    paths = tuple(blob(record[0]) for record in records)
    existing = db.execute('''SELECT id,path,size,mtime,ctime,dev,ino,nlink,epoch FROM files
                           WHERE path IN (''' + ','.join('?' for path in paths) + ')', paths).fetchall()
    existing = dict((raw(row['path']), row) for row in existing)
    new, changed, same = [], [], []
    for path, values, links in records:
        row = existing.get(raw(path))
        if row is None:
            new.append((blob(path),) + values + (links, epoch))
        elif expected(row) != values:
            changed.append(values + (links, epoch, row['id']))
        elif row['epoch'] != epoch or row['nlink'] != links:
            same.append((epoch, links, row['id']))
    if new:
        db.executemany(INSERT_FILE, new)
        count('new_files', len(new))
    if changed:
        db.executemany(UPDATE_CHANGED, changed)
        count('changed_files', len(changed))
    if same:
        db.executemany('UPDATE files SET epoch=?,nlink=? WHERE id=?', same)
    count('unchanged_files', len(records) - inserted - len(new) - len(changed))


def inventory_worker(root, root_dev):
    def work(job, emit, stop):
        directory_id, relative = job
        path = os.path.join(root, relative)
        batch, failed = [], False
        with span('task.directory', path):
            directory_fd = None
            try:
                directory_fd = call('fs.directory.open', os.open, path,
                                    os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
                st = call('fs.directory.fstat', os.fstat, directory_fd)
                if not stat.S_ISDIR(st.st_mode) or st.st_dev != root_dev:
                    raise OSError('directory is a symlink, replaced, or on another filesystem')
                # Linux/Python 2 accepts /proc/self/fd paths, not directory FDs.
                # Anchor child lookups to the open directory. Deep backup paths
                # need no repeated traversal of every ancestor for each entry.
                lookup_root = raw('/proc/self/fd/%s' % directory_fd) if sys.platform.startswith('linux') else path
                # Python 2 lacks scandir; memory is bounded by the largest ONE
                # directory per worker plus the bounded emitted batch queues.
                names = call('fs.listdir', os.listdir, lookup_root)
                for name in names:
                    if stop.is_set():
                        return
                    child_relative = os.path.join(relative, name)
                    child = os.path.join(root, child_relative)
                    try:
                        st = call('fs.entry.stat', os.lstat, os.path.join(lookup_root, name), _path=child)
                        count('entries')
                        if any(within(child, excluded) for excluded in SOURCE_EXCLUDES):
                            batch.append(('skipped', child_relative, 'deckard-local-state'))
                        elif st.st_dev != root_dev:
                            count('other_filesystem_skips')
                            batch.append(('skipped', child_relative, 'other-filesystem'))
                        elif stat.S_ISDIR(st.st_mode):
                            batch.append(('directory', child_relative))
                        elif stat.S_ISREG(st.st_mode):
                            blocks = getattr(st, 'st_blocks', None)
                            batch.append(('file', child_relative, fingerprint(st), st.st_nlink,
                                          None if blocks is None else int(blocks) * 512))
                        else:
                            count('special_file_skips')
                            batch.append(('skipped', child_relative,
                                          'symlink' if stat.S_ISLNK(st.st_mode) else 'special'))
                    except OSError as exc:
                        failed = True
                        batch.append(('issue', child_relative, str(exc)))
                    if len(batch) >= 128:
                        if not emit(('batch', directory_id, relative, batch)):
                            return
                        batch = []
            except OSError as exc:
                failed = True
                batch.append(('issue', relative, str(exc)))
            finally:
                if directory_fd is not None:
                    call('fs.directory.close', os.close, directory_fd)
            # The tail batch and completion travel together: most directories
            # fit in one event instead of separate batch/done queue round trips.
            emit(('directory_done', directory_id, failed, relative, batch))
    return work


def inventory_batch_worker(root, root_dev):
    """Portable fallback; the SSH source overrides this with one batched RPC."""
    single = inventory_worker(root, root_dev)
    def work(jobs, emit, stop):
        for job in jobs:
            if stop.is_set():
                return
            single(job, emit, stop)
    return work


def inventory(db, root, args, checkpoint):
    if meta(db, 'phase') != 'inventory' and not args.refresh and not args.retry_errors:
        log('Resuming saved inventory; use --refresh to discover source-tree changes.')
        return
    phase('inventory')
    if args.refresh or meta(db, 'epoch') is None:
        epoch = int(meta(db, 'epoch', '0')) + 1
        setmeta(db, 'epoch', epoch)
        db.execute('DELETE FROM dirs')
        db.execute('DELETE FROM issues')
        db.execute('DELETE FROM skipped')
        setmeta(db, 'report_inventory_epoch', epoch)
        db.execute('INSERT INTO dirs(path,state) VALUES (?,0)', (blob(b''),))
        setmeta(db, 'phase', 'inventory')
        checkpoint.tick(force=True)
        drop_indexes(db, ('files_size', 'files_full'))
    else:
        epoch = int(meta(db, 'epoch'))
        if args.retry_errors:
            db.execute('UPDATE dirs SET state=0 WHERE state=2')
    setmeta(db, 'phase', 'inventory')
    phase('inventory')
    checkpoint.tick(force=True)
    clear_old_issues = db.execute('SELECT 1 FROM issues LIMIT 1').fetchone() is not None
    batch_size = getattr(args, 'inventory_batch', 1)
    if not 1 <= batch_size <= 16:
        raise ValueError('inventory batch must be 1..16')
    batch_work = inventory_batch_worker(root, source_root_stat(root).st_dev)
    def worker(jobs, emit, stop):
        batch_work(jobs, emit, stop)
        if not stop.is_set():
            emit(('inventory_group_done', jobs[0][0]))
    pool = Pool(args.inventory_workers, worker)
    inflight, groups = set(), {}
    max_groups = args.inventory_workers * 2
    try:
        while True:
            # Bound GROUPS independently of directory completions. Otherwise
            # early completions could fill the job queue with tiny groups while
            # workers block on results, deadlocking the database consumer.
            available = []
            if len(groups) < max_groups:
                available = db.execute('SELECT id,path FROM dirs WHERE state=0 ORDER BY id LIMIT ?',
                                       (max_groups * batch_size * 2,)).fetchall()
            candidates = [row for row in available if row['id'] not in inflight]
            capacity = (max_groups - len(groups)) * batch_size
            candidates = candidates[:capacity]
            for offset in range(0, len(candidates), batch_size):
                jobs = [(row['id'], raw(row['path'])) for row in candidates[offset:offset + batch_size]]
                for ident, relative in jobs:
                    if clear_old_issues:
                        db.execute('DELETE FROM issues WHERE parent=?', (blob(relative),))
                    db.execute('DELETE FROM skipped WHERE parent=?', (blob(relative),))
                call('queue.job_put', pool.jobs.put, jobs)
                groups[jobs[0][0]] = set(job[0] for job in jobs)
                inflight.update(groups[jobs[0][0]])
            if PROFILE is not None:
                PROFILE.gauge('inventory_batch_size', batch_size)
                PROFILE.gauge('directory_batches_inflight', len(groups))
                PROFILE.gauge('directories_inflight', len(inflight))
                PROFILE.gauge('jobs_queued', pool.jobs.qsize())
                PROFILE.gauge('results_queued', pool.results.qsize())
            if not groups:
                if inflight:
                    raise RuntimeError('directory/group accounting mismatch')
                break
            try:
                event = call('queue.result_wait', pool.results.get, timeout=.5)
            except queue.Empty:
                checkpoint.tick(0)
                continue
            if event[0] == 'fatal':
                raise_worker_error(event)
            if event[0] == 'inventory_group_done':
                members = groups.pop(event[1])
                if members.intersection(inflight):
                    raise SourceUnavailable('directory batch ended without all completions')
                count('inventory_batches_completed')
                continue
            if event[0] == 'batch':
                _, directory_id, parent, records = event
            else:
                _, directory_id, failed, parent, records = event
            directories = [(blob(record[1]),) for record in records if record[0] == 'directory']
            files = [(record[1], record[2], record[3]) for record in records if record[0] == 'file']
            issues = [(blob(record[1]), blob(parent), record[2]) for record in records if record[0] == 'issue']
            skipped = [(blob(record[1]), blob(parent), record[2]) for record in records if record[0] == 'skipped']
            if directories:
                db.executemany('INSERT OR IGNORE INTO dirs(path,state) VALUES (?,0)', directories)
            files_upsert_many(db, files, epoch)
            allocations = [(record[4], blob(record[1])) for record in records
                           if record[0] == 'file' and len(record) > 4]
            if allocations:
                db.executemany('UPDATE files SET allocated=? WHERE path=?', allocations)
            if skipped:
                db.executemany('INSERT OR REPLACE INTO skipped VALUES (?,?,?)', skipped)
            count('inventory_files', len(files))
            if issues:
                db.executemany('INSERT OR REPLACE INTO issues VALUES (?,?,?)', issues)
            # Commit progress only after a complete bounded batch. Directory
            # completion is written after all its metadata, in the same order.
            checkpoint.tick(len(records))
            if event[0] == 'directory_done':
                db.execute('UPDATE dirs SET state=? WHERE id=?', (2 if failed else 1, directory_id))
                inflight.remove(directory_id)
                count('directories_failed' if failed else 'directories_completed')
                checkpoint.tick()
    finally:
        pool.close()
        checkpoint.tick(force=True)
    phase('inventory.prune')
    db.execute('DELETE FROM files WHERE epoch<>?', (epoch,))
    setmeta(db, 'phase', 'hashes')
    setmeta(db, 'inventory_time', time.time())
    if meta(db, 'report_inventory_epoch') == str(epoch):
        setmeta(db, 'report_inventory_version', '1')
    checkpoint.tick(force=True)


def build_index(db, statement, label):
    # SQLite 3.10 predates large-WAL-transaction improvements (3.11).
    # Bulk index builds use a disk rollback journal; regular batches use WAL.
    name = statement.split(' ON ', 1)[0].split()[-1]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone():
        return
    phase(label)
    db.commit()
    db.execute('PRAGMA journal_mode=DELETE')
    try:
        with span('sql.index_build', detail=statement):
            db.execute(statement)
            db.commit()
    finally:
        db.execute('PRAGMA journal_mode=WAL')


def drop_indexes(db, names):
    existing = [name for name in names if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()]
    if not existing:
        return
    db.commit()
    phase('index.drop')
    db.execute('PRAGMA journal_mode=DELETE')
    try:
        for name in existing:
            db.execute('DROP INDEX ' + name)
        db.commit()
    finally:
        db.execute('PRAGMA journal_mode=WAL')


def representatives(db):
    phase('hashes.representatives')
    db.execute('DROP TABLE IF EXISTS temp.reps')
    db.execute('CREATE TEMP TABLE reps(id INTEGER PRIMARY KEY)')
    db.execute("INSERT INTO reps SELECT id FROM files WHERE nlink<=1 OR ino='0'")
    db.execute('''INSERT INTO reps SELECT MIN(id) FROM files WHERE nlink>1 AND ino<>'0'
               GROUP BY dev,ino,size,mtime,ctime''')
    db.commit()


def prepare_work(db, stage, minimum, sample_min_size=100*1048576):
    phase(stage + '.grouping')
    db.execute('DROP TABLE IF EXISTS temp.work')
    db.execute('CREATE TEMP TABLE work(id INTEGER PRIMARY KEY)')
    previous = None if stage == 'prefix' else 'f.prefix'
    if stage == 'sample':
        minimum = max(minimum, sample_min_size)
    elif stage == 'full':
        # Prefix hashing already reads these files completely. They cannot
        # create pending full jobs, so exclude them from the expensive sort.
        minimum = max(minimum, SAMPLE + 1)
        previous = '(CASE WHEN f.size<%d THEN f.prefix ELSE f.sample END)' % sample_min_size
    keys = 'f.size' + (',' + previous if previous else '')
    selected = 'f.size AS size' + (',' + previous + ' AS digest' if previous else '')
    nonnull = ' AND %s IS NOT NULL' % previous if previous else ''
    join = 'f.size=g.size' + (' AND %s=g.digest' % previous if previous else '')
    statement = '''INSERT INTO work SELECT f.id FROM files f JOIN reps r ON r.id=f.id
        JOIN (SELECT %s FROM files f JOIN reps r ON r.id=f.id
        WHERE f.error IS NULL AND f.size>=? %s GROUP BY %s HAVING COUNT(*)>1) g
        ON %s WHERE f.error IS NULL AND f.%s IS NULL''' % (selected, nonnull, keys, join, stage)
    db.execute(statement, (minimum,))
    db.commit()
    return db.execute('SELECT COUNT(*) FROM work').fetchone()[0]


def read_exact(stream, amount):
    chunks = []
    while amount:
        if CANCEL_EVENT is not None and CANCEL_EVENT.is_set():
            raise SourceUnavailable('scan paused')
        chunk = call('fs.read', stream.read, amount)
        count('bytes_read', len(chunk))
        if not chunk:
            raise OSError('unexpected EOF')
        chunks.append(chunk)
        amount -= len(chunk)
    return b''.join(chunks)


def size_band(size):
    for limit, name in ((65536, 'le64k'), (196608, 'le192k'),
                        (1048576-1, 'lt1m'), (10485760-1, 'lt10m'),
                        (104857600-1, 'lt100m'), (1073741824-1, 'lt1g')):
        if size <= limit:
            return name
    return 'ge1g'


def hash_file(root, row, stage, block):
    timed = PROFILE is not None and PROFILE.enabled
    started, cpu = (NOW(), CPU()) if timed else (None, None)
    label = 'hash_%s_%s_' % (stage, size_band(row['size']))
    try:
        value = read_hash_file(root, row, stage, block)
        full = stage == 'full' or row['size'] <= (SAMPLE if stage == 'prefix' else 3*SAMPLE)
        count(label + 'files')
        count(label + 'candidate_bytes', row['size'])
        count(label + 'read_bytes', row['size'] if full else (SAMPLE if stage == 'prefix' else 2*SAMPLE))
        return value
    except (IOError, OSError):
        count(label + 'errors')
        raise
    finally:
        if timed:
            count(label + 'seconds', NOW() - started)
            count(label + 'cpu_seconds', max(0, CPU() - cpu))


def read_hash_file(root, row, stage, block):
    path = os.path.join(root, raw(row['path']))
    with span('task.hash', path, stage):
        if not hasattr(os, 'O_NOFOLLOW'):
            check(call('fs.file.stat', os.lstat, path), row)
        fd = call('fs.open', os.open, path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                  getattr(os, 'O_NONBLOCK', 0))
        stream = os.fdopen(fd, 'rb')
        try:
            check(call('fs.fstat', os.fstat, stream.fileno()), row)
            size = row['size']
            full = stage == 'full' or size <= (SAMPLE if stage == 'prefix' else 3 * SAMPLE)
            digest = hashlib.sha256()
            if full:
                remaining = size
                while remaining:
                    chunk = read_exact(stream, min(block, remaining))
                    call('cpu.sha256', digest.update, chunk)
                    remaining -= len(chunk)
                if call('fs.eof', stream.read, 1):
                    raise OSError('file grew during read')
            elif stage == 'prefix':
                call('cpu.sha256', digest.update, read_exact(stream, SAMPLE))
            else:
                # Prefix equality was already established. Read only middle/tail.
                digest.update(row['prefix'].encode('ascii'))
                for offset in ((size - SAMPLE) // 2, size - SAMPLE):
                    call('fs.seek', stream.seek, offset)
                    call('cpu.sha256', digest.update, read_exact(stream, SAMPLE))
            check(call('fs.fstat', os.fstat, stream.fileno()), row)
        finally:
            call('fs.close', stream.close)
        check(call('fs.file.stat', os.lstat, path), row)
        value = digest.hexdigest()
        return value, value if full else None


def object_where(row):
    if row['nlink'] > 1 and row['ino'] != '0':
        return 'dev=? AND ino=? AND size=? AND mtime=? AND ctime=? AND nlink>1', (
            row['dev'], row['ino'], row['size'], row['mtime'], row['ctime'])
    return 'id=?', (row['id'],)


def update_object(db, row, assignments, values, checkpoint=None):
    where, parameters = object_where(row)
    if row['nlink'] <= 1 or row['ino'] == '0':
        db.execute('UPDATE files SET ' + assignments + ' WHERE ' + where, values + parameters)
        return
    last = 0
    while True:
        # Keep the representative/job row unchanged until all aliases are saved.
        # A crash after an intermediate commit must leave the job discoverable.
        rows = db.execute('SELECT id FROM files WHERE ' + where + ' AND id>? AND id<>? ORDER BY id LIMIT 256',
                          parameters + (last, row['id'])).fetchall()
        if not rows:
            db.execute('UPDATE files SET ' + assignments + ' WHERE id=?', values + (row['id'],))
            return
        ids = tuple(value[0] for value in rows)
        last = ids[-1]
        db.execute('UPDATE files SET ' + assignments + ' WHERE id IN (' + ','.join('?' for value in ids) + ')',
                   values + ids)
        if checkpoint is not None:
            checkpoint.tick(len(ids))


def save_hash(db, row, stage, digest, full, checkpoint=None):
    update_object(db, row, '%s=?,full=COALESCE(?,full),error=NULL' % stage, (digest, full), checkpoint)


def save_error(db, row, error, checkpoint=None):
    update_object(db, row, 'prefix=NULL,sample=NULL,full=NULL,error=?', (str(error),), checkpoint)
    count('hash_errors')


def reuse_small_hashes(db, stage, checkpoint):
    """Copy cached full hashes in bounded transactions, without per-file jobs.

    Hard-linked representatives retain the alias propagation/recovery path.
    No cached hash is trusted beyond the existing metadata/report checks.
    """
    if stage == 'full':
        return 0
    phase(stage + '.cache_reuse')
    limit = SAMPLE if stage == 'prefix' else 3 * SAMPLE
    last, reused = 0, 0
    while True:
        rows = db.execute('''SELECT f.id FROM work w CROSS JOIN files f ON w.id=f.id
            WHERE w.id>? AND f.full IS NOT NULL AND f.size<=?
            AND (f.nlink<=1 OR f.ino='0') ORDER BY w.id LIMIT 256''', (last, limit)).fetchall()
        if not rows:
            return reused
        ids = tuple(row[0] for row in rows)
        last = ids[-1]
        placeholders = ','.join('?' for value in ids)
        db.execute('UPDATE files SET ' + stage + '=full WHERE id IN (' + placeholders + ')', ids)
        db.execute('DELETE FROM work WHERE id IN (' + placeholders + ')', ids)
        reused += len(ids)
        count('small_hash_reuse', len(ids))
        count(stage + '_completed', len(ids))
        checkpoint.tick(len(ids))


def local_hash_batch_worker(root, stage, block):
    reader = hash_file
    def work(rows, emit, stop):
        for row in rows:
            if stop.is_set():
                return
            try:
                value, full = reader(root, row, stage, block)
                event = ('hash_done', row, value, full, None)
            except (IOError, OSError) as exc:
                event = ('hash_done', row, None, None, str(exc))
            if not emit(event):
                return
    return work


hash_batch_worker = local_hash_batch_worker


def hash_job_groups(rows, stage, batch_size, byte_budget):
    """Bound RPC work by both file count and source bytes; large reads stay solo."""
    groups, group, total = [], [], 0
    for row in rows:
        amount = row['size'] if stage == 'full' else min(row['size'], SAMPLE if stage == 'prefix' else 3*SAMPLE)
        if group and (len(group) >= batch_size or total + amount > byte_budget):
            groups.append(group)
            group, total = [], 0
        group.append(row)
        total += amount
        if amount > byte_budget:
            groups.append(group)
            group, total = [], 0
    if group:
        groups.append(group)
    return groups


def hash_stage(db, root, args, stage, checkpoint):
    total = prepare_work(db, stage, args.min_size, args.sample_min_size)
    if PROFILE is not None:
        PROFILE.gauge(stage + '_pending_at_start', total)
    log('%s: %s distinct file objects pending' % (stage, total))
    if not total:
        return
    reused = reuse_small_hashes(db, stage, checkpoint)
    if reused == total:
        checkpoint.tick(force=True)
        return
    phase(stage + '.hash')
    batch_size = args.prefix_hash_batch if stage == 'prefix' else args.hash_batch
    if not 1 <= batch_size <= 16:
        raise ValueError('prefix hash batch must be 1..16')
    batch_work = hash_batch_worker(root, stage, args.read_block)
    def worker(rows, emit, stop):
        batch_work(rows, emit, stop)
        if not stop.is_set():
            emit(('hash_group_done', rows[0]['id']))
    pool = Pool(args.hash_workers, worker)
    inflight, groups, last_id, exhausted = set(), {}, 0, False
    pending = []
    max_groups = args.hash_workers * 2
    try:
        while groups or pending or not exhausted:
            capacity = max_groups * batch_size
            if not pending and not exhausted:
                # Drive the bounded fetch from pending IDs, avoiding a scan
                # through already-processed file IDs on resume or sparse work.
                rows = db.execute('''SELECT f.* FROM work w CROSS JOIN files f ON w.id=f.id
                                  WHERE w.id>? ORDER BY w.id LIMIT ?''', (last_id, capacity)).fetchall()
                if not rows:
                    exhausted = True
                jobs = []
                for row in rows:
                    row = dict(row)
                    last_id = row['id']
                    limit = SAMPLE if stage == 'prefix' else 3 * SAMPLE
                    if row['full'] is not None and (stage == 'full' or row['size'] <= limit):
                        save_hash(db, row, stage, row['full'], row['full'], checkpoint)
                        count('small_hash_reuse')
                        count(stage + '_completed')
                        checkpoint.tick()
                    else:
                        jobs.append(row)
                pending = hash_job_groups(jobs, stage, batch_size, args.hash_batch_bytes)
            while pending and len(groups) < max_groups:
                group = pending.pop(0)
                call('queue.job_put', pool.jobs.put, group)
                groups[group[0]['id']] = set(row['id'] for row in group)
                inflight.update(groups[group[0]['id']])
                if PROFILE is not None:
                    PROFILE.gauge('hash_batch_size', len(group))
            if PROFILE is not None:
                PROFILE.gauge('hash_batch_limit', batch_size)
                PROFILE.gauge('hash_batches_inflight', len(groups))
                PROFILE.gauge('hash_inflight', len(inflight))
                PROFILE.gauge('jobs_queued', pool.jobs.qsize())
                PROFILE.gauge('results_queued', pool.results.qsize())
            if not groups:
                if inflight:
                    raise RuntimeError('hash/group accounting mismatch')
                continue
            try:
                event = call('queue.result_wait', pool.results.get, timeout=.5)
            except queue.Empty:
                checkpoint.tick(0)
                continue
            if event[0] == 'fatal':
                raise_worker_error(event)
            if event[0] == 'hash_group_done':
                members = groups.pop(event[1])
                if members.intersection(inflight):
                    raise SourceUnavailable('hash batch ended without all results')
                count(stage + '_batches_completed')
                continue
            _, row, value, full, error = event
            if row['id'] not in inflight:
                raise RuntimeError('unexpected or repeated hash result')
            if error is None:
                save_hash(db, row, stage, value, full, checkpoint)
                count('hashes_completed')
            else:
                save_error(db, row, error, checkpoint)
                log('Hash error: %r: %s' % (display(row['path']), error))
            inflight.remove(row['id'])
            count(stage + '_completed')
            checkpoint.tick()
    finally:
        pool.close()
        checkpoint.tick(force=True)


def candidate_groups(db, minimum=0):
    return db.execute('''SELECT f.size,f.full,COUNT(*) objects FROM files f JOIN reps r ON r.id=f.id
        WHERE f.full IS NOT NULL AND f.error IS NULL AND f.size>=?
        GROUP BY f.size,f.full HAVING COUNT(*)>1''', (minimum,))


def write_json(stream, row):
    call('report.write', stream.write, json.dumps(row, ensure_ascii=True) + '\n')


def validate_report_rows(root, rows):
    for row in rows:
        error = None
        path = os.path.join(root, raw(row['path']))
        try:
            check(call('fs.report.stat', os.lstat, path, _path=path), row)
        except OSError as exc:
            error = display(str(exc))
        yield row, error


def validate_candidates(db, root, args, checkpoint):
    def worker(rows, emit, stop):
        values = list(validate_report_rows(root, rows))
        if not stop.is_set():
            emit(('validated', values))
    pool = Pool(args.validation_workers, worker)
    inflight, last, exhausted = 0, 0, False
    try:
        while inflight or not exhausted:
            while not exhausted and inflight < 2*args.validation_workers:
                rows = db.execute('''SELECT f.id,f.path,f.size,f.mtime,f.ctime,f.dev,f.ino,f.nlink FROM files f JOIN report_keys k ON k.size=f.size AND k.digest=f.full
                    WHERE f.id>? AND f.error IS NULL ORDER BY f.id LIMIT ?''', (last, args.validation_batch)).fetchall()
                if not rows:
                    exhausted = True
                    break
                # Copy SQLite rows before handing ownership to an I/O thread.
                rows = [dict(row) for row in rows]
                last = rows[-1]['id']
                call('queue.job_put', pool.jobs.put, rows)
                inflight += 1
            if PROFILE is not None:
                PROFILE.gauge('validation_batches_inflight', inflight)
                PROFILE.gauge('validation_batch_size', args.validation_batch)
                PROFILE.gauge('jobs_queued', pool.jobs.qsize())
                PROFILE.gauge('results_queued', pool.results.qsize())
            if not inflight:
                continue
            try:
                event = call('queue.result_wait', pool.results.get, timeout=.5)
            except queue.Empty:
                checkpoint.tick(0)
                continue
            if event[0] == 'fatal':
                raise_worker_error(event)
            for row, error in event[1]:
                if error is not None:
                    save_error(db, row, error, checkpoint)
                count('report_validations')
                checkpoint.tick()
            count('validation_batches_completed')
            inflight -= 1
    finally:
        pool.close()
        checkpoint.tick(force=True)


def report(db, root, state, args, checkpoint):
    phase('report.candidates')
    db.execute('DROP TABLE IF EXISTS temp.report_keys')
    db.execute('CREATE TEMP TABLE report_keys(size INTEGER,digest TEXT,PRIMARY KEY(size,digest))')
    for row in candidate_groups(db, args.min_size):
        db.execute('INSERT INTO report_keys VALUES (?,?)', (row['size'], row['full']))
    db.commit()
    phase('report.validate')
    validate_candidates(db, root, args, checkpoint)
    phase('report.write')
    destination = os.path.join(state, b'duplicates.jsonl')
    temporary = destination + b'.partial'
    groups = savings = paths = 0
    with open(temporary, 'w') as output:
        write_json(output, {'type': 'scan', 'root': display(root), 'root_base64': b64(root),
                            'inventory_time_unix': float(meta(db, 'inventory_time')),
                            'report_time_unix': time.time(), 'algorithm': 'SHA-256',
                            'min_size': args.min_size, 'scope': 'regular-file data'})
        for group in candidate_groups(db, args.min_size):
            groups += 1
            members = db.execute('SELECT COUNT(*) FROM files WHERE size=? AND full=? AND error IS NULL',
                                 (group['size'], group['full'])).fetchone()[0]
            logical = (group['objects'] - 1) * group['size']
            header = {'type': 'duplicate_group', 'group': groups, 'size': group['size'],
                      'sha256': group['full'], 'path_count': members, 'distinct_file_count': group['objects'],
                      'potential_reclaimable_logical_bytes': logical}
            call('report.write', output.write, json.dumps(header)[:-1] + ',"paths":[')
            first = True
            for row in db.execute('SELECT path FROM files WHERE size=? AND full=? AND error IS NULL ORDER BY id',
                                  (group['size'], group['full'])):
                path = os.path.join(root, raw(row['path']))
                member = {'path': display(path), 'path_base64': b64(path)}
                call('report.write', output.write, ('' if first else ',') + json.dumps(member))
                first = False
            call('report.write', output.write, ']}\n')
            paths += members
            savings += logical
        issue_count = db.execute('SELECT COUNT(*) FROM issues').fetchone()[0]
        file_errors = db.execute('SELECT COUNT(*) FROM files WHERE error IS NOT NULL').fetchone()[0]
        for row in db.execute('SELECT path,message FROM issues'):
            write_json(output, {'type': 'inventory_error', 'path': display(row['path']),
                                'relative_path_base64': b64(row['path']), 'message': row['message']})
        for row in db.execute('SELECT path,error FROM files WHERE error IS NOT NULL'):
            write_json(output, {'type': 'file_error', 'path': display(row['path']),
                                'relative_path_base64': b64(row['path']), 'message': row['error']})
        summary = {'type': 'summary', 'groups': groups, 'paths': paths,
                   'potential_reclaimable_logical_bytes': savings,
                   'inventory_errors': issue_count, 'file_errors': file_errors,
                   'complete': not (issue_count or file_errors), 'min_size': args.min_size}
        write_json(output, summary)
        call('report.flush', output.flush)
        call('report.fsync', os.fsync, output.fileno())
    call('report.rename', os.rename, temporary, destination)
    setmeta(db, 'phase', 'complete')
    checkpoint.tick(force=True)
    log('Report: %s; %s groups; complete=%s' % (display(destination), groups, summary['complete']))
    return 0 if summary['complete'] else 2


def scan(root, state, args):
    db = connect(os.path.join(state, b'index.sqlite3'), args.sqlite_cache_mib, state)
    try:
        if meta(db, 'schema', SCHEMA) != SCHEMA:
            raise ValueError('incompatible index; choose a new state directory')
        if meta(db, 'root', b64(root)) != b64(root):
            raise ValueError('index belongs to another root')
        device = str(source_root_stat(root).st_dev)
        if meta(db, 'device', device) != device and not args.refresh:
            raise ValueError('filesystem changed; verify the source mount, then use --refresh')
        setmeta(db, 'schema', SCHEMA)
        setmeta(db, 'implementation', IMPLEMENTATION)
        setmeta(db, 'root', b64(root))
        setmeta(db, 'device', device)
        if meta(db, 'phase') is None:
            setmeta(db, 'phase', 'inventory')
        # Avoid hash-index maintenance during large batches; keep it on a
        # completed unchanged resume. Old SQLite uses disk journals for DDL.
        if (meta(db, 'phase') != 'complete' or args.refresh or args.rehash or args.retry_errors or
                meta(db, 'last_min_size') != str(args.min_size)):
            drop_indexes(db, ('files_full',))
        setmeta(db, 'last_min_size', args.min_size)
        if args.retry_errors:
            db.execute('UPDATE files SET error=NULL WHERE error IS NOT NULL')
        if args.rehash:
            db.execute('UPDATE files SET prefix=NULL,sample=NULL,full=NULL,error=NULL')
        checkpoint = Checkpoint(db, args.commit_rows, args.commit_seconds)
        checkpoint.tick(force=True)
        inventory(db, root, args, checkpoint)
        errors = db.execute('SELECT COUNT(*) FROM issues').fetchone()[0]
        if errors:
            log('%s inventory errors retained; readable files will still be processed.' % errors)
        if args.inventory_only or (args.strict and errors):
            return 2 if errors else 0
        build_index(db, 'CREATE INDEX IF NOT EXISTS files_size ON files(size)', 'index.size')
        representatives(db)
        for stage in ('prefix', 'sample', 'full'):
            hash_stage(db, root, args, stage, checkpoint)
        build_index(db, 'CREATE INDEX IF NOT EXISTS files_full ON files(size,full)', 'index.report')
        return report(db, root, state, args, checkpoint)
    finally:
        db.close()


def probe_directory(root, relative):
    started = NOW()
    path = os.path.join(root, relative)
    result = {'path_base64': b64(relative), 'entries': 0, 'error': None}
    try:
        st = call('probe.directory.stat', os.lstat, path, _path=path)
        if not stat.S_ISDIR(st.st_mode):
            raise OSError('not a directory')
        names = call('probe.listdir', os.listdir, path, _path=path)
        result['listed_entries'] = len(names)
        result['truncated'] = len(names) > 128
        for name in names[:128]:
            child = os.path.join(path, name)
            call('probe.entry.stat', os.lstat, child, _path=child)
            count('entries')
            result['entries'] += 1
    except OSError as exc:
        result['error'] = str(exc)
    result['seconds'] = NOW() - started
    return result


def parallel_map(function, jobs, workers):
    pool = Pool(workers, lambda job, emit, stop: emit(('result', function(job))))
    outstanding, position, results = 0, 0, []
    try:
        while position < len(jobs) or outstanding:
            while position < len(jobs) and outstanding < workers * 2:
                call('queue.job_put', pool.jobs.put, jobs[position])
                position += 1
                outstanding += 1
            try:
                row = call('queue.result_wait', pool.results.get, timeout=.5)
            except queue.Empty:
                continue
            if row[0] == 'fatal':
                raise_worker_error(row)
            results.append(row[1])
            outstanding -= 1
        return results
    finally:
        pool.close()


def probe_hash_file(root, relative, limit, block):
    path = os.path.join(root, relative)
    st = os.lstat(path)
    row = dict(zip(('size','mtime','ctime','dev','ino'), fingerprint(st)))
    digest, amount = hashlib.sha256(), 0
    with span('task.probe_hash', path):
        fd = call('probe.open', os.open, path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            check(os.fstat(stream.fileno()), row)
            while amount < limit:
                data = call('probe.read', stream.read, min(block, limit - amount))
                if not data:
                    break
                count('bytes_read', len(data))
                call('probe.sha256', digest.update, data)
                amount += len(data)
            check(os.fstat(stream.fileno()), row)
        check(os.lstat(path), row)
    return {'bytes': amount, 'digest': digest.hexdigest()}


def choose_probe_file(root, value):
    path = raw(os.path.realpath(os.path.expanduser(value)))
    if not within(path, root):
        raise ValueError('--probe-file must be inside source root')
    return os.path.relpath(path, root), os.lstat(path).st_size


def probes(root, state, args):
    phase('probe.select_samples')
    index = os.path.join(state, b'index.sqlite3')
    directories, large = [], []
    if os.path.isfile(index):
        db = sqlite3.connect(display(index), timeout=2)
        try:
            db.execute('PRAGMA query_only=ON')
            if meta(db, 'root') != b64(root):
                raise ValueError('probe index belongs to another root')
            directories = [raw(row[0]) for row in db.execute('SELECT path FROM dirs WHERE state=1 LIMIT 4096')]
            large = [(raw(row[0]), row[1]) for row in db.execute('SELECT path,size FROM files WHERE size>=? LIMIT 64',
                                                              (args.probe_read_mib * 1048576,))]
        finally:
            db.close()
    if not directories:
        directories = [b'']
    random.Random(471).shuffle(directories)
    samples = directories[:args.probe_directories]
    output = {'type': 'probe_results', 'root': display(root), 'inventory': [], 'hashing': [],
              'settings': {'timings': not args.no_timings, 'sqlite_cache_mib': args.sqlite_cache_mib,
                           'timing_sample': args.timing_sample,
                           'commit_rows': args.commit_rows, 'read_budget_per_case_mib': args.probe_read_mib},
              'note': 'Bounded samples, shared warm caches, fixed case order. Rates are not whole-scan predictions. Production index is not modified.'}
    for workers in args.probe_workers:
        phase('probe.inventory.%s' % workers)
        started = NOW()
        rows = parallel_map(lambda path: probe_directory(root, path), samples, workers)
        elapsed = NOW() - started
        result = {'workers': workers, 'directories': len(rows), 'seconds': elapsed,
                  'entries': sum(row['entries'] for row in rows), 'errors': sum(row['error'] is not None for row in rows),
                  'service_seconds': sum(row['seconds'] for row in rows), 'details': rows}
        output['inventory'].append(result)
        log('Probe directory workers=%s: %.3fs for %s entries' % (workers, elapsed, result['entries']))
    if args.probe_file:
        large = [choose_probe_file(root, args.probe_file)]
    if large:
        for workers in args.probe_workers:
            for block in args.probe_blocks:
                phase('probe.hash.%s.%s' % (workers, block))
                # Fixed aggregate read budget per configuration, split among jobs.
                budget = args.probe_read_mib * 1048576
                jobs = [(large[i % len(large)][0], max(1, budget // workers)) for i in range(workers)]
                def hash_prefix(job):
                    return probe_hash_file(root, job[0], job[1], block)
                started, cpu = NOW(), CPU()
                records = parallel_map(hash_prefix, jobs, workers)
                elapsed = NOW() - started
                result = {'workers': workers, 'block_bytes': block, 'bytes': sum(row['bytes'] for row in records),
                          'seconds': elapsed, 'cpu_seconds': CPU() - cpu}
                result['mib_per_second'] = result['bytes'] / 1048576. / elapsed
                output['hashing'].append(result)
                log('Probe hashing workers=%s block=%s: %.2fMiB/s' % (workers, block, result['mib_per_second']))
    phase('probe.sqlite')
    scratch = os.path.join(state, raw('probe-sqlite-%s.sqlite3' % os.getpid()))
    if os.path.exists(scratch):
        raise ValueError('scratch probe database already exists')
    db = None
    try:
        db = sqlite3.connect(display(scratch))
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=NORMAL')
        db.execute('PRAGMA cache_size=-%d' % (args.sqlite_cache_mib * 1024))
        db.execute('PRAGMA temp_store=FILE')
        db.execute("PRAGMA temp_store_directory='%s'" % display(state).replace("'", "''"))
        db.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY,path BLOB,size INTEGER,digest TEXT)')
        started = NOW()
        committed = 0
        for i in range(args.probe_rows):
            db.execute('INSERT INTO sample VALUES (?,?,?,NULL)', (i, blob(raw('synthetic/%s' % i)), i % 4096))
            if (i + 1) % args.commit_rows == 0:
                db.commit()
                committed += 1
        db.commit()
        insert_seconds = NOW() - started
        started = NOW()
        db.execute('CREATE INDEX sizes ON sample(size)')
        db.commit()
        index_seconds = NOW() - started
        started = NOW()
        for i in range(args.probe_rows):
            db.execute('UPDATE sample SET digest=? WHERE id=?', ('a' * 64, i))
            if (i + 1) % args.commit_rows == 0:
                db.commit()
        db.commit()
        output['sqlite'] = {'rows': args.probe_rows, 'commit_rows': args.commit_rows,
                            'insert_seconds': insert_seconds, 'index_seconds': index_seconds,
                            'update_seconds': NOW() - started}
        phase('probe.sqlite.batched')
        db.execute('CREATE TABLE sample_batch(id INTEGER PRIMARY KEY,path BLOB,size INTEGER,digest TEXT)')
        started, cpu, pending = NOW(), CPU(), 0
        for offset in range(0, args.probe_rows, 128):
            rows = [(i, blob(raw('synthetic/%s' % i)), i % 4096)
                    for i in range(offset, min(offset + 128, args.probe_rows))]
            db.executemany('INSERT INTO sample_batch VALUES (?,?,?,NULL)', rows)
            pending += len(rows)
            if pending >= args.commit_rows:
                db.commit()
                pending = 0
        db.commit()
        output['sqlite']['batched_insert_seconds'] = NOW() - started
        output['sqlite']['batched_insert_cpu_seconds'] = CPU() - cpu
        started = NOW()
        db.execute('CREATE INDEX batch_sizes ON sample_batch(size)')
        db.commit()
        output['sqlite']['batched_index_seconds'] = NOW() - started
        started, cpu, pending = NOW(), CPU(), 0
        for offset in range(0, args.probe_rows, 128):
            rows = [('a' * 64, i) for i in range(offset, min(offset + 128, args.probe_rows))]
            db.executemany('UPDATE sample_batch SET digest=? WHERE id=?', rows)
            pending += len(rows)
            if pending >= args.commit_rows:
                db.commit()
                pending = 0
        db.commit()
        output['sqlite']['batched_update_seconds'] = NOW() - started
        output['sqlite']['batched_update_cpu_seconds'] = CPU() - cpu
        output['sqlite']['batch_rows'] = 128
    finally:
        if db is not None:
            db.close()
        for suffix in (b'', b'-wal', b'-shm', b'-journal'):
            with ignored_io():
                os.unlink(scratch + suffix)
    destination = os.path.join(state, raw('probe-%s-%s.json' % (int(time.time()), os.getpid())))
    with open(destination, 'w') as stream:
        stream.write(json.dumps(output, indent=2))
    log('Probe results: ' + display(destination))
    return 0


def within(path, root):
    return path == root or path.startswith(root.rstrip(os.sep.encode('ascii')) + os.sep.encode('ascii'))


def network_filesystem(path):
    if not sys.platform.startswith('linux'):
        return False
    matches = []
    with open('/proc/mounts') as stream:
        for line in stream:
            fields = line.split()
            mount = raw(fields[1].replace('\\040', ' ').replace('\\134', '\\'))
            if within(path, mount):
                matches.append((len(mount), fields[2]))
    return bool(matches and max(matches)[1] in ('nfs', 'nfs4', 'cifs', 'smbfs', 'fuse.sshfs'))


def number_list(value):
    try:
        numbers = [int(item) for item in value.split(',')]
    except ValueError:
        raise argparse.ArgumentTypeError('expected comma-separated integers')
    if not numbers or any(item <= 0 for item in numbers):
        raise argparse.ArgumentTypeError('values must be positive')
    return numbers


def main(argv=None):
    global PROFILE
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['scan', 'probe', 'status'])
    parser.add_argument('root')
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--inventory-workers', type=int, default=2)
    parser.add_argument('--inventory-batch', type=int, default=1)
    parser.add_argument('--hash-workers', type=int, default=2)
    parser.add_argument('--prefix-hash-batch', type=int, default=1)
    parser.add_argument('--sample-min-size', type=int, default=100*1048576)
    parser.add_argument('--hash-batch', type=int, default=16)
    parser.add_argument('--hash-batch-bytes', type=int, default=8*1048576)
    parser.add_argument('--validation-workers', type=int, default=2)
    parser.add_argument('--validation-batch', type=int, default=512)
    parser.add_argument('--read-block', type=int, default=1048576)
    parser.add_argument('--sqlite-cache-mib', type=int, default=8)
    parser.add_argument('--commit-rows', type=int, default=1000)
    parser.add_argument('--commit-seconds', type=float, default=2)
    parser.add_argument('--heartbeat', type=float, default=10)
    parser.add_argument('--no-timings', action='store_true', help='keep heartbeats/counters, disable call timings')
    parser.add_argument('--timing-sample', type=int, default=16,
                        help='time every Nth frequent call; default 16; use 1 for full tracing')
    parser.add_argument('--refresh', action='store_true', help='refresh entire inventory, retaining valid hashes')
    parser.add_argument('--rehash', action='store_true')
    parser.add_argument('--retry-errors', action='store_true')
    parser.add_argument('--strict', action='store_true')
    parser.add_argument('--inventory-only', action='store_true')
    parser.add_argument('--min-size', type=int, default=0, help='explicit scope limit in bytes; default includes all sizes')
    parser.add_argument('--probe-workers', type=number_list, default=[1, 2, 4])
    parser.add_argument('--probe-blocks', type=number_list, default=[65536, 1048576])
    parser.add_argument('--probe-directories', type=int, default=32)
    parser.add_argument('--probe-read-mib', type=int, default=16)
    parser.add_argument('--probe-rows', type=int, default=10000)
    parser.add_argument('--probe-file', help='optional existing regular file inside ROOT for bounded read probes')
    args = parser.parse_args(argv)
    if not (1 <= args.inventory_workers <= 8 and 1 <= args.hash_workers <= 8):
        parser.error('worker counts must be between 1 and 8')
    if not (0 <= args.sample_min_size and 1 <= args.hash_batch <= 16 and
            196608 <= args.hash_batch_bytes <= 64*1048576 and
            1 <= args.validation_workers <= 8 and 1 <= args.validation_batch <= 1024):
        parser.error('invalid sampling, hashing or validation bounds')
    if not 1 <= args.inventory_batch <= 16:
        parser.error('inventory batch must be 1..16')
    if not 1 <= args.prefix_hash_batch <= 16:
        parser.error('prefix hash batch must be 1..16')
    if any(value < 1 for value in (args.commit_rows, args.sqlite_cache_mib, args.probe_rows,
                                  args.probe_directories, args.probe_read_mib, args.timing_sample)):
        parser.error('batch, cache and probe sizes must be positive')
    if args.commit_rows > 10000 or args.sqlite_cache_mib > 64 or args.probe_rows > 100000:
        parser.error('maximums: 10000 commit rows, 64MiB SQLite cache, 100000 scratch probe rows')
    if not (4096 <= args.read_block <= 8 * 1048576) or args.min_size < 0:
        parser.error('read block must be 4KiB..8MiB and minimum size must be nonnegative')
    if not (0 < args.heartbeat < float('inf') and 0 < args.commit_seconds < float('inf')):
        parser.error('intervals must be finite and positive')
    if max(args.probe_workers) > 8 or min(args.probe_blocks) < 4096 or max(args.probe_blocks) > 8 * 1048576:
        parser.error('probe workers must be <=8; blocks must be 4KiB..8MiB')
    root = raw(os.path.realpath(os.path.expanduser(args.root)))
    state = raw(os.path.realpath(os.path.expanduser(args.state_dir)))
    if not os.path.isdir(root):
        parser.error('source root must be an existing local directory')
    if within(state, root) or within(root, state):
        parser.error('source and state directories must be disjoint')
    ancestor = state
    while not os.path.exists(ancestor):
        ancestor = os.path.dirname(ancestor)
    if network_filesystem(root) or network_filesystem(ancestor):
        parser.error('run on the NAS using local source and state paths')
    if not os.path.isdir(state):
        os.makedirs(state, 0o700)
    # SQLite uses this environment on initialization; the per-connection PRAGMA
    # also protects old SQLite versions initialized earlier by another library.
    os.environ['SQLITE_TMPDIR'] = display(state)
    os.environ['TMPDIR'] = display(state)
    lock = open(os.path.join(state, b'scan.lock'), 'a')
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            parser.error('scanner/probe is already using this state directory; stop it before probing')
        if args.command == 'status':
            index = os.path.join(state, b'index.sqlite3')
            if not os.path.isfile(index):
                parser.error('no index yet')
            db = sqlite3.connect(display(index))
            try:
                db.execute('PRAGMA query_only=ON')
                print(json.dumps(dict(db.execute('SELECT key,value FROM meta')), indent=2))
            finally:
                db.close()
            return 0
        PROFILE = Profile(state, args.heartbeat, not args.no_timings, args.timing_sample)
        PROFILE.start()
        PROFILE.emit({'type': 'configuration', 'root': display(root), 'root_base64': b64(root),
                      'command': args.command, 'inventory_workers': args.inventory_workers,
                      'hash_workers': args.hash_workers, 'read_block': args.read_block,
                      'sqlite_cache_mib': args.sqlite_cache_mib, 'commit_rows': args.commit_rows,
                      'commit_seconds': args.commit_seconds, 'min_size': args.min_size,
                      'timing_sample': args.timing_sample,
                      'schema': SCHEMA, 'implementation': IMPLEMENTATION})
        try:
            if args.command == 'probe':
                return probes(root, state, args)
            return scan(root, state, args)
        finally:
            PROFILE.close()
            PROFILE = None
    finally:
        lock.close()


if __name__ == '__main__':
    raise SystemExit('Use controller.py; duplicate_core.py is a shared library.')
