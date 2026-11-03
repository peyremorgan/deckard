"""Native Qt desktop shell around the isolated scanner and local report API."""
from collections import deque
import base64
import contextlib
import json
import os
import signal
import sqlite3
from pathlib import Path
import sys
import threading
import time

from PySide6.QtCore import Qt, QProcess, QProcessEnvironment, QTimer, QUrl, Slot, Signal, QFile, QIODevice, QObject
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence, QActionGroup, QColor
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QFrame, QLabel, QPushButton, QLineEdit, QFileDialog, QStackedWidget,
    QSpinBox, QFormLayout, QGroupBox, QListWidget, QListWidgetItem, QProgressBar,
    QPlainTextEdit, QMessageBox, QInputDialog, QScrollArea)
from PySide6.QtWidgets import QDialog
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView

from PySide6.QtWebChannel import QWebChannel

from .theme import ThemeManager, PALETTES
from .auth import AuthBridge
from .storage import Library, atomic_json
from .ssh import validate_target
from .folders import RemoteFolders, selected_folders
from .report.model import readonly


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


class FolderSignals(QObject):
    ready = Signal(object, object, str)


class ReportActions(QObject):
    """Open only folders identified by the active report's item tokens."""
    foldersOpened = Signal(str)
    shutdownFinished = Signal(str)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.remote = RemoteFolders()
        self.worker = None
        self.busy = False
        self.cancelled = threading.Event()
        self.stopping = False
        self.closed = False
        self.signals = FolderSignals(self)
        self.signals.ready.connect(self._open_ready)
        self.shutdownFinished.connect(self.shutdown_complete)

    def active_report(self):
        window = self.window
        url = window.web.url()
        if (window.report_folder is None or url.scheme() != 'http' or
                url.host() != '127.0.0.1' or url.port() != window.web_page.allowed_port):
            return None
        config = json.loads((window.report_folder.parent / 'job.json').read_text())
        return (window.report_folder, config['source']) if config['source']['kind'] in ('local', 'ssh') else None

    @Slot(result=bool)
    def canOpenFolders(self):
        try:
            return not self.stopping and not self.closed and self.active_report() is not None
        except (OSError, ValueError, KeyError, TypeError):
            return False

    @Slot('QStringList')
    def openFolders(self, tokens):
        if self.busy or self.stopping or self.closed:
            return
        try:
            active = self.active_report()
            if active is None:
                raise ValueError('Opening folders requires a scan opened in the desktop app.')
            if not 1 <= len(tokens) <= 2:
                raise ValueError('Select a folder comparison first.')
            environment = dict(os.environ)
            if hasattr(self.window, 'auth'):
                environment.update(self.window.auth.environment())
            environment['DECKARD_AUTH_PURPOSE'] = 'folders'
            self.busy = True
            self.worker = threading.Thread(target=self._prepare, args=(active, list(tokens), environment), daemon=True)
            self.worker.start()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.foldersOpened.emit(str(exc))

    def _prepare(self, active, tokens, environment):
        folders, error = [], ''
        try:
            report, target = active
            with contextlib.closing(readonly(report / 'report.sqlite3')) as db:
                metadata = db.execute("SELECT value FROM metadata WHERE key='source_root_base64'").fetchone()
                root = base64.b64decode(json.loads(metadata[0]), validate=True)
                paths = []
                for token in tokens:
                    kind, ident = token.split(':', 1)
                    if kind == 'd':
                        row = db.execute('SELECT path FROM nodes WHERE id=?', (int(ident),)).fetchone()
                    elif kind == 'f':
                        row = db.execute('SELECT n.path FROM entries e JOIN nodes n ON n.id=e.parent WHERE e.id=?',
                                         (int(ident),)).fetchone()
                    else:
                        raise ValueError('Select individual folders or files first.')
                    if row is None:
                        raise ValueError('The selected item is no longer in this report.')
                    path = bytes(row[0])
                    if path.startswith(b'/') or b'..' in path.split(b'/') or b'\0' in path:
                        raise ValueError('The selected folder is outside the scan source.')
                    paths.append(path)
            if target['kind'] == 'ssh':
                folders = self.remote.folders(target, root, paths, environment, self.cancelled)
            else:
                folders = selected_folders(os.fsdecode(root), paths)
        except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError, RuntimeError, sqlite3.Error) as exc:
            error = str(exc)
        except KeyboardInterrupt:
            error = 'Opening the SSH folder was cancelled.'
        self.signals.ready.emit(active, folders, error)

    @Slot(object, object, str)
    def _open_ready(self, active, folders, error):
        self.busy = False
        try:
            if self.stopping or self.active_report() != active:
                error = 'The report changed before the folders could be opened.'
            elif not error:
                failed = [str(folder) for folder in folders
                          if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))]
                error = 'Could not open: ' + ', '.join(failed) if failed else ''
        except (OSError, ValueError, KeyError, TypeError) as exc:
            error = str(exc)
        self.foldersOpened.emit(error)

    def close(self):
        if self.closed:
            return True
        if self.stopping:
            return False
        if not self.busy and not self.remote.mounts:
            self.closed = True
            return True
        self.stopping = True
        self.cancelled.set()
        threading.Thread(target=self._shutdown, daemon=True).start()
        return False

    def _shutdown(self):
        error = ''
        if self.worker is not None:
            self.worker.join()
        try:
            self.remote.close()
        except (OSError, RuntimeError) as exc:
            error = str(exc)
        self.shutdownFinished.emit(error)

    @Slot(str)
    def shutdown_complete(self, error):
        self.stopping = False
        self.closed = not error
        if error:
            self.cancelled.clear()


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
        self.report_folder = None
        self.pending_removal = None
        self.report_buffer = bytearray()
        self.buffer = bytearray()
        self.metrics = {}
        self.closing = False
        self.terminal_event = False
        self.started = 0
        self.worker_pid = None
        self.auth_dialog = None
        self.auth_request = None
        self.auth = AuthBridge(self)
        self.auth.requested.connect(self.authentication_requested)
        self.setWindowTitle('Deckard')
        self.resize(1360, 960)
        self.setMinimumSize(1000, 720)
        self.appearance = ThemeManager(self.library.root / 'preferences.json', self)
        self._build()
        self._menus()
        self.appearance.changed.connect(self.appearance_changed)
        self.appearance_changed(self.appearance.preference, self.appearance.resolved)
        self.refresh_library()
        self.show_new()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_metrics)
        self.timer.start(1000)

    def _build(self):
        container = QWidget(); container.setObjectName('content')
        layout = QHBoxLayout(container); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        side = QFrame(); side.setObjectName('sidebar'); side.setFixedWidth(194)
        left = QVBoxLayout(side); left.setContentsMargins(6, 6, 6, 6); left.setSpacing(6)
        left.addWidget(label('DECKARD', 'brand'))
        new = QPushButton('New scan'); new.clicked.connect(self.show_new); left.addWidget(new)
        open_report = QPushButton('Open report…'); open_report.clicked.connect(self.choose_report); left.addWidget(open_report)
        left.addWidget(label('SCANS', 'section'))
        self.jobs = QListWidget(); self.jobs.setObjectName('library')
        self.jobs.currentItemChanged.connect(self.select_job); left.addWidget(self.jobs, 1)
        layout.addWidget(side)
        self.pages = QStackedWidget(); layout.addWidget(self.pages, 1)
        self.new_page = QScrollArea(); self.new_page.setWidgetResizable(True); self.new_page.setFrameShape(QFrame.Shape.NoFrame)
        new_content = QWidget(); new_content.setObjectName('content'); self.new_page.setWidget(new_content)
        outer = QVBoxLayout(new_content); outer.setContentsMargins(12, 12, 12, 12); outer.setSpacing(10)
        outer.addWidget(label('NEW SCAN', 'section'))
        card = QGroupBox('Source'); form = QVBoxLayout(card); form.setContentsMargins(12, 20, 12, 12); form.setSpacing(10)
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
        self.local_path = QLineEdit(); self.local_path.setPlaceholderText('/path/to/folder')
        choose = QPushButton('Browse…'); choose.clicked.connect(self.choose_local)
        row = QHBoxLayout(); row.addWidget(self.local_path, 1); row.addWidget(choose); local_layout.addLayout(row)
        self.source_pages.addWidget(local)
        remote = QWidget(); grid = QGridLayout(remote); grid.setContentsMargins(0, 8, 0, 0); grid.setSpacing(8)
        self.host = QLineEdit(); self.host.setPlaceholderText('server.example or SSH config alias')
        self.user = QLineEdit(); self.user.setPlaceholderText('SSH config / current user')
        self.port = QSpinBox(); self.port.setRange(0, 65535); self.port.setSpecialValueText('SSH config / 22')
        self.remote_path = QLineEdit('~'); self.remote_path.setPlaceholderText('/path/to/folder')
        for r, title, field in ((0, 'Host', self.host), (1, 'Username (optional)', self.user), (2, 'Port', self.port), (3, 'Remote folder', self.remote_path)):
            grid.addWidget(label(title), r, 0); grid.addWidget(field, r, 1)
        self.identity = QLineEdit(); self.identity.setPlaceholderText('SSH agent / default keys')
        key = QPushButton('Choose…'); key.clicked.connect(self.choose_identity)
        keyrow = QHBoxLayout(); keyrow.addWidget(self.identity, 1); keyrow.addWidget(key)
        grid.addWidget(label('Private key (optional)'), 4, 0); grid.addLayout(keyrow, 4, 1)
        self.source_pages.addWidget(remote); form.addWidget(self.source_pages)
        options = QGroupBox('Scan options'); options_form = QFormLayout(options)
        self.workers = QSpinBox(); self.workers.setRange(1, 8); self.workers.setValue(2)
        self.workers.setMaximumWidth(110)
        options_form.addRow('Parallel workers', self.workers); form.addWidget(options)
        self.start = QPushButton('Start scan'); self.start.setObjectName('primary'); self.start.clicked.connect(self.start_new)
        form.addWidget(self.start, alignment=Qt.AlignmentFlag.AlignRight)
        outer.addWidget(card); outer.addStretch()
        self.pages.addWidget(self.new_page)

        self.detail_page = QWidget(); detail = QVBoxLayout(self.detail_page); detail.setContentsMargins(10, 10, 10, 10); detail.setSpacing(8)
        head = QHBoxLayout(); titles = QVBoxLayout()
        self.job_title = label('Scan', 'title'); titles.addWidget(self.job_title)
        self.job_location = label('', 'path'); titles.addWidget(self.job_location)
        head.addLayout(titles, 1)
        self.resume = QPushButton('Resume scan'); self.resume.clicked.connect(self.resume_current)
        self.pause = QPushButton('Pause'); self.pause.clicked.connect(self.pause_scan)
        self.map_button = QPushButton('Duplicate map'); self.map_button.setObjectName('primary'); self.map_button.clicked.connect(self.open_current_report)
        self.remove = QPushButton('Remove scan'); self.remove.clicked.connect(self.remove_current)
        head.addWidget(self.resume); head.addWidget(self.pause); head.addWidget(self.map_button); head.addWidget(self.remove); detail.addLayout(head)
        status_card = self.status_card = QFrame(); status_card.setObjectName('card'); status = QVBoxLayout(status_card); status.setContentsMargins(10, 8, 10, 8)
        self.phase = label('Ready'); status.addWidget(self.phase)
        self.progress = QProgressBar(); self.progress.setTextVisible(False); status.addWidget(self.progress)
        stats = QHBoxLayout(); self.stat_values = []
        for title in ('ITEMS VISITED', 'READ ON SOURCE', 'ELAPSED'):
            column = QVBoxLayout(); column.addWidget(label(title, 'metric-label')); value = label('—', 'metric'); column.addWidget(value); self.stat_values.append(value); stats.addLayout(column)
        status.addLayout(stats); detail.addWidget(status_card)
        self.detail_pages = QStackedWidget()
        progress_page = QWidget(); pl = QVBoxLayout(progress_page); pl.setContentsMargins(0, 0, 0, 0)
        self.note = label(''); self.note.hide(); pl.addWidget(self.note)
        self.logs = QPlainTextEdit(); self.logs.setReadOnly(True); self.logs.setMaximumBlockCount(600); pl.addWidget(self.logs, 1)
        self.detail_pages.addWidget(progress_page)
        profile = QWebEngineProfile.defaultProfile()
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self.web = QWebEngineView()
        self.web_page = ReportPage(self.web); self.web.setPage(self.web_page)
        self.web_channel = QWebChannel(self.web_page)
        self.web_channel.registerObject('appearance', self.appearance)
        self.report_actions = ReportActions(self)
        self.report_actions.shutdownFinished.connect(self.folders_shutdown)
        self.web_channel.registerObject('reportActions', self.report_actions)
        self.web_page.setWebChannel(self.web_channel)
        resource = QFile(':/qtwebchannel/qwebchannel.js')
        if not resource.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError('Qt WebChannel JavaScript is unavailable')
        self.channel_source = bytes(resource.readAll()).decode('utf-8')
        resource.close()
        self.appearance_script = None
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

        settings = self.menuBar().addMenu('Settings')
        themes = settings.addMenu('Theme')
        group = QActionGroup(self); group.setExclusive(True)
        self.theme_actions = {}
        for mode in ('light', 'dark', 'system'):
            action = QAction(mode.title(), self); action.setCheckable(True)
            action.triggered.connect(lambda checked, value=mode: self.appearance.setPreference(value))
            group.addAction(action); themes.addAction(action)
            self.theme_actions[mode] = action

    def appearance_changed(self, preference, resolved):
        self.theme_actions[preference].setChecked(True)
        self.web_page.setBackgroundColor(QColor(PALETTES[resolved]['face']))
        if self.appearance_script is not None:
            self.web_page.scripts().remove(self.appearance_script)
        script = QWebEngineScript()
        script.setName('deckard-appearance')
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        script.setSourceCode('window.deckardAppearance = ' + json.dumps(self.appearance.snapshot()) +
                             ';\n' + self.channel_source)
        self.web_page.scripts().insert(script)
        self.appearance_script = script

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
        self.start.setEnabled(not self.is_running() and self.pending_removal is None)

    def is_running(self):
        return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

    def start_new(self):
        if self.is_running() or self.pending_removal is not None:
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
        self.show_job()

    def append_log(self, folder, message):
        if folder is not None:
            with (folder / 'scan.log').open('a', encoding='utf-8') as stream:
                stream.write(message + '\n')
        if self.current == folder:
            self.logs.appendPlainText(message)

    def load_log(self):
        try:
            with (self.current / 'scan.log').open(encoding='utf-8', errors='replace') as stream:
                # Keep the viewer bounded while retaining the full history on disk.
                text = ''.join(deque(stream, maxlen=self.logs.maximumBlockCount()))
        except FileNotFoundError:
            text = ''
        self.logs.setPlainText(text.removesuffix('\n'))
        bar = self.logs.verticalScrollBar()
        bar.setValue(bar.maximum())

    def show_job(self):
        if self.current is None:
            return
        self.load_log()
        cfg = json.loads((self.current / 'job.json').read_text())
        name = Path(cfg['source']['root'].rstrip('/')).name or cfg['source']['root']
        self.job_title.setText(name)
        self.job_location.setText(location(cfg))
        running = self.is_running() and self.current == self.active_folder
        self.phase.setText(cfg.get('phase', 'Ready'))
        self.remove.show(); self.remove.setEnabled(self.pending_removal is None)
        self.pause.setVisible(running); self.resume.setVisible(not running)
        self.resume.setEnabled(not self.is_running() and self.pending_removal is None)
        self.resume.setText('Refresh scan' if cfg.get('status') == 'complete' else 'Resume scan')
        ready = (self.current / 'report/manifest.json').exists()
        self.map_button.setEnabled(ready and not running)
        self.progress.setRange(0, 0 if running else 100); self.progress.setValue(100 if cfg.get('status') == 'complete' else 0)
        self.note.setText(cfg.get('error') or '')
        self.note.setVisible(bool(self.note.text()))
        self.pages.setCurrentWidget(self.detail_page)
        self.status_card.show()
        self.detail_pages.setCurrentIndex(0)
        self.update_metrics()
        if self.pending_removal == self.current:
            self.phase.setText('Stopping scan before removal…')
        if cfg.get('status') == 'complete' and ready and not running:
            self.open_current_report()

    def remove_current(self):
        if self.current is None or self.pending_removal is not None:
            return
        folder = self.current
        dialog = QMessageBox(self)
        dialog.setWindowTitle('Remove scan')
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText('Remove this scan and all of its saved scan data and reports?')
        dialog.setInformativeText('A running scan will be stopped first. Source files are kept.')
        dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        answer = dialog.exec()
        dialog.deleteLater()
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.pending_removal = folder
        self.remove.setEnabled(False); self.resume.setEnabled(False); self.start.setEnabled(False)
        if self.is_running() and self.active_folder == folder:
            self.pause_scan()
            self.phase.setText('Stopping scan before removal…')
        else:
            self.finish_removal()

    def finish_removal(self):
        folder = self.pending_removal
        if folder is None or (self.is_running() and self.active_folder == folder):
            return
        try:
            if self.report_folder == (folder / 'report').resolve() and not self.stop_report():
                raise RuntimeError('The report viewer is still stopping. Try removing the scan again.')
            self.library.remove(folder)
        except (OSError, ValueError, RuntimeError) as exc:
            self.pending_removal = None
            self.remove.setEnabled(True); self.start.setEnabled(not self.is_running())
            self.refresh_library()
            if self.current is not None:
                self.show_job()
            QMessageBox.warning(self, 'Cannot remove scan', str(exc))
            return
        self.pending_removal = None
        if self.active_folder == folder:
            self.active_folder = None; self.worker_pid = None
        selected = self.current == folder
        if selected:
            self.current = None
        self.remove.setEnabled(True); self.start.setEnabled(not self.is_running())
        self.resume.setEnabled(not self.is_running())
        self.refresh_library()
        if selected:
            if self.jobs.count():
                self.jobs.setCurrentRow(0)
            else:
                self.show_new()

    def resume_current(self):
        if self.current is not None and not self.is_running() and self.pending_removal is None:
            cfg = json.loads((self.current / 'job.json').read_text())
            self.start_job(self.current, refresh=cfg.get('status') == 'complete')

    def start_job(self, folder, refresh=False):
        if self.is_running() or self.pending_removal is not None:
            return
        self.stop_report()
        self.active_folder = self.current = Path(folder)
        self.metrics = {}; self.buffer.clear(); self.started = time.monotonic(); self.terminal_event = False
        self.start.setEnabled(False)
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
            self.append_log(self.active_folder, message)
            if self.pending_removal == self.active_folder:
                self.finish_removal()
            else:
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
                self.append_log(self.active_folder, event.get('message', ''))
            elif kind == 'phase' and self.current == self.active_folder:
                self.phase.setText(event['phase'])
            elif kind in ('complete', 'paused', 'failed'):
                self.terminal_event = True
                if kind == 'failed':
                    if self.current == self.active_folder:
                        self.note.setText(event.get('message', 'Could not finish the scan.'))
                        self.note.show()
                    self.append_log(self.active_folder, event.get('message', ''))

    def read_errors(self):
        text = bytes(self.process.readAllStandardError()).decode('utf-8', 'replace').strip()
        if text:
            self.append_log(self.active_folder, text)

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
            self.auth.cancel_pending('scan')
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
        if self.auth_dialog is not None and self.auth_request.purpose == 'scan':
            self.auth_dialog.reject()
        self.auth.cancel_pending('scan')
        self.pause.setEnabled(True); self.start.setEnabled(True)
        path = self.active_folder
        cfg = json.loads((path / 'job.json').read_text())
        if cfg.get('status') in ('running', 'reporting') and cfg.get('pid') == self.worker_pid:
            cfg.update(status='paused' if code != 0 else 'error', phase='Paused' if code != 0 else 'Could not finish')
            atomic_json(path / 'job.json', cfg)
        if self.pending_removal == path:
            self.finish_removal()
        else:
            self.refresh_library()
            if self.current == path:
                self.show_job()
        if self.closing:
            self.finish_close()

    @Slot(object)
    def authentication_requested(self, request):
        folders = request.purpose == 'folders'
        if (self.closing or (not self.report_actions.busy if folders else not self.is_running()) or
                self.pending_removal is not None or request.done.is_set()):
            request.answer(None); return
        if self.auth_dialog is not None:
            QTimer.singleShot(100, lambda: self.authentication_requested(request))
            return
        if request.kind == 'none':
            self.note.setText(request.prompt); self.note.show(); request.answer(''); return
        self.auth_request = request
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
            if response is None and not folders:
                self.pause_scan()
        self.auth_request = None

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
            self.resume.hide(); self.pause.hide(); self.remove.hide(); self.map_button.setEnabled(False)
            self.open_report(folder)

    def show_diagnostics(self):
        if self.current is None:
            QMessageBox.information(self, 'Performance diagnostics', 'Select a scan in the library first.')
            return
        dialog = QDialog(self); dialog.setWindowTitle('Deckard · Performance diagnostics'); dialog.resize(960, 660)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label('PERFORMANCE / 60 s', 'section'))
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
        folder = Path(folder).resolve()
        if not (folder / 'report.sqlite3').exists():
            QMessageBox.warning(self, 'No report found', 'Choose a report folder containing report.sqlite3 and manifest.json.')
            return
        self.stop_report()
        self.report_folder = folder
        self.pages.setCurrentWidget(self.detail_page)
        self.status_card.hide()
        self.detail_pages.setCurrentIndex(1)
        self.report_process = QProcess(self); self.report_buffer.clear()
        self.report_process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))
        self.report_process.readyReadStandardOutput.connect(self.read_report_url)
        log_folder = self.current
        self.report_process.readyReadStandardError.connect(lambda: self.append_log(log_folder, bytes(self.report_process.readAllStandardError()).decode('utf8', 'replace')))
        self.report_process.errorOccurred.connect(self.report_process_error)
        self.report_process.finished.connect(self.report_exited)
        self.report_process.start(sys.executable, ['-u', '-m', 'deckard.report', 'serve', '--report-dir', str(folder), '--port', '0'])

    def report_process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.note.setText('Could not open the report: ' + self.report_process.errorString())
            self.note.show()
            self.detail_pages.setCurrentIndex(0)

    def report_exited(self, code, status):
        if code:
            self.note.setText('The report viewer stopped. Details appear below; reopen the report to retry.')
            self.note.show()
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
            if proc.state() != QProcess.ProcessState.NotRunning:
                self.report_process = proc
                return False
            proc.deleteLater()
        self.report_folder = None
        self.web.setUrl(QUrl('about:blank'))
        return True

    def closeEvent(self, event):
        if self.is_running():
            self.closing = True; self.pause_scan(); event.ignore()
        else:
            self.closing = True
            if self.auth_dialog is not None:
                self.auth_dialog.reject()
            self.auth.cancel_pending()
            if self.report_actions.close():
                self.stop_report(); self.auth.close(); event.accept()
            else:
                event.ignore()

    @Slot(str)
    def folders_shutdown(self, error):
        if error:
            self.closing = False
            QMessageBox.warning(self, 'Could not disconnect SSH folders', error)
        else:
            self.close()

    def finish_close(self):
        self.closing = False
        self.close()


def create_application(argv=None):
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setStyle('Fusion')
    app.setApplicationName('Deckard'); app.setOrganizationName('Deckard')
    app.setApplicationDisplayName('Deckard')
    app.setWindowIcon(QIcon(str(Path(__file__).parent / 'assets/deckard.svg')))
    return app


def main():
    app = create_application()
    window = MainWindow(); window.show()
    return app.exec()
