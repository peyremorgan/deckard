"""Native GUI and private askpass IPC integration; never contacts a real host."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')

import pytest
from PySide6.QtCore import QObject, QProcess, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox
from deckard.gui import MainWindow, ReportActions, create_application
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
    diagnostics = []
    for window in app.topLevelWidgets():
        if isinstance(window, MainWindow):
            details = {'url': window.web.url().toString(), 'note': window.note.text(),
                       'log': window.logs.toPlainText()}
            if window.current and (window.current / 'job.json').exists():
                details['job'] = json.loads((window.current / 'job.json').read_text())
            if window.report_process is not None:
                details['report_process'] = {
                    'state': window.report_process.state().name,
                    'error': window.report_process.errorString(),
                    'exit_code': window.report_process.exitCode(),
                }
            diagnostics.append(details)
    raise AssertionError('GUI condition timed out\n' + json.dumps(diagnostics, indent=2))


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


@pytest.mark.parametrize('report_source', ['local', 'ssh'])
def test_gui_scan_opens_embedded_map_and_selected_folders(app, tmp_path, monkeypatch, report_source):
    opened = []
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url.toLocalFile()) or True)
    root = tmp_path / 'source'; root.mkdir()
    (root / 'A').mkdir(); (root / 'B').mkdir()
    (root / 'A/copy').write_bytes(b'actual file contents')
    (root / 'B/copy').write_bytes(b'actual file contents')
    window = MainWindow(Library(tmp_path / 'library'))
    mounted = []
    def mount_folders(target, source_root, paths, environment, cancelled):
        from deckard.folders import selected_folders
        mounted.append((target, source_root))
        return selected_folders(os.fsdecode(source_root), paths)
    monkeypatch.setattr(window.report_actions.remote, 'folders', mount_folders)
    window.show()
    try:
        window.local_path.setText(str(root))
        window.start_new()
        groups = []
        window.process.started.connect(lambda: groups.append((window.process.processId(), os.getpgid(window.process.processId()))))
        until(app, lambda: window.process is not None and window.process.state().value == 0, timeout=30)
        assert groups and groups[0][0] == groups[0][1]
        if report_source == 'ssh':
            config_path = window.current / 'job.json'
            config = json.loads(config_path.read_text())
            config['source'] = dict(kind='ssh', host='ssh-config-alias', user='user', port=2222,
                                    identity='/key with spaces', root='~')
            config_path.write_text(json.dumps(config))
            window.web.reload()
        until(app, lambda: window.web.url().host() == '127.0.0.1')
        ready = []
        def check():
            window.web.page().runJavaScript("document.title + '|' + document.getElementById('loading')?.classList.contains('hidden')", lambda value: ready.append(value))
        until(app, lambda: (check() or False) if not any(v == 'Deckard|true' for v in ready) else True, timeout=15)
        cfg = json.loads((window.current / 'job.json').read_text())
        assert cfg['status'] == 'complete' and cfg['report_ready']
        assert window.detail_pages.currentIndex() == 1
        assert 'embedded=1' in window.web.url().query()
        assert window.pause.isHidden()
        verify_map_selection_freeze(app, window.web, opened, root)
        if report_source == 'ssh':
            assert mounted == [(cfg['source'], os.fsencode(root))]
        else:
            assert mounted == []
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


def javascript(app, page, source):
    result = []
    page.runJavaScript(source, result.append)
    until(app, lambda: bool(result))
    return result[0]


def verify_map_selection_freeze(app, web, opened, root):
    """Keep a chosen comparison stable while crossing other cells to its controls."""
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    page = web.page()

    def js(source):
        return javascript(app, page, source)

    def move(a, b, shift=False):
        return js(f"""(() => {{
            const r = canvas.getBoundingClientRect();
            canvas.dispatchEvent(new PointerEvent('pointermove', {{
                shiftKey: {json.dumps(shift)},
                clientX: r.left + layout.band + panX +
                    (layout.xx[{a}][0] + layout.xx[{a}][1]) / 2 * layout.side * zoom,
                clientY: r.top + layout.band + panY +
                    (layout.yy[{b}][0] + layout.yy[{b}][1]) / 2 * layout.side * zoom
            }}));
            return document.getElementById('selection').textContent;
        }})()""")

    def key(value, code=''):
        js("canvas.focus(); canvas.dispatchEvent(new KeyboardEvent('keydown', " +
           json.dumps(dict(key=value, code=code, bubbles=True, cancelable=True)) + ")); true")

    assert js("document.getElementById('freeze-selection').disabled")
    assert js("document.getElementById('open-in-explorer').hidden")
    assert js("document.getElementById('selection-actions').hidden")
    assert js("document.getElementById('open-in-explorer').disabled")
    assert move(0, 1) == 'A × B'
    until(app, lambda: not js("document.getElementById('open-in-explorer').hidden"))
    assert js("document.getElementById('open-in-explorer').textContent") == (
        'Open in Finder' if sys.platform == 'darwin' else 'Open in explorer')
    assert js("document.getElementById('open-comparison').nextElementSibling.id") == 'open-in-explorer'
    assert not js("document.getElementById('selection-actions').hidden")
    assert move(1, 0, shift=True) == 'A × B'
    # Releasing Shift resumes ordinary hover without a persistent freeze.
    assert js("document.getElementById('freeze-selection').getAttribute('aria-pressed')") == 'false'
    assert move(1, 0) == 'B × A'
    assert move(0, 1) == 'A × B'
    js("document.getElementById('freeze-selection').click(); true")
    assert move(1, 0) == 'A × B'
    js("document.getElementById('open-in-explorer').click(); true")
    until(app, lambda: len(opened) == 2)
    assert opened == [str(root / 'A'), str(root / 'B')]
    assert not js("document.getElementById('explorer-error').textContent")
    # Axis bands must not replace a frozen cell either.
    assert js("""canvas.dispatchEvent(new PointerEvent('pointermove', {
        clientX: canvas.getBoundingClientRect().left + layout.band + 5,
        clientY: canvas.getBoundingClientRect().top + 5
    })); document.getElementById('selection').textContent""") == 'A × B'
    assert js("document.getElementById('freeze-selection').getAttribute('aria-pressed')") == 'true'
    js("document.getElementById('freeze-selection').click(); true")
    assert move(1, 0) == 'B × A'
    key(' ', 'Space')
    assert move(0, 1) == 'B × A'
    js("document.getElementById('zoom-in').click(); true")
    key('Escape')
    assert js("document.getElementById('zoom').textContent") == '150%'
    assert js("document.getElementById('freeze-selection').getAttribute('aria-pressed')") == 'false'
    js("document.getElementById('fit').click(); true")
    assert move(0, 1) == 'A × B'

    # Exercise Shift+click through real pointer capture, without navigating.
    point = json.loads(js("""JSON.stringify((() => {
        const r = canvas.getBoundingClientRect();
        return [r.left + layout.band + (layout.xx[0][0] + layout.xx[0][1]) / 2 * layout.side,
                r.top + layout.band + (layout.yy[1][0] + layout.yy[1][1]) / 2 * layout.side];
    })())"""))
    QTest.mouseClick(web.focusProxy(), Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.ShiftModifier, QPoint(round(point[0]), round(point[1])))
    until(app, lambda: js("document.getElementById('freeze-selection').getAttribute('aria-pressed')") == 'true')
    assert move(1, 0) == 'A × B'
    # The retained button must open the frozen comparison, then clear its freeze.
    js("document.getElementById('open-comparison').click(); true")
    until(app, lambda: js("document.getElementById('loading').classList.contains('hidden')"))
    assert js("document.getElementById('xpath').textContent") == 'X: A'
    assert js("document.getElementById('ypath').textContent") == 'Y: B'
    assert js("document.getElementById('freeze-selection').disabled")
    assert js("document.getElementById('open-in-explorer').disabled")
    assert js("document.getElementById('open-in-explorer').hidden")
    assert js("document.getElementById('selection-actions').hidden")
    assert js("document.getElementById('freeze-selection').getAttribute('aria-pressed')") == 'false'
    js("document.getElementById('back').click(); true")
    until(app, lambda: js("document.getElementById('loading').classList.contains('hidden')"))


@pytest.fixture
def report_actions(app, tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    (root / 'A # café').mkdir(); (root / 'B').mkdir()
    folder = Library(tmp_path / 'library').create(dict(kind='local', root=str(root)))
    report = folder / 'report'; report.mkdir()
    with sqlite3.connect(report / 'report.sqlite3') as db:
        db.executescript('''CREATE TABLE metadata(key TEXT, value TEXT);
            CREATE TABLE nodes(id INTEGER, path BLOB);
            CREATE TABLE entries(id INTEGER, parent INTEGER);''')
        db.execute('INSERT INTO metadata VALUES (?,?)',
                   ('source_root_base64', json.dumps(base64.b64encode(os.fsencode(root)).decode())))
        db.executemany('INSERT INTO nodes VALUES (?,?)',
                       [(1, b''), (2, os.fsencode('A # café')), (3, b'B'), (4, b'../')])
        db.execute('INSERT INTO entries VALUES (1,2)')
    window = QObject()
    window.report_folder = report
    window.web_page = SimpleNamespace(allowed_port=12345)
    window.web = SimpleNamespace(url=lambda: QUrl('http://127.0.0.1:12345/?embedded=1'))
    actions = ReportActions(window)
    yield actions, root
    if not actions.close():
        until(app, lambda: actions.closed)


def open_folders(app, actions, tokens):
    results = []
    actions.foldersOpened.connect(results.append)
    try:
        actions.openFolders(tokens)
        until(app, lambda: bool(results))
        return results[0]
    finally:
        actions.foldersOpened.disconnect(results.append)


def test_report_actions_open_folders_and_file_parents(app, report_actions, monkeypatch):
    actions, root = report_actions
    opened = []
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url.toLocalFile()) or True)
    assert actions.canOpenFolders()
    assert open_folders(app, actions, ['d:2', 'd:3']) == ''
    assert opened == [str(root / 'A # café'), str(root / 'B')]
    opened.clear()
    assert open_folders(app, actions, ['d:2', 'f:1']) == ''
    assert opened == [str(root / 'A # café')]
    opened.clear()
    assert open_folders(app, actions, ['d:1', 'd:1']) == ''
    assert opened == [str(root)]


def test_report_actions_reject_unavailable_and_untrusted_folders(app, report_actions, monkeypatch):
    actions, root = report_actions
    opened = []
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url.toLocalFile()) or True)
    for tokens in ([], ['d:2', 'd:3', 'd:1'], ['d:2', 'd:999'], ['d:4'], ['other:1'], ['d:nope']):
        assert open_folders(app, actions, tokens)
    (root / 'B').rmdir()
    assert open_folders(app, actions, ['d:2', 'd:3'])
    assert opened == []
    config_path = actions.window.report_folder.parent / 'job.json'
    config = json.loads(config_path.read_text())
    config['source']['kind'] = 'ssh'
    config_path.write_text(json.dumps(config))
    assert actions.canOpenFolders()
    assert open_folders(app, actions, ['d:2'])  # Missing SSH target is not a local fallback.
    config['source']['kind'] = 'local'
    config_path.write_text(json.dumps(config))
    actions.window.web_page.allowed_port = 54321
    assert not actions.canOpenFolders()
    assert open_folders(app, actions, ['d:2'])
    assert opened == []


def test_report_actions_return_explorer_launch_errors(app, report_actions, monkeypatch):
    actions, root = report_actions
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: False)
    assert open_folders(app, actions, ['d:2', 'd:3']) == f'Could not open: {root / "A # café"}, {root / "B"}'


@pytest.mark.parametrize('interruption', ['navigate', 'close'])
def test_remote_open_stays_responsive_and_discards_interrupted_results(app, report_actions, monkeypatch, interruption):
    actions, root = report_actions
    config_path = actions.window.report_folder.parent / 'job.json'
    config = json.loads(config_path.read_text())
    config['source'] = dict(kind='ssh', host='alias', root='~')
    config_path.write_text(json.dumps(config))
    started, release = threading.Event(), threading.Event()
    opened, results = [], []
    def remote_folders(target, source_root, paths, environment, cancelled):
        assert source_root == os.fsencode(root)
        assert environment['DECKARD_AUTH_PURPOSE'] == 'folders'
        started.set()
        if interruption == 'close':
            assert cancelled.wait(5)
            raise KeyboardInterrupt()
        assert release.wait(5)
        return [root / 'B']
    monkeypatch.setattr(actions.remote, 'folders', remote_folders)
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url) or True)
    actions.foldersOpened.connect(results.append)
    actions.openFolders(['d:2', 'd:3'])
    until(app, started.is_set)
    assert actions.busy
    if interruption == 'navigate':
        actions.window.web_page.allowed_port = 54321
        release.set()
        until(app, lambda: bool(results))
    else:
        assert not actions.close()
        until(app, lambda: actions.closed)
    assert results and results[0] and not opened


def test_remote_authentication_cancel_does_not_pause_scan(app, tmp_path, monkeypatch):
    window = MainWindow(Library(tmp_path / 'library'))
    paused = []
    monkeypatch.setattr(window, 'pause_scan', lambda: paused.append(True))
    request = AuthRequest('Password:', '', 'folders')
    window.report_actions.busy = True
    try:
        QTimer.singleShot(0, lambda: window.auth_dialog.reject())
        window.authentication_requested(request)
        assert request.done.is_set() and request.response is None
        assert not paused
    finally:
        window.report_actions.busy = False
        window.close(); app.processEvents()


def test_window_waits_for_unmount_and_can_retry_a_busy_mount(app, tmp_path, monkeypatch):
    window = MainWindow(Library(tmp_path / 'library')); window.show()
    errors = []
    window.report_actions.remote.mounts['test'] = object()
    def disconnect():
        if not errors:
            raise RuntimeError('Mount busy')
        window.report_actions.remote.mounts.clear()
    monkeypatch.setattr(window.report_actions.remote, 'close', disconnect)
    monkeypatch.setattr(QMessageBox, 'warning', lambda parent, title, message: errors.append(message))
    try:
        window.close()
        until(app, lambda: bool(errors))
        assert window.isVisible() and not window.closing and not window.auth.closed
        assert not window.report_actions.closed
        window.close()
        until(app, lambda: window.auth.closed)
        assert window.report_actions.closed and not window.isVisible()
    finally:
        window.report_actions.remote.mounts.clear()
        window.close(); app.processEvents()


def confirm_removal(window, answer=QMessageBox.StandardButton.Yes):
    defaults = []
    def respond():
        dialog = QApplication.activeModalWidget()
        defaults.append(dialog.defaultButton() == dialog.button(QMessageBox.StandardButton.No))
        dialog.done(answer.value)
    QTimer.singleShot(0, respond)
    window.remove.click()
    assert defaults == [True]


def test_remove_completed_scan_stops_report_and_selects_remaining_scan(app, tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    (root / 'a').write_bytes(b'matching'); (root / 'b').write_bytes(b'matching')
    window = MainWindow(Library(tmp_path / 'library'))
    try:
        window.local_path.setText(str(root)); window.start_new()
        folder = window.current
        until(app, lambda: not window.is_running(), timeout=30)
        until(app, lambda: window.web.url().host() == '127.0.0.1')
        report = window.report_process
        other = window.library.create(dict(kind='local', root=str(root), workers=1))
        window.refresh_library()
        confirm_removal(window, QMessageBox.StandardButton.No)
        assert folder.exists() and window.report_process is report
        confirm_removal(window)
        assert not folder.exists()
        assert report.state() == QProcess.ProcessState.NotRunning
        assert window.report_process is None and window.report_folder is None
        assert window.current == other and window.jobs.count() == 1
        assert window.detail_pages.currentIndex() == 0
        assert (root / 'a').read_bytes() == b'matching'
        confirm_removal(window)
        assert window.current is None and window.jobs.count() == 0
        assert window.pages.currentWidget() is window.new_page
        assert window.start.isEnabled()
    finally:
        window.close(); app.processEvents()


def test_remove_locked_scan_keeps_it_available_for_retry(app, tmp_path, monkeypatch):
    import fcntl
    library = Library(tmp_path / 'library')
    folder = library.create(dict(kind='local', root='/source'))
    window = MainWindow(library)
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args[2]))
    try:
        window.jobs.setCurrentRow(0)
        with (folder / 'scan.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            confirm_removal(window)
            assert folder.exists() and window.current == folder
            assert window.jobs.count() == 1 and window.pending_removal is None
            assert window.remove.isEnabled() and window.resume.isEnabled()
            assert len(warnings) == 1 and 'still running' in warnings[0]
        confirm_removal(window)
        assert not folder.exists() and window.current is None
    finally:
        window.close(); app.processEvents()


@pytest.mark.parametrize('navigate', [False, True])
def test_remove_running_scan_waits_for_graceful_exit(app, tmp_path, monkeypatch, navigate):
    marker = tmp_path / 'stopped'
    script = '''
import fcntl, json, os, signal, sys, time
from pathlib import Path
folder, marker = map(Path, sys.argv[1:])
stopped = False
def stop(*_):
    global stopped
    stopped = True
signal.signal(signal.SIGTERM, stop)
with (folder / 'scan.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    cfg = json.loads((folder / 'job.json').read_text())
    cfg.update(status='running', pid=os.getpid())
    (folder / 'job.json').write_text(json.dumps(cfg))
    print(json.dumps(dict(event='log', message='Ready')), flush=True)
    while not stopped:
        time.sleep(.01)
    time.sleep(.2)
    assert (folder / 'job.json').exists()
    cfg.update(status='paused')
    (folder / 'job.json').write_text(json.dumps(cfg))
    print(json.dumps(dict(event='log', message='Graceful cleanup finished')), flush=True)
    marker.write_text('stopped gracefully')
'''
    start = QProcess.start
    def start_worker(proc, program, args):
        if 'deckard.scan_job' in args:
            args = ['-u', '-c', script, args[args.index('--job') + 1], str(marker)]
        return start(proc, program, args)
    monkeypatch.setattr(QProcess, 'start', start_worker)
    library = Library(tmp_path / 'library')
    folder = library.create(dict(kind='local', root='/source'))
    other = library.create(dict(kind='local', root='/other'))
    window = MainWindow(library)
    removed = []
    remove = library.remove
    def check_remove(path):
        assert not window.is_running()
        assert marker.read_text() == 'stopped gracefully'
        assert 'Graceful cleanup finished' in (path / 'scan.log').read_text()
        removed.append(path)
        remove(path)
    monkeypatch.setattr(library, 'remove', check_remove)
    try:
        window.start_job(folder)
        until(app, lambda: window.logs.toPlainText() == 'Ready')
        confirm_removal(window)
        assert folder.exists() and window.pending_removal == folder
        assert not window.remove.isEnabled() and not window.start.isEnabled()
        if navigate:
            window.current = other; window.show_job()
        until(app, lambda: not folder.exists())
        assert removed == [folder]
        assert window.pending_removal is None
        assert window.current == other and other.exists()
        assert window.remove.isEnabled() and window.resume.isEnabled()
    finally:
        window.close(); app.processEvents()


def test_theme_persistence_system_changes_and_invalid_settings(app, tmp_path, monkeypatch):
    from deckard.theme import PALETTES
    from PySide6.QtGui import QPalette
    library = Library(tmp_path / 'library')
    preferences = library.root / 'preferences.json'
    preferences.write_text(json.dumps({'theme': 'invalid', 'other': True}))
    window = MainWindow(library)
    try:
        assert window.appearance.preference == 'system'
        monkeypatch.setattr(window.appearance, 'system_scheme', lambda: Qt.ColorScheme.Dark)
        app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
        assert window.appearance.resolved == 'dark'
        window.theme_actions['light'].trigger()
        assert app.palette().color(QPalette.ColorRole.Window).name() == PALETTES['light']['face']
        app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
        assert window.appearance.resolved == 'light'
        window.appearance.setPreference('invalid')
        assert window.appearance.preference == 'light'
        assert json.loads(preferences.read_text()) == {'theme': 'light', 'other': True}
        window.theme_actions['system'].trigger()
        assert window.appearance.resolved == 'dark'
        monkeypatch.setattr(window.appearance, 'system_scheme', lambda: Qt.ColorScheme.Light)
        app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
        assert window.appearance.resolved == 'light'
        window.theme_actions['dark'].trigger()
        assert sum(action.isChecked() for action in window.theme_actions.values()) == 1
    finally:
        window.close(); app.processEvents()
    reopened = MainWindow(library)
    try:
        assert reopened.appearance.snapshot() == {'preference': 'dark', 'resolved': 'dark'}
        assert reopened.theme_actions['dark'].isChecked()
    finally:
        reopened.close(); app.processEvents()


def test_embedded_theme_sync_reload_and_matrix_contrast(app, tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    (root / 'A').mkdir(); (root / 'B').mkdir()
    (root / 'A/copy').write_bytes(b'actual file contents')
    (root / 'B/copy').write_bytes(b'actual file contents')
    library = Library(tmp_path / 'library')
    window = MainWindow(library); window.show()
    def ready():
        return javascript(app, window.web_page,
            "Boolean(window.DeckardTheme && document.getElementById('loading')?.classList.contains('hidden'))")
    try:
        window.theme_actions['dark'].trigger()
        window.local_path.setText(str(root)); window.start_new()
        until(app, lambda: window.process is not None and window.process.state().value == 0, timeout=30)
        until(app, lambda: window.web.url().host() == '127.0.0.1')
        until(app, ready)
        assert javascript(app, window.web_page, 'document.documentElement.dataset.theme') == 'dark'
        # The web control updates the native menu and the durable desktop preference.
        javascript(app, window.web_page, "document.getElementById('theme').value = 'light'; document.getElementById('theme').dispatchEvent(new Event('change')); true")
        until(app, lambda: window.appearance.preference == 'light')
        assert window.theme_actions['light'].isChecked()
        assert json.loads((library.root / 'preferences.json').read_text())['theme'] == 'light'
        window.theme_actions['dark'].trigger()
        until(app, lambda: javascript(app, window.web_page, 'document.documentElement.dataset.theme') == 'dark')
        assert javascript(app, window.web_page, "document.getElementById('theme').value") == 'dark'
        # Every sampled heat color, including the old failing mid-blue, has readable text.
        assert javascript(app, window.web_page, """(() => {
            function lum(hex) {
                const c = hex.match(/[0-9a-f]{2}/gi).map(h => parseInt(h, 16) / 255)
                    .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
                return c[0] * .2126 + c[1] * .7152 + c[2] * .0722;
            }
            const backgrounds = Array.from({length: 1001}, (_, i) => heat(i / 1000));
            backgrounds.push('#7d5cc5', '#172333');
            return Math.min(...backgrounds.map(bg => {
                const a = lum(bg), b = lum(contrastText(bg));
                return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
            }));
        })()""") >= 4.5
        loaded = []
        window.web.loadFinished.connect(loaded.append)
        window.web.reload()
        until(app, lambda: bool(loaded))
        assert loaded[-1]
        until(app, ready)
        assert javascript(app, window.web_page, 'document.documentElement.dataset.theme') == 'dark'
        from PySide6.QtTest import QTest
        QTest.qWait(150)
        window.grab().save(str(tmp_path / 'dark-theme.png'))
        window.theme_actions['light'].trigger()
        until(app, lambda: javascript(app, window.web_page, 'document.documentElement.dataset.theme') == 'light')
        QTest.qWait(150)
        window.grab().save(str(tmp_path / 'light-theme.png'))
        verify_standalone_themes(app, window.web.url(), tmp_path)
    finally:
        window.close(); app.processEvents()


def report_contrast_violations(app, page):
    """Check rendered text against the background it actually inherits."""
    return json.loads(javascript(app, page, """(() => {
        document.getElementById('settings').open = true;
        const rgb = value => value.match(/[\\d.]+/g).map(Number);
        const blend = (top, bottom) => top.slice(0, 3).map((c, i) =>
            c * (top[3] ?? 1) + bottom[i] * (1 - (top[3] ?? 1)));
        const lum = c => c.slice(0, 3).map(v => v / 255)
            .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
            .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
        const failures = [], walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const node = walker.currentNode, parent = node.parentElement;
            if (!node.textContent.trim() || !parent.getClientRects().length ||
                !parent.checkVisibility({visibilityProperty: true})) continue;
            const ancestors = [];
            for (let e = parent; e; e = e.parentElement) ancestors.unshift(e);
            const background = ancestors.reduce((bg, e) =>
                blend(rgb(getComputedStyle(e).backgroundColor), bg), [255, 255, 255]);
            const ink = blend(rgb(getComputedStyle(parent).color), background);
            const a = lum(ink), b = lum(background), ratio = (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
            if (ratio < 4.5) failures.push({text: node.textContent.trim(), ratio});
        }
        return JSON.stringify(failures);
    })()"""))


def verify_standalone_themes(app, url, tmp_path):
    from PySide6.QtWebEngineCore import QWebEngineScript
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtTest import QTest
    from PySide6.QtCore import QUrl, QUrlQuery
    url = QUrl(url)
    query = QUrlQuery(url); query.removeAllQueryItems('embedded'); url.setQuery(query)
    browser = QWebEngineView(); browser.resize(1360, 960); browser.show()
    page = browser.page()
    media = QWebEngineScript()
    media.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    media.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    media.setSourceCode('''window.testMedia = new EventTarget(); window.testMedia.matches = false;
        window.matchMedia = () => window.testMedia;''')
    page.scripts().insert(media)
    loaded = []
    browser.loadFinished.connect(loaded.append)
    def load():
        loaded.clear(); browser.load(url)
        until(app, lambda: bool(loaded))
        assert loaded[-1]
        until(app, lambda: javascript(app, page,
            "Boolean(window.DeckardTheme && document.getElementById('loading')?.classList.contains('hidden'))"))
    def choose(mode):
        javascript(app, page, "document.getElementById('theme').value = " + json.dumps(mode) +
            "; document.getElementById('theme').dispatchEvent(new Event('change')); true")
    try:
        load()
        for mode in ('light', 'dark'):
            choose(mode)
            assert javascript(app, page, 'document.documentElement.dataset.theme') == mode
            # Include dynamic inspector content and keeper badges in the audit.
            javascript(app, page, "inspect({axis: 'x', index: 0}); document.getElementById('covered-load').click(); true")
            until(app, lambda: javascript(app, page, "!document.getElementById('covered-load').disabled"))
            javascript(app, page, "document.querySelector('#covered-list button').click(); true")
            until(app, lambda: javascript(app, page, "!document.querySelector('#covered-list button').disabled"))
            assert report_contrast_violations(app, page) == []
            javascript(app, page, "inspect({a: 0, b: 1}); true")
            assert report_contrast_violations(app, page) == []
            QTest.qWait(150)
            browser.grab().save(str(tmp_path / ('browser-' + mode + '.png')))
        load()
        assert javascript(app, page, "document.getElementById('theme').value") == 'dark'
        assert javascript(app, page, 'document.documentElement.dataset.theme') == 'dark'
        # Simulate a browser media-query change while System is selected.
        choose('system')
        assert javascript(app, page, 'document.documentElement.dataset.theme') == 'light'
        javascript(app, page, "testMedia.matches = true; testMedia.dispatchEvent(new Event('change')); true")
        assert javascript(app, page, 'document.documentElement.dataset.theme') == 'dark'
        choose('light')
        javascript(app, page, "testMedia.dispatchEvent(new Event('change')); true")
        assert javascript(app, page, 'document.documentElement.dataset.theme') == 'light'
        # A blocked storage API must not prevent report startup or theme changes.
        blocked = QWebEngineScript()
        blocked.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        blocked.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        blocked.setSourceCode("Object.defineProperty(window, 'localStorage', {get() {throw new Error('blocked');}});")
        page.scripts().insert(blocked)
        load(); choose('dark')
        assert javascript(app, page, 'document.documentElement.dataset.theme') == 'dark'
    finally:
        from shiboken6 import delete
        browser.close(); delete(browser); app.processEvents()
