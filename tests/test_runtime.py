"""Standalone helper routing and portable scan-library locking."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from deckard.runtime import module_command
from deckard.storage import Library


def test_report_server_startup_does_not_require_reverse_dns(tmp_path, monkeypatch, capsys):
    import socket
    from deckard.report import server

    def unavailable_dns(*args):
        raise AssertionError('Loopback report startup must not resolve a hostname')

    listening = []
    monkeypatch.setattr(socket, 'getfqdn', unavailable_dns)
    monkeypatch.setattr(server.ThreadingHTTPServer, 'serve_forever',
                        lambda http: listening.append((http.server_name, http.server_port)))
    server.serve(tmp_path, port=0)
    assert len(listening) == 1
    name, port = listening[0]
    assert name == 'localhost' and port > 0
    assert capsys.readouterr().out == f'Deckard report: http://127.0.0.1:{port}/\n'


@pytest.mark.parametrize('newline', [b'\n', b'\r\n'])
def test_report_url_accepts_platform_line_endings(newline):
    from deckard.gui import MainWindow

    output = iter([b'Deckard report: http://127.0.0.1:', b'54321/' + newline])
    loaded = []
    window = SimpleNamespace(
        report_buffer=bytearray(),
        report_process=SimpleNamespace(readAllStandardOutput=lambda: next(output)),
        web_page=SimpleNamespace(allowed_port=None),
        web=SimpleNamespace(load=loaded.append),
    )
    MainWindow.read_report_url(window)
    assert not loaded
    MainWindow.read_report_url(window)
    assert window.web_page.allowed_port == 54321
    assert len(loaded) == 1
    assert loaded[0].toString() == 'http://127.0.0.1:54321/?embedded=1'


def test_source_commands_use_current_interpreter():
    assert module_command('deckard.report', 'serve', '--port', 0) == [
        sys.executable, '-u', '-m', 'deckard.report', 'serve', '--port', '0',
    ]


@pytest.mark.parametrize('platform,executable,helper', [
    ('darwin', '/Applications/Deckard.app/Contents/MacOS/Deckard', 'Deckard-helper'),
    ('linux', '/opt/Deckard/Deckard', 'Deckard-helper'),
    ('win32', 'C:/Program Files/Deckard/Deckard.exe', 'Deckard-helper.exe'),
])
def test_frozen_commands_use_sibling_console_helper(monkeypatch, platform, executable, helper):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'platform', platform)
    monkeypatch.setattr(sys, 'executable', executable)
    assert module_command('deckard.scan_job', '--job', 'scan with spaces') == [
        str(Path(executable).with_name(helper)), '--module', 'deckard.scan_job', '--job', 'scan with spaces',
    ]


def test_locked_scan_cannot_be_removed(tmp_path):
    library = Library(tmp_path / 'library')
    folder = library.create({'kind': 'local', 'root': str(tmp_path / 'source')})
    # A separate process exercises Windows byte locks and POSIX flock without
    # relying on their different same-process lock semantics.
    script = '''
import sys
from deckard.locking import lock_exclusive
with open(sys.argv[1], 'a') as stream:
    lock_exclusive(stream)
    print('locked', flush=True)
    sys.stdin.readline()
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(folder / 'scan.lock')],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'locked'
        with pytest.raises(RuntimeError, match='still running'):
            library.remove(folder)
        assert (folder / 'job.json').exists()
    finally:
        process.communicate('\n', timeout=10)
    library.remove(folder)
    assert not folder.exists()


def test_inventory_keeps_report_paths_portable(tmp_path):
    from deckard import engine
    import threading

    (tmp_path / 'nested').mkdir()
    (tmp_path / 'nested/copy').write_bytes(b'\x00\r\n')
    events = []
    root = os.fsencode(tmp_path)
    work = engine.inventory_worker(root, os.stat(root).st_dev)
    work((1, b'nested'), lambda event: events.append(event) or True, threading.Event())
    records = events[-1][-1]
    assert len(records) == 1 and records[0][0] == 'file', events
    assert records[0][1] == b'nested/copy'
    assert records[0][2][0] == 3
    assert records[0][4] is not None
