"""Native GUI and private askpass IPC integration; never contacts a real host."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')

import pytest
from PySide6.QtCore import QTimer
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
