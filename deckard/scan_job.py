"""Isolated, resumable scan process; Qt is deliberately not imported here."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from . import engine
from .remote import AgentlessSource
from .ssh import SSHTransport
from .storage import atomic_json, data_home
from .report.model import build


OUTPUT_LOCK = threading.Lock()


def publish(event, **values):
    with OUTPUT_LOCK:
        print(json.dumps(dict(values, event=event), ensure_ascii=True), flush=True)


class Progress(engine.Profile):
    def emit(self, record):
        super().emit(record)
        if record.get('type') in ('phase', 'heartbeat', 'final'):
            publish('progress', record=record)


def settings(refresh=False, retry=False, workers=2):
    return SimpleNamespace(refresh=refresh, retry_errors=retry, rehash=False, strict=False,
                           inventory_only=False, min_size=0, sqlite_cache_mib=256,
                           inventory_workers=workers, inventory_batch=16, hash_workers=workers,
                           prefix_hash_batch=16, sample_min_size=100 * 1048576,
                           hash_batch=16, hash_batch_bytes=8 * 1048576,
                           validation_workers=workers, validation_batch=512,
                           read_block=1048576, commit_rows=1000, commit_seconds=2)


def run(folder, refresh=False):
    folder = Path(folder).resolve()
    config = json.loads((folder / 'job.json').read_text())
    started = time.monotonic()
    cancelled = threading.Event()
    engine.CANCEL_EVENT = cancelled
    signal.signal(signal.SIGTERM, lambda *_: cancelled.set())
    signal.signal(signal.SIGINT, lambda *_: cancelled.set())
    engine.log = lambda message: publish('log', message=message)
    original_call = engine.call
    original_connect = engine.connect
    original_commit = engine.Connection.commit

    def call(name, function, *args, **kwargs):
        if cancelled.is_set() and name in ('queue.result_wait', 'queue.job_put'):
            raise KeyboardInterrupt()
        return original_call(name, function, *args, **kwargs)

    def connect(*args, **kwargs):
        db = original_connect(*args, **kwargs)
        db.set_progress_handler(lambda: int(cancelled.is_set()), 10000)
        return db

    def commit(db):
        # An interrupted query must not prevent checkpoint cleanup.
        db.set_progress_handler(None, 0)
        try:
            return original_commit(db)
        finally:
            db.set_progress_handler(lambda: int(cancelled.is_set()), 10000)

    engine.call, engine.connect, engine.Connection.commit = call, connect, commit
    profile, source, originals, awake = None, None, None, None
    with (folder / 'scan.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            publish('failed', message='This scan is already running in another process.', busy=True)
            return 2
        try:
            config.update(status='running', pid=os.getpid(), phase='Connecting' if config['source']['kind'] == 'ssh' else 'Preparing')
            atomic_json(folder / 'job.json', config)
            if sys.platform == 'darwin':
                awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            profile = Progress(engine.raw(str(folder)), 2, True, 16)
            engine.PROFILE = profile
            profile.start()
            target = config['source']
            args = settings(refresh=refresh, retry=True, workers=int(target.get('workers', 2)))
            if refresh:
                config['scan_complete'] = False
            if not config.get('scan_complete'):
                for attempt in range(3):
                    try:
                        if cancelled.is_set():
                            raise KeyboardInterrupt()
                        publish('phase', phase='Connecting' if target['kind'] == 'ssh' else 'Scanning')
                        if target['kind'] == 'ssh':
                            transport = SSHTransport(target['host'], target.get('user', ''),
                                                     target.get('port', 0), target.get('identity', ''), cancelled)
                            source = AgentlessSource(target['root'], transport)
                            identity = [source.info['device'], source.info['inode']]
                            root = source.root
                            originals = source.install()
                        else:
                            path = Path(target['root']).expanduser().resolve(strict=True)
                            if not path.is_dir():
                                raise ValueError('Select an existing folder.')
                            root = engine.raw(str(path))
                            if engine.within(root, engine.raw(str(folder.parent.parent.resolve()))):
                                raise ValueError("Choose a source outside Deckard's own scan-data folder.")
                            st = os.stat(root)
                            identity = [str(st.st_dev), str(st.st_ino)]
                            # Scanning a home folder must exclude Deckard's own
                            # growing checkpoints and report output.
                            engine.SOURCE_EXCLUDES = (engine.raw(str(folder.parent.parent.resolve())),
                                                      engine.raw(str(data_home().resolve())))
                        if config.get('identity') not in (None, identity) and not refresh:
                            raise ValueError('The source folder was replaced. Start a new scan or refresh its inventory.')
                        config['identity'] = identity
                        atomic_json(folder / 'job.json', config)
                        code = engine.scan(root, engine.raw(str(folder)), args)
                        config['scan_complete'] = True
                        config['scan_exit_code'] = code
                        atomic_json(folder / 'job.json', config)
                        break
                    except engine.SourceUnavailable:
                        if cancelled.is_set():
                            raise KeyboardInterrupt()
                        if attempt == 2:
                            raise
                        publish('log', message='Connection interrupted; reconnecting from saved checkpoints…')
                        args.refresh = False
                        if cancelled.wait(2 * (attempt + 1)):
                            raise KeyboardInterrupt()
                    finally:
                        if originals is not None:
                            AgentlessSource.restore(originals); originals = None
                        if source is not None:
                            source.close(); source = None
            if cancelled.is_set():
                raise KeyboardInterrupt()
            publish('phase', phase='Building the duplicate map')
            engine.phase('visualizer.build')
            config.update(status='reporting', phase='Building the duplicate map')
            atomic_json(folder / 'job.json', config)
            report = folder / 'report'
            # Only this job owns this output while its exclusive lock is held.
            # Recover temporary exports left by a force-closed application.
            if report.exists():
                for p in report.glob('*.building'):
                    p.unlink()
            def progress(message):
                if cancelled.is_set():
                    raise KeyboardInterrupt()
                phases = {'Loading directory hierarchy': 'hierarchy',
                          'Streaming file contents and allocated sizes': 'export',
                          'Aggregating folder coverage': 'coverage',
                          'Indexing report data': 'indexes',
                          'Preparing the initial folder matrix': 'matrix'}
                if message in phases:
                    engine.phase('visualizer.' + phases[message])
                publish('log', message=message)
            with engine.span('visualizer.build'):
                manifest = build(folder / 'index.sqlite3', report, progress=progress)
            if cancelled.is_set():
                raise KeyboardInterrupt()
            engine.phase('complete')
            config.update(status='complete', phase='Complete', report_ready=True,
                          summary=manifest, error=None, elapsed_seconds=time.monotonic() - started)
            atomic_json(folder / 'job.json', config)
            publish('complete', report=str(report), summary=manifest)
            return 0
        except BaseException as exc:
            if cancelled.is_set() or isinstance(exc, KeyboardInterrupt):
                config.update(status='paused', phase='Paused', error=None)
                publish('paused')
                code = 130
            else:
                config.update(status='error', phase='Could not finish', error=str(exc))
                publish('failed', message=str(exc))
                code = 2
            config['elapsed_seconds'] = time.monotonic() - started
            atomic_json(folder / 'job.json', config)
            return code
        finally:
            if originals is not None:
                AgentlessSource.restore(originals)
            if source is not None:
                source.close()
            if profile is not None:
                profile.close()
            if awake is not None:
                awake.terminate(); awake.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=Path, required=True)
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    return run(args.job, args.refresh)


if __name__ == '__main__':
    raise SystemExit(main())
