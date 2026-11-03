"""Native Qt desktop shell around the isolated scanner and local report API."""
import json
import os
import signal
from pathlib import Path
import sys
import time

from PySide6.QtCore import Qt, QProcess, QProcessEnvironment, QTimer, QUrl, Slot
from PySide6.QtGui import QAction, QDesktopServices, QFont, QIcon, QKeySequence, QPalette, QColor
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QFrame, QLabel, QPushButton, QLineEdit, QFileDialog, QStackedWidget,
    QSpinBox, QFormLayout, QGroupBox, QListWidget, QListWidgetItem, QProgressBar,
    QPlainTextEdit, QMessageBox, QInputDialog, QSizePolicy, QSplitter, QScrollArea)
from PySide6.QtWidgets import QDialog
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

from . import __version__
from .auth import AuthBridge
from .storage import Library, atomic_json
from .ssh import validate_target


STYLE = '''
QMainWindow, QWidget#content { background: #f5f4ee; color: #26352d; }
QWidget { font-family: 'Helvetica Neue', 'Segoe UI', sans-serif; font-size: 13px; }
QFrame#sidebar { background: #203b30; } QFrame#sidebar QLabel { color: #edf1e5; }
QLabel#brand { font-size: 25px; font-weight: 700; letter-spacing: 2px; }
QLabel#side-note { color: #b8c9b8; font-size: 11px; }
QLabel#eyebrow { color: #617d68; font-size: 10px; font-weight: 700; letter-spacing: 2px; }
QLabel#title { font-family: Georgia, serif; font-size: 38px; font-weight: 400; }
QLabel#subtitle { color: #748073; font-size: 13px; }
QLabel#metric { font-size: 24px; font-weight: 500; color: #294c39; }
QLabel#metric-label { font-size: 10px; color: #7b8377; }
QFrame#card, QGroupBox { background: #fffef9; border: 1px solid #dde1d4; border-radius: 9px; }
QGroupBox { padding: 20px 12px 10px; margin-top: 12px; } QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #61705c; }
QLineEdit, QSpinBox { background: #fbfcf6; border: 1px solid #d8ddcf; border-radius: 5px; padding: 10px; color: #26352d; min-height: 18px; }
QLineEdit:focus, QSpinBox:focus { border-color: #527b5b; }
QPushButton { padding: 10px 16px; border: 1px solid #cfd8c8; border-radius: 5px; background: #fffef9; color: #36533e; font-weight: 500; }
QPushButton:hover { background: #e9eee2; } QPushButton:disabled { color: #9ba697; background: #eceee6; }
QPushButton#primary { background: #355e44; color: white; border-color: #355e44; padding: 12px 20px; }
QPushButton#primary:hover { background: #244a34; } QPushButton#primary:disabled { background: #9cafa0; border-color: #9cafa0; }
QPushButton#source-tab:checked { background: #e5eddc; border-color: #769070; }
QFrame#sidebar QPushButton { color: #edf1e5; background: #345343; border-color: #4b6651; }
QFrame#sidebar QPushButton:hover { background: #476953; }
QListWidget#library { background: transparent; border: none; color: #dbe7d7; outline: none; }
QListWidget#library::item { color: #dbe7d7; padding: 13px 8px; margin: 2px 0; border-radius: 5px; }
QListWidget#library::item:selected { color: #f5f8ec; background: #3e5b47; } QListWidget#library::item:hover { background: #304d3c; }
QProgressBar { border: none; background: #e4e9dc; border-radius: 4px; height: 7px; max-height: 7px; }
QProgressBar::chunk { background: #688867; border-radius: 4px; }
QPlainTextEdit { background: #f3f5ed; color: #61715c; border: 1px solid #e0e4d7; border-radius: 6px; padding: 9px; font-family: Menlo, monospace; font-size: 10px; }
QSplitter::handle { background: #d7dfcf; width: 1px; }
'''


def label(value='', name=None):
    item = QLabel(value)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(True)
    if name:
        item.setObjectName(name)
    return item


def size(value):
    value = float(value or 0)
    units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024; unit += 1
    return ('%.1f' % value).rstrip('0').rstrip('.') + ' ' + units[unit]


def location(config):
    target = config['source']
    return ((target.get('user', '') + '@' if target.get('user') else '') + target['host'] + ':'
            if target['kind'] == 'ssh' else '') + target['root']


