"""Native GUI and private askpass IPC integration; never contacts a real host."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')

import pytest
from PySide6.QtCore import QProcess, QTimer, Qt
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox
from deckard.gui import MainWindow, create_application
from deckard.auth import AuthBridge, AuthRequest
from deckard.storage import Library


@pytest.fixture(scope='module')
def app():
    return create_application([])


def until(app, condition, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(.01)
    raise AssertionError('GUI condition timed out')


def test_private_askpass_ipc_returns_secret_without_writing_it(app, tmp_path):
    bridge = AuthBridge()
    received = []
    bridge.requested.connect(lambda request: (received.append(request.prompt), request.answer('test-only-secret')))
    env = os.environ.copy(); env.update(bridge.environment())
    root = Path(bridge.runtime.name)
    proc = subprocess.Popen([str(bridge.helper), 'Password for test host:'], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        until(app, lambda: proc.poll() is not None)
        out, err = proc.communicate()
        assert proc.returncode == 0 and out == 'test-only-secret\n'
        assert received == ['Password for test host:']
        assert all(b'test-only-secret' not in p.read_bytes() for p in root.iterdir() if p.is_file())
    finally:
        bridge.close()
    assert not root.exists()


def test_cancelled_askpass_fails_closed(app):
    bridge = AuthBridge(); bridge.requested.connect(lambda request: request.answer(None))
    env = os.environ.copy(); env.update(bridge.environment())
    proc = subprocess.Popen([str(bridge.helper), 'Password:'], env=env, stdout=subprocess.PIPE)
    try:
        until(app, lambda: proc.poll() is not None)
        assert proc.communicate()[0] == b'' and proc.returncode == 1
    finally:
        bridge.close()


def test_gui_local_scan_opens_embedded_map(app, tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    (root / 'A').mkdir(); (root / 'B').mkdir()
    (root / 'A/copy').write_bytes(b'actual file contents')
    (root / 'B/copy').write_bytes(b'actual file contents')
    window = MainWindow(Library(tmp_path / 'library'))
    window.show()
    try:
        window.local_path.setText(str(root))
        window.start_new()
        groups = []
        window.process.started.connect(lambda: groups.append((window.process.processId(), os.getpgid(window.process.processId()))))
        until(app, lambda: window.process is not None and window.process.state().value == 0, timeout=30)
        assert groups and groups[0][0] == groups[0][1]
        until(app, lambda: window.web.url().host() == '127.0.0.1')
        ready = []
        def check():
            window.web.page().runJavaScript("document.title + '|' + document.getElementById('loading').classList.contains('hidden')", lambda value: ready.append(value))
        until(app, lambda: (check() or False) if not any(v == 'Deckard|true' for v in ready) else True, timeout=15)
        cfg = json.loads((window.current / 'job.json').read_text())
        assert cfg['status'] == 'complete' and cfg['report_ready']
        assert window.detail_pages.currentIndex() == 1
        assert 'embedded=1' in window.web.url().query()
        assert window.pause.isHidden()
        interaction = []
        window.web.page().runJavaScript("document.getElementById('zoom-in').click(); document.getElementById('zoom').textContent", interaction.append)
        until(app, lambda: bool(interaction))
        assert interaction == ['150%']
        window.grab().save(str(tmp_path / 'desktop.png'))
        window.library.create({'kind': 'local', 'root': str(root), 'workers': 2})
        window.refresh_library()
        window.jobs.setCurrentRow(0)
        assert window.detail_pages.currentIndex() == 0
        assert not window.status_card.isHidden()
        window.jobs.setCurrentRow(1)
        assert window.detail_pages.currentIndex() == 1
        assert window.status_card.isHidden()
        until(app, lambda: window.web.url().host() == '127.0.0.1')
    finally:
        window.close(); app.processEvents()


def test_gui_new_scan_has_both_sources_and_stores_no_password(app, tmp_path):
    window = MainWindow(Library(tmp_path / 'library'))
    try:
        window.source_kind('ssh')
        assert window.source_pages.currentIndex() == 1
        assert window.user.text() == '' and window.port.value() == 0
        assert window.remote_path.text() == '~'
        assert window.identity.text() == ''
        window.source_kind('local')
        assert window.source_pages.currentIndex() == 0
    finally:
        window.close(); app.processEvents()


def test_scan_output_survives_navigation_and_restart(app, tmp_path):
    library = Library(tmp_path / 'library')
    first = library.create(dict(kind='local', root='/first'))
    second = library.create(dict(kind='local', root='/second'))
    window = MainWindow(library)
    stdout, stderr = bytearray(), bytearray()

    def drain(buffer):
        result = bytes(buffer)
        buffer.clear()
        return result

    window.process = SimpleNamespace(
        state=lambda: QProcess.ProcessState.NotRunning,
        readAllStandardOutput=lambda: drain(stdout),
        readAllStandardError=lambda: drain(stderr))
    window.active_folder = first

    def select(folder):
        for index in range(window.jobs.count()):
            item = window.jobs.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == str(folder):
                window.jobs.setCurrentItem(item)
                return
        raise AssertionError('Scan missing from library')

    def event(kind, message):
        stdout.extend((json.dumps(dict(event=kind, message=message)) + '\n').encode())
        window.read_events()

    try:
        select(first)
        event('log', 'Inventory started')
        assert window.logs.toPlainText() == 'Inventory started'
        select(second)
        assert window.logs.toPlainText() == ''
        event('log', 'Hashing café\nStill working')
        stderr.extend(b'Scanner diagnostic\n')
        window.read_errors()
        event('failed', 'Source disconnected')
        assert window.logs.toPlainText() == ''
        assert window.note.isHidden()
        select(first)
        expected = 'Inventory started\nHashing café\nStill working\nScanner diagnostic\nSource disconnected'
        assert window.logs.toPlainText() == expected
        window.show_new()
        window.select_job(window.jobs.currentItem())
        assert window.logs.toPlainText() == expected
        select(second)
        window.active_folder = second
        event('log', 'Second scan output')
        select(first)
        assert window.logs.toPlainText() == expected
    finally:
        window.close(); app.processEvents()

    reopened = MainWindow(library)
    try:
        reopened.current = first
        reopened.show_job()
        assert reopened.logs.toPlainText() == expected
        reopened.current = second
        reopened.show_job()
        assert reopened.logs.toPlainText() == 'Second scan output'
    finally:
        reopened.close(); app.processEvents()


@pytest.mark.parametrize('refresh', [False, True])
def test_scan_keeps_previous_output_when_started(app, tmp_path, refresh):
    root = tmp_path / 'source'; root.mkdir()
    (root / 'a').write_bytes(b'matching'); (root / 'b').write_bytes(b'matching')
    library = Library(tmp_path / 'library')
    folder = library.create(dict(kind='local', root=str(root), workers=1))
    history = ''.join(f'Previous output {i}\n' for i in range(650))
    (folder / 'scan.log').write_text(history, encoding='utf-8')
    window = MainWindow(library)
    try:
        window.current = folder
        window.show_job()
        assert window.logs.blockCount() == 600
        assert window.logs.toPlainText().splitlines()[0] == 'Previous output 50'
        window.start_job(folder, refresh=refresh)
        until(app, lambda: window.process.state() == QProcess.ProcessState.NotRunning, timeout=30)
        saved = (folder / 'scan.log').read_text(encoding='utf-8')
        assert saved.startswith(history)
        assert 'Loading directory hierarchy' in saved[len(history):]
        assert json.loads((folder / 'job.json').read_text())['status'] == 'complete'
        window.show_job()
        assert window.logs.toPlainText() == '\n'.join(saved.splitlines()[-600:])
    finally:
        window.close(); app.processEvents()


@pytest.mark.parametrize('kind', ['password', 'passphrase', 'confirm'])
def test_native_authentication_dialog_masks_secrets_and_defaults_to_no(app, tmp_path, kind):
    window = MainWindow(Library(tmp_path / 'library'))
    window.is_running = lambda: True
    request = AuthRequest('Password:' if kind == 'password' else 'SSH key passphrase:',
                          'confirm' if kind == 'confirm' else '')
    inspected = []
    def answer():
        dialog = window.auth_dialog
        if kind == 'confirm':
            inspected.append(dialog.defaultButton() == dialog.button(QMessageBox.StandardButton.No))
            dialog.done(QMessageBox.StandardButton.No.value)
        else:
            inspected.append(dialog.findChild(QLineEdit).echoMode() == QLineEdit.EchoMode.Password)
            dialog.setTextValue('dialog-only-secret'); dialog.accept()
    QTimer.singleShot(0, answer)
    try:
        window.authentication_requested(request)
        assert inspected == [True]
        assert request.response == ('no' if kind == 'confirm' else 'dialog-only-secret')
        assert not window.library.jobs()
    finally:
        window.is_running = lambda: False
        window.close(); app.processEvents()
