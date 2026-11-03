import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from deckard import engine
from deckard.bootstrap import payload, LOADER, remote_command
from deckard.remote import AgentlessSource
from deckard.ssh import validate_target, SSHTransport
from deckard.storage import Library


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def test_streamed_worker_does_not_create_remote_files(tmp_path):
    root = tmp_path / 'remote'; root.mkdir()
    (root / 'a').write_bytes(b'same'); (root / 'b').write_bytes(b'same')
    before = snapshot(root)
    # This is the exact bootstrap+wire protocol used over SSH, over local pipes.
    source = AgentlessSource(str(root), local_python=sys.executable)
    try:
        assert source.info['agentless'] is True
        assert source.info['core_sha256'] == payload()[1]
        records = []
        import threading
        source.inventory_worker(source.root, int(source.info['device']))((1, b''), lambda e: records.extend(e[-1]) or True, threading.Event())
        assert {r[1] for r in records if r[0] == 'file'} == {b'a', b'b'}
        file = next(r for r in records if r[0] == 'file')
        row = dict(zip(('size', 'mtime', 'ctime', 'dev', 'ino'), file[2]))
        row.update(id=1, path=file[1], nlink=1)
        assert source.hash_file(source.root, row, 'prefix', 1048576)[0] == hashlib.sha256(b'same').hexdigest()
    finally:
        source.close()
    assert snapshot(root) == before
    assert not list(root.rglob('__pycache__'))
    assert not list(root.rglob('*.sqlite3'))


def test_in_memory_worker_full_scan_matches_native(tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    data = b'x' * (engine.SAMPLE * 4)
    for name in ('a', 'b'):
        (root / name).write_bytes(data)
    (root / 'different').write_bytes(data[:-1] + b'y')
    state = tmp_path / 'wire'; state.mkdir()
    from deckard.scan_job import settings
    source = AgentlessSource(str(root), local_python=sys.executable)
    old = source.install()
    try:
        assert engine.scan(source.root, engine.raw(str(state)), settings()) == 0
    finally:
        source.restore(old); source.close()
    summary = json.loads((state / 'duplicates.jsonl').read_text().splitlines()[-1])
    assert summary['groups'] == 1
    assert summary['paths'] == 2
    assert summary['complete']


def test_root_shell_characters_are_data_not_commands(tmp_path):
    root = tmp_path / "folder '; touch PWNED; '"
    root.mkdir(); (root / 'a').write_text('hello')
    source = AgentlessSource(str(root), local_python=sys.executable)
    source.close()
    command = remote_command(['--root-base64', engine.b64(os.fsencode(root))])
    assert 'PWNED' not in command
    assert not (tmp_path / 'PWNED').exists()


def test_source_validation_and_no_credential_storage(tmp_path):
    library = Library(tmp_path / 'library')
    with pytest.raises(ValueError):
        library.create(dict(kind='ssh', root='~', password='never-store-this'))
    assert validate_target('user@host', '', 743) == ('host', 'user', 743)
    for value in ('-oProxyCommand=evil', 'host name', 'host\ncommand', '/path'):
        with pytest.raises(ValueError):
            validate_target(value)
    with pytest.raises(ValueError):
        validate_target('user:password@host')


def run_job(folder, *args):
    return subprocess.run([sys.executable, '-m', 'deckard.scan_job', '--job', str(folder), *args],
                          capture_output=True, text=True, timeout=30)


def test_local_job_and_refresh_keep_index_and_generate_report(tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    (root / 'a').write_bytes(b'matching'); (root / 'b').write_bytes(b'matching')
    job = Library(tmp_path / 'library').create(dict(kind='local', root=str(root), workers=2))
    result = run_job(job)
    assert result.returncode == 0, result.stdout + result.stderr
    cfg = json.loads((job / 'job.json').read_text())
    assert cfg['status'] == 'complete' and cfg['report_ready']
    assert (job / 'report/root.json').exists()
    records = [json.loads(line) for line in next(job.glob('metrics-*.jsonl')).read_text().splitlines()]
    assert any(record.get('phase') == 'visualizer.matrix' for record in records)
    assert any('visualizer.build' in record.get('metrics', {}) for record in records)
    with contextlib.closing(sqlite3.connect(job / 'index.sqlite3')) as db:
        before = db.execute('SELECT path,full FROM files ORDER BY path').fetchall()
    result = run_job(job, '--refresh')
    assert result.returncode == 0, result.stdout
    with contextlib.closing(sqlite3.connect(job / 'index.sqlite3')) as db:
        assert db.execute('SELECT path,full FROM files ORDER BY path').fetchall() == before


def test_own_local_state_is_excluded_when_scanning_ancestor(tmp_path):
    root = tmp_path / 'home'; root.mkdir()
    (root / 'one').write_bytes(b'copy'); (root / 'two').write_bytes(b'copy')
    job = Library(root / 'Deckard').create(dict(kind='local', root=str(root), workers=1))
    result = run_job(job)
    assert result.returncode == 0, result.stdout
    with contextlib.closing(sqlite3.connect(job / 'index.sqlite3')) as db:
        assert db.execute('SELECT COUNT(*) FROM files').fetchone()[0] == 2
        assert db.execute('SELECT kind FROM skipped').fetchone()[0] == 'deckard-local-state'


def test_pause_and_resume_committed_inventory(tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    for i in range(3000):
        (root / ('f%05d' % i)).write_bytes(b'same')
    job = Library(tmp_path / 'library').create(dict(kind='local', root=str(root), workers=1))
    with (tmp_path / 'events').open('w') as out:
        proc = subprocess.Popen([sys.executable, '-u', '-m', 'deckard.scan_job', '--job', str(job)], stdout=out, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and proc.poll() is None:
            if 'inventory' in (tmp_path / 'events').read_text():
                proc.send_signal(signal.SIGTERM); break
            time.sleep(.01)
        proc.wait(timeout=15)
    assert json.loads((job / 'job.json').read_text())['status'] in ('paused', 'complete')
    result = run_job(job)
    assert result.returncode == 0, result.stdout
    with contextlib.closing(sqlite3.connect(job / 'index.sqlite3')) as db:
        assert db.execute('SELECT COUNT(*) FROM files').fetchone()[0] == 3000
        assert db.execute('SELECT COUNT(*) FROM files WHERE error IS NOT NULL').fetchone()[0] == 0