class ReportPage(QWebEnginePage):
    def __init__(self, parent):
        super().__init__(parent)
        self.allowed_port = None

    def acceptNavigationRequest(self, url, kind, main_frame):
        return (url.scheme() == 'about' or
                (url.scheme() == 'http' and url.host() == '127.0.0.1' and url.port() == self.allowed_port))


class SourcePages(QWidget):
    """Size the form to the selected source instead of the tallest page."""
    def __init__(self):
        super().__init__()
        self.widgets = []; self.index = 0
        self.layout = QVBoxLayout(self); self.layout.setContentsMargins(0, 0, 0, 0)

    def addWidget(self, widget):
        self.widgets.append(widget); self.layout.addWidget(widget)
        widget.setVisible(len(self.widgets) == 1)

    def currentIndex(self):
        return self.index

    def setCurrentIndex(self, index):
        self.index = index
        for i, widget in enumerate(self.widgets):
            widget.setVisible(i == index)
        self.layout.invalidate(); self.updateGeometry()


class MainWindow(QMainWindow):
    def __init__(self, library=None):
        super().__init__()
        self.library = library or Library()
        self.current = None
        self.active_folder = None
        self.process = None
        self.report_process = None
        self.report_buffer = bytearray()
        self.buffer = bytearray()
        self.metrics = {}
        self.closing = False
        self.terminal_event = False
        self.started = 0
        self.worker_pid = None
        self.auth_dialog = None
        self.auth = AuthBridge(self)
        self.auth.requested.connect(self.authentication_requested)
        self.setWindowTitle('Deckard')
        self.resize(1360, 960)
        self.setMinimumSize(1000, 720)
        self.setStyleSheet(STYLE)
        self._build()
        self._menus()
        self.refresh_library()
        self.show_new()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_metrics)
        self.timer.start(1000)

    def _build(self):
        container = QWidget(); container.setObjectName('content')
        layout = QHBoxLayout(container); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        side = QFrame(); side.setObjectName('sidebar'); side.setFixedWidth(235)
        left = QVBoxLayout(side); left.setContentsMargins(21, 30, 21, 22); left.setSpacing(18)
        left.addWidget(label('▥  DECKARD', 'brand'))
        left.addWidget(label('A clearer view of your copies.', 'side-note'))
        new = QPushButton('+  New scan'); new.clicked.connect(self.show_new); left.addWidget(new)
        open_report = QPushButton('Open a report…'); open_report.clicked.connect(self.choose_report); left.addWidget(open_report)
        left.addSpacing(12); left.addWidget(label('YOUR SCAN LIBRARY', 'side-note'))
        self.jobs = QListWidget(); self.jobs.setObjectName('library')
        self.jobs.currentItemChanged.connect(self.select_job); left.addWidget(self.jobs, 1)
        left.addWidget(label('Indexes and reports stay on this computer.', 'side-note'))
        left.addWidget(label('v' + __version__, 'side-note'))
        layout.addWidget(side)
        self.pages = QStackedWidget(); layout.addWidget(self.pages, 1)
        self.new_page = QScrollArea(); self.new_page.setWidgetResizable(True); self.new_page.setFrameShape(QFrame.Shape.NoFrame)
        new_content = QWidget(); new_content.setObjectName('content'); self.new_page.setWidget(new_content)
        outer = QVBoxLayout(new_content); outer.setContentsMargins(45, 38, 45, 32); outer.setSpacing(19)
        outer.addWidget(label('DUPLICATE DISCOVERY', 'eyebrow'))
        outer.addWidget(label('Find what’s already there.', 'title'))
        outer.addWidget(label('Compare file contents, explore the overlap, and find folders whose contents are preserved elsewhere.', 'subtitle'))
        card = QFrame(); card.setObjectName('card'); form = QVBoxLayout(card); form.setContentsMargins(26, 24, 26, 24); form.setSpacing(18)
        tabs = QHBoxLayout()
        self.local_button = QPushButton('Local folder'); self.ssh_button = QPushButton('SSH folder')
        for button in (self.local_button, self.ssh_button):
            button.setObjectName('source-tab'); button.setCheckable(True); tabs.addWidget(button)
        tabs.addStretch(); form.addLayout(tabs)
        self.source_pages = SourcePages()
        self.local_button.clicked.connect(lambda: self.source_kind('local'))
        self.ssh_button.clicked.connect(lambda: self.source_kind('ssh'))
        local = QWidget(); local_layout = QVBoxLayout(local); local_layout.setContentsMargins(0, 8, 0, 0); local_layout.setSpacing(10)
        local_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        local_layout.addWidget(label('Folder to scan'))
        self.local_path = QLineEdit(); self.local_path.setPlaceholderText('Choose a folder on this computer')
        choose = QPushButton('Browse…'); choose.clicked.connect(self.choose_local)
        row = QHBoxLayout(); row.addWidget(self.local_path, 1); row.addWidget(choose); local_layout.addLayout(row)
        local_layout.addWidget(label('File contents are read locally. Symlinks and other mounted filesystems are excluded.', 'subtitle'))
        self.source_pages.addWidget(local)
        remote = QWidget(); grid = QGridLayout(remote); grid.setContentsMargins(0, 8, 0, 0); grid.setSpacing(12)
        self.host = QLineEdit(); self.host.setPlaceholderText('server.example or SSH config alias')
        self.user = QLineEdit(); self.user.setPlaceholderText('SSH config / current user')
        self.port = QSpinBox(); self.port.setRange(0, 65535); self.port.setSpecialValueText('SSH config / 22')
        self.remote_path = QLineEdit('~'); self.remote_path.setPlaceholderText('/path/to/folder')
        for r, title, field in ((0, 'Host', self.host), (1, 'Username (optional)', self.user), (2, 'Port', self.port), (3, 'Remote folder', self.remote_path)):
            grid.addWidget(label(title), r, 0); grid.addWidget(field, r, 1)
        self.identity = QLineEdit(); self.identity.setPlaceholderText('Automatic · SSH agent and default keys')
        key = QPushButton('Choose…'); key.clicked.connect(self.choose_identity)
        keyrow = QHBoxLayout(); keyrow.addWidget(self.identity, 1); keyrow.addWidget(key)
        grid.addWidget(label('Private key (optional)'), 4, 0); grid.addLayout(keyrow, 4, 1)
        grid.addWidget(label('Uses your normal SSH keys first. Deckard asks for a password or key passphrase when needed. Nothing is installed on the host.', 'subtitle'), 5, 0, 1, 2)
        self.source_pages.addWidget(remote); form.addWidget(self.source_pages)
        options = QGroupBox('Scan options'); options_form = QFormLayout(options)
        self.workers = QSpinBox(); self.workers.setRange(1, 8); self.workers.setValue(2)
        self.workers.setMaximumWidth(110)
        options_form.addRow('Parallel workers', self.workers); form.addWidget(options)
        self.start = QPushButton('Start scan  →'); self.start.setObjectName('primary'); self.start.clicked.connect(self.start_new)
        form.addWidget(self.start, alignment=Qt.AlignmentFlag.AlignRight)
        outer.addWidget(card); outer.addStretch()
        outer.addWidget(label('Deckard is read-only. Its map compares regular-file contents; names, folder layout and file metadata can differ.', 'subtitle'))
        self.pages.addWidget(self.new_page)

        self.detail_page = QWidget(); detail = QVBoxLayout(self.detail_page); detail.setContentsMargins(35, 28, 35, 25); detail.setSpacing(17)
        head = QHBoxLayout(); titles = QVBoxLayout()
        titles.addWidget(label('SCAN WORKSPACE', 'eyebrow'))
        self.job_title = label('Your scan', 'title'); titles.addWidget(self.job_title)
        self.job_location = label('', 'subtitle'); titles.addWidget(self.job_location)
        head.addLayout(titles, 1)
        self.resume = QPushButton('Resume scan'); self.resume.clicked.connect(self.resume_current)
        self.pause = QPushButton('Pause'); self.pause.clicked.connect(self.pause_scan)
        self.map_button = QPushButton('Open duplicate map'); self.map_button.setObjectName('primary'); self.map_button.clicked.connect(self.open_current_report)
        head.addWidget(self.resume); head.addWidget(self.pause); head.addWidget(self.map_button); detail.addLayout(head)
        status_card = self.status_card = QFrame(); status_card.setObjectName('card'); status = QVBoxLayout(status_card); status.setContentsMargins(22, 18, 22, 18)
        self.phase = label('Ready'); status.addWidget(self.phase)
        self.progress = QProgressBar(); self.progress.setTextVisible(False); status.addWidget(self.progress)
        stats = QHBoxLayout(); self.stat_values = []
        for title in ('ITEMS VISITED', 'READ ON SOURCE', 'ELAPSED'):
            column = QVBoxLayout(); column.addWidget(label(title, 'metric-label')); value = label('—', 'metric'); column.addWidget(value); self.stat_values.append(value); stats.addLayout(column)
        status.addLayout(stats); detail.addWidget(status_card)
        self.detail_pages = QStackedWidget()
        progress_page = QWidget(); pl = QVBoxLayout(progress_page); pl.setContentsMargins(0, 0, 0, 0)
        self.note = label('Progress and diagnostics appear here.', 'subtitle'); pl.addWidget(self.note)
        self.logs = QPlainTextEdit(); self.logs.setReadOnly(True); self.logs.setMaximumBlockCount(600); pl.addWidget(self.logs, 1)
        self.detail_pages.addWidget(progress_page)
        profile = QWebEngineProfile.defaultProfile()
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self.web = QWebEngineView()
        self.web_page = ReportPage(self.web); self.web.setPage(self.web_page)
        self.detail_pages.addWidget(self.web)
        detail.addWidget(self.detail_pages, 1)
        self.pages.addWidget(self.detail_page)
        self.setCentralWidget(container)
        self.source_kind('local')

    def _menus(self):
        menu = self.menuBar().addMenu('File')
        for title, shortcut, callback in (('New scan', QKeySequence.StandardKey.New, self.show_new),
                                          ('Open report…', QKeySequence.StandardKey.Open, self.choose_report)):
            action = QAction(title, self); action.setShortcut(shortcut); action.triggered.connect(callback); menu.addAction(action)
        files = QAction('Show local scan data', self); files.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.library.root))))
        menu.addAction(files)
        diagnostics = QAction('Performance diagnostics', self)
        diagnostics.triggered.connect(self.show_diagnostics); menu.addAction(diagnostics)
        quit_action = QAction('Quit Deckard', self); quit_action.setShortcut(QKeySequence.StandardKey.Quit); quit_action.triggered.connect(self.close); menu.addAction(quit_action)

    def source_kind(self, kind):
        self.local_button.setChecked(kind == 'local'); self.ssh_button.setChecked(kind == 'ssh')
        self.source_pages.setCurrentIndex(0 if kind == 'local' else 1)

    def choose_local(self):
        path = QFileDialog.getExistingDirectory(self, 'Choose a folder to scan', str(Path.home()))
        if path:
            self.local_path.setText(path)

    def choose_identity(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose an SSH private key', str(Path.home() / '.ssh'))
        if path:
            self.identity.setText(path)

    def show_new(self):
        self.pages.setCurrentWidget(self.new_page)
        self.start.setEnabled(not self.is_running())

    def is_running(self):
        return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

    def start_new(self):
        if self.is_running():
            return
        try:
            kind = 'local' if self.local_button.isChecked() else 'ssh'
            root = (self.local_path if kind == 'local' else self.remote_path).text().strip()
            if not root:
                raise ValueError('Choose a folder to scan.')
            source = {'kind': kind, 'root': root, 'workers': self.workers.value()}
            if kind == 'local':
                path = Path(root).expanduser().resolve(strict=True)
                if not path.is_dir():
                    raise ValueError('Choose an existing folder.')
                source['root'] = str(path)
            else:
                host, user, port = validate_target(self.host.text().strip(), self.user.text().strip(), self.port.value())
                source.update(host=host, user=user, port=port, identity=self.identity.text().strip())
            self.current = self.library.create(source)
            self.start_job(self.current)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Cannot start scan', str(exc))

    def refresh_library(self):
        previous = str(self.current) if self.current else None
        self.jobs.blockSignals(True); self.jobs.clear()
        for job in self.library.jobs():
            name = Path(job['source']['root'].rstrip('/')).name or job['source']['root']
            item = QListWidgetItem(name + '\n' + job.get('status', 'new').replace('_', ' ').title())
            item.setToolTip(location(job)); item.setData(Qt.ItemDataRole.UserRole, job['folder']); self.jobs.addItem(item)
            if job['folder'] == previous:
                self.jobs.setCurrentItem(item)
        self.jobs.blockSignals(False)

    def select_job(self, item, previous=None):
        if item is None:
            return
        self.current = Path(item.data(Qt.ItemDataRole.UserRole))
        if self.current != self.active_folder:
            self.logs.clear()
        self.show_job()

    def show_job(self):
        if self.current is None:
            return
        cfg = json.loads((self.current / 'job.json').read_text())
        name = Path(cfg['source']['root'].rstrip('/')).name or cfg['source']['root']
        self.job_title.setText(name)
        self.job_location.setText(location(cfg))
        running = self.is_running() and self.current == self.active_folder
        self.phase.setText(cfg.get('phase', 'Ready'))
        self.pause.setVisible(running); self.resume.setVisible(not running)
        self.resume.setEnabled(not self.is_running())
        self.resume.setText('Refresh scan' if cfg.get('status') == 'complete' else 'Resume scan')
        ready = (self.current / 'report/manifest.json').exists()
        self.map_button.setEnabled(ready and not running)
        self.progress.setRange(0, 0 if running else 100); self.progress.setValue(100 if cfg.get('status') == 'complete' else 0)
        self.note.setText(cfg.get('error') or ('The index is saved locally. Resume continues from committed checkpoints.' if not running else 'You can pause this scan and continue later.'))
        self.pages.setCurrentWidget(self.detail_page)
        self.status_card.show()
        self.detail_pages.setCurrentIndex(0)
        self.update_metrics()

    def resume_current(self):
        if self.current is not None and not self.is_running():
            cfg = json.loads((self.current / 'job.json').read_text())
            self.start_job(self.current, refresh=cfg.get('status') == 'complete')

    def start_job(self, folder, refresh=False):
        self.stop_report()
        self.active_folder = self.current = Path(folder)
        self.metrics = {}; self.buffer.clear(); self.started = time.monotonic(); self.terminal_event = False
        self.logs.clear(); self.start.setEnabled(False)
        self.process = QProcess(self)
        # Own a separate process group so forced cancellation also closes the
        # SSH master and worker channels, even if the backend is unresponsive.
        unix = QProcess.UnixProcessParameters()
        unix.flags = QProcess.UnixProcessFlag.CreateNewSession | QProcess.UnixProcessFlag.ResetSignalHandlers
        self.process.setUnixProcessParameters(unix)
        self.process.started.connect(lambda: self.capture_started())
        env = QProcessEnvironment.systemEnvironment()
        for key, value in self.auth.environment().items():
            env.insert(key, value)
        self.process.setProcessEnvironment(env)
        self.process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))
        self.process.readyReadStandardOutput.connect(self.read_events)
        self.process.readyReadStandardError.connect(self.read_errors)
        self.process.finished.connect(self.scan_finished)
        self.process.errorOccurred.connect(self.scan_process_error)
        args = ['-u', '-m', 'deckard.scan_job', '--job', str(folder)]
        if refresh:
            args.append('--refresh')
        self.process.start(sys.executable, args)
        self.show_job(); self.refresh_library()

    def capture_started(self):
        self.worker_pid = self.process.processId()
        QTimer.singleShot(400, self.refresh_library)

    def scan_process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            message = 'Could not start the scan process: ' + self.process.errorString()
            cfg = json.loads((self.active_folder / 'job.json').read_text())
            cfg.update(status='error', phase='Could not start', error=message)
            atomic_json(self.active_folder / 'job.json', cfg)
            self.start.setEnabled(True); self.refresh_library(); self.show_job()
            if self.closing:
                self.finish_close()

    def read_events(self):
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        while b'\n' in self.buffer:
            raw, _, tail = self.buffer.partition(b'\n'); self.buffer = bytearray(tail)
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            kind = event.get('event')
            if kind == 'progress':
                self.metrics = event['record']
                if not self.terminal_event:
                    phase = self.metrics.get('phase', 'Scanning')
                    if self.current == self.active_folder:
                        self.phase.setText(phase.replace('.', ' · ').replace('_', ' ').capitalize())
                self.update_metrics()
            elif kind == 'log':
                if self.current == self.active_folder:
                    self.logs.appendPlainText(event.get('message', ''))
            elif kind == 'phase' and self.current == self.active_folder:
                self.phase.setText(event['phase'])
            elif kind in ('complete', 'paused', 'failed'):
                self.terminal_event = True
                if kind == 'failed':
                    self.note.setText(event.get('message', 'Could not finish the scan.'))
                    self.logs.appendPlainText(event.get('message', ''))

    def read_errors(self):
        text = bytes(self.process.readAllStandardError()).decode('utf-8', 'replace').strip()
        if text and self.current == self.active_folder:
            self.logs.appendPlainText(text)

    def update_metrics(self):
        if self.current is None:
            return
        active = self.is_running() and self.current == self.active_folder
        cfg = json.loads((self.current / 'job.json').read_text())
        c = self.metrics.get('counters', {}) if active else {}
        total = c.get('entries', 0) if active else cfg.get('summary', {}).get('files', 0)
        self.stat_values[0].setText(f'{int(total):,}')
        self.stat_values[1].setText(size(c.get('bytes_read', 0)))
        elapsed = time.monotonic() - self.started if active else cfg.get('elapsed_seconds', 0)
        self.stat_values[2].setText(f'{int(elapsed)//3600:02}:{int(elapsed)//60%60:02}:{int(elapsed)%60:02}')
        phase = self.metrics.get('phase', '')
        stage = phase.split('.')[0]
        gauge = self.metrics.get('gauges', {}).get(stage + '_pending_at_start', 0)
        if active and phase.endswith('.hash') and gauge:
            self.progress.setRange(0, 1000); self.progress.setValue(min(1000, int(c.get(stage + '_completed', 0) * 1000 / gauge)))
        elif active:
            self.progress.setRange(0, 0)

    def pause_scan(self):
        if self.is_running():
            self.auth.cancel_pending()
            self.phase.setText('Saving checkpoints…'); self.pause.setEnabled(False)
            proc = self.process
            proc.terminate()
            QTimer.singleShot(15000, lambda: self.force_stop(proc))

    @staticmethod
    def force_stop(proc):
        if proc.state() != QProcess.ProcessState.NotRunning:
            pid = proc.processId()
            try:
                if pid and os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
                    return
            except ProcessLookupError:
                return
            proc.kill()

    def scan_finished(self, code, status):
        self.read_events(); self.read_errors()
        if self.auth_dialog is not None:
            self.auth_dialog.reject()
        self.auth.cancel_pending()
        self.pause.setEnabled(True); self.start.setEnabled(True)
        path = self.active_folder
        cfg = json.loads((path / 'job.json').read_text())
        if cfg.get('status') in ('running', 'reporting') and cfg.get('pid') == self.worker_pid:
            cfg.update(status='paused' if code != 0 else 'error', phase='Paused' if code != 0 else 'Could not finish')
            atomic_json(path / 'job.json', cfg)
        self.refresh_library()
        if self.current == path:
            self.show_job()
            if cfg.get('status') == 'complete':
                self.open_report(path / 'report')
        if self.closing:
            self.finish_close()

    @Slot(object)
    def authentication_requested(self, request):
        if not self.is_running() or request.done.is_set():
            request.answer(None); return
        if request.kind == 'none':
            self.note.setText(request.prompt); request.answer(''); return
        confirm = request.kind == 'confirm' or 'yes/no' in request.prompt.lower()
        if confirm:
            dialog = QMessageBox(self)
            dialog.setWindowTitle('SSH host confirmation')
            dialog.setTextFormat(Qt.TextFormat.PlainText); dialog.setText(request.prompt)
            dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            dialog.setDefaultButton(QMessageBox.StandardButton.No)
            self.auth_dialog = dialog
            answer = dialog.exec() == QMessageBox.StandardButton.Yes
            self.auth_dialog = None; request.answer('yes' if answer else 'no')
        else:
            dialog = QInputDialog(self); dialog.setWindowTitle('SSH sign-in')
            dialog.setLabelText(request.prompt); dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
            for item in dialog.findChildren(QLabel):
                item.setTextFormat(Qt.TextFormat.PlainText)
            self.auth_dialog = dialog
            accepted = dialog.exec(); response = dialog.textValue() if accepted else None
            dialog.setTextValue(''); self.auth_dialog = None
            request.answer(response)
            if response is None:
                self.pause_scan()

    def choose_report(self):
        path = QFileDialog.getExistingDirectory(self, 'Open a Deckard report', str(self.library.root))
        if path:
            folder = Path(path)
            if not (folder / 'report.sqlite3').exists():
                QMessageBox.warning(self, 'No report found', 'Choose a folder containing report.sqlite3 and manifest.json.')
                return
            self.current = None
            self.jobs.clearSelection()
            self.job_title.setText(folder.name)
            self.job_location.setText(str(folder))
            self.resume.hide(); self.pause.hide(); self.map_button.setEnabled(False)
            self.open_report(folder)

    def show_diagnostics(self):
        if self.current is None:
            QMessageBox.information(self, 'Performance diagnostics', 'Select a scan in the library first.')
            return
        dialog = QDialog(self); dialog.setWindowTitle('Deckard · Performance diagnostics'); dialog.resize(960, 660)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label('Operations and source throughput over the latest 60 seconds', 'subtitle'))
        output = QPlainTextEdit(); output.setReadOnly(True); layout.addWidget(output)
        process = QProcess(dialog)
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))
        process.readyReadStandardOutput.connect(lambda: output.insertPlainText(bytes(process.readAllStandardOutput()).decode('utf8', 'replace')))
        process.readyReadStandardError.connect(lambda: output.insertPlainText(bytes(process.readAllStandardError()).decode('utf8', 'replace')))
        process.start(sys.executable, ['-u', '-m', 'deckard.diagnostics', '--state-dir', str(self.current)])
        dialog.exec()
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill(); process.waitForFinished(3000)

    def open_current_report(self):
        if self.current:
            self.open_report(self.current / 'report')

    def open_report(self, folder):
        if not (folder / 'report.sqlite3').exists():
            QMessageBox.warning(self, 'No report found', 'Choose a report folder containing report.sqlite3 and manifest.json.')
            return
        self.stop_report()
        self.pages.setCurrentWidget(self.detail_page)
        self.status_card.hide()
        self.detail_pages.setCurrentIndex(1)
        self.report_process = QProcess(self); self.report_buffer.clear()
        self.report_process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))
        self.report_process.readyReadStandardOutput.connect(self.read_report_url)
        self.report_process.readyReadStandardError.connect(lambda: self.logs.appendPlainText(bytes(self.report_process.readAllStandardError()).decode('utf8', 'replace')))
        self.report_process.errorOccurred.connect(self.report_process_error)
        self.report_process.finished.connect(self.report_exited)
        self.report_process.start(sys.executable, ['-u', '-m', 'deckard.report', 'serve', '--report-dir', str(folder), '--port', '0'])

    def report_process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.note.setText('Could not open the report: ' + self.report_process.errorString())
            self.detail_pages.setCurrentIndex(0)

    def report_exited(self, code, status):
        if code:
            self.note.setText('The report viewer stopped. Details appear below; reopen the report to retry.')
            self.detail_pages.setCurrentIndex(0)

    def read_report_url(self):
        self.report_buffer.extend(bytes(self.report_process.readAllStandardOutput()))
        while b'\n' in self.report_buffer:
            raw, _, rest = self.report_buffer.partition(b'\n'); self.report_buffer = bytearray(rest)
            line = raw.decode('utf8', 'replace')
            if line.startswith('Deckard report: '):
                url = QUrl(line.split(' ', 2)[2])
                self.web_page.allowed_port = url.port()
                self.web.load(QUrl(url.toString() + '?embedded=1'))

    def stop_report(self):
        if self.report_process is not None:
            proc = self.report_process; self.report_process = None
            proc.readyReadStandardOutput.disconnect()
            proc.readyReadStandardError.disconnect()
            proc.errorOccurred.disconnect(); proc.finished.disconnect()
            proc.kill(); proc.waitForFinished(3000)
            proc.deleteLater()
        self.web.setUrl(QUrl('about:blank'))

    def closeEvent(self, event):
        if self.is_running():
            self.closing = True; self.pause_scan(); event.ignore()
        else:
            self.stop_report(); self.auth.close(); event.accept()

    def finish_close(self):
        self.closing = False
        self.close()


def create_application(argv=None):
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setStyle('Fusion')
    palette = QPalette()
    for role, color in ((QPalette.ColorRole.Window, '#f5f4ee'), (QPalette.ColorRole.WindowText, '#26352d'),
                        (QPalette.ColorRole.Base, '#fffef9'), (QPalette.ColorRole.Text, '#26352d'),
                        (QPalette.ColorRole.Button, '#fffef9'), (QPalette.ColorRole.ButtonText, '#35533e'),
                        (QPalette.ColorRole.Highlight, '#dbe8d2'), (QPalette.ColorRole.HighlightedText, '#26352d')):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setApplicationName('Deckard'); app.setOrganizationName('Deckard')
    app.setApplicationDisplayName('Deckard')
    app.setWindowIcon(QIcon(str(Path(__file__).parent / 'assets/deckard.svg')))
    return app


def main():
    app = create_application()
    window = MainWindow(); window.show()
    return app.exec()
