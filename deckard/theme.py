"""Application appearance, shared by native controls and the embedded viewer."""
import json
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .storage import atomic_json


PALETTES = {
    'light': dict(face='#c5c9ce', panel='#d6d9dd', ink='#141a22', muted='#3c495c',
                  line='#626e7e', edge='#ffffff', sidebar='#b6bbc2', section='#4d5d73',
                  field='#f6f7f8', hover='#e9edf2', pressed='#aebdce', accent='#234978',
                  accent_hover='#2c5c98', on_accent='#ffffff', focus='#244e89',
                  disabled='#3c495c', row='#eef0f3', row_alt='#e4e8ee',
                  log='#101b2a', log_ink='#b8cbdc', metric='#6ae1bb',
                  placeholder='#526176'),
    'dark': dict(face='#161d27', panel='#232e3d', ink='#edf2f8', muted='#bdcadb',
                 line='#899bb1', edge='#899bb1', sidebar='#1c2532', section='#34465e',
                 field='#101722', hover='#35465d', pressed='#3e526e', accent='#a9caff',
                 accent_hover='#c7dcff', on_accent='#10213a', focus='#a9caff',
                 disabled='#bdcadb', row='#1b2533', row_alt='#263447',
                 log='#101b2a', log_ink='#b8cbdc', metric='#6ae1bb',
                 placeholder='#a3b4ca'),
}


STYLE = '''
QWidget { font-size: 12px; color: @ink@; }
QMainWindow, QDialog, QMessageBox, QWidget#content { background: @face@; }
QFrame#sidebar { background: @sidebar@; border-right: 1px solid @line@; }
QLabel#brand { background: #152c50; color: #ffffff; font-size: 16px; font-weight: 700; padding: 8px; }
QLabel#section { background: @section@; color: #ffffff; font-size: 11px; font-weight: 700; padding: 4px 6px; }
QLabel#title { font-size: 18px; font-weight: 700; }
QLabel#path { font-family: monospace; font-size: 11px; color: @muted@; }
QLabel#metric { background: @log@; color: @metric@; font-family: Menlo, Consolas, "DejaVu Sans Mono", "Liberation Mono", monospace; font-size: 20px; padding: 5px 8px; }
QLabel#metric-label { font-size: 10px; font-weight: 700; color: @muted@; }
QFrame#card, QGroupBox { background: @panel@; border: 1px solid @line@; border-top-color: @edge@; border-left-color: @edge@; border-radius: 0; }
QGroupBox { padding: 16px 10px 10px; margin-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 9px; padding: 0 4px; color: @ink@; font-weight: 700; }
QLineEdit, QSpinBox { background: @field@; color: @ink@; border: 1px solid @line@; border-radius: 0; padding: 4px; min-height: 18px; selection-background-color: @accent@; selection-color: @on_accent@; }
QLineEdit:focus, QSpinBox:focus { border: 2px solid @focus@; padding: 3px; }
QSpinBox { padding-right: 20px; }
QSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; width: 17px; height: 14px; background: @panel@; border-left: 1px solid @line@; }
QSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; width: 17px; height: 14px; background: @panel@; border-left: 1px solid @line@; }
QSpinBox::up-arrow { image: url("@assets@/spin-up@arrow_suffix@.svg"); width: 7px; height: 5px; }
QSpinBox::down-arrow { image: url("@assets@/spin-down@arrow_suffix@.svg"); width: 7px; height: 5px; }
QPushButton { background: @panel@; color: @ink@; border: 1px solid @line@; border-radius: 0; padding: 5px 12px; min-height: 18px; }
QPushButton:hover { background: @hover@; }
QPushButton:pressed, QPushButton#source-tab:checked { background: @pressed@; border: 1px solid @focus@; }
QPushButton:focus { border: 2px solid @focus@; padding: 4px 11px; }
QPushButton:disabled { color: @disabled@; background: @face@; }
QPushButton#primary { background: @accent@; color: @on_accent@; font-weight: 700; }
QPushButton#primary:focus { border-color: @on_accent@; }
QPushButton#primary:hover { background: @accent_hover@; }
QPushButton#primary:disabled { color: @disabled@; background: @face@; }
QListWidget#library { background: @row@; color: @ink@; border: 1px solid @line@; }
QListWidget#library::item { color: @ink@; padding: 7px 6px; border-bottom: 1px solid @line@; }
QListWidget#library::item:selected { color: @on_accent@; background: @accent@; }
QListWidget#library::item:hover:!selected { background: @hover@; }
QProgressBar { border: 1px solid @line@; background: @field@; border-radius: 0; height: 14px; max-height: 14px; }
QProgressBar::chunk { background: @accent@; width: 8px; margin: 1px; }
QPlainTextEdit { background: @log@; color: @log_ink@; border: 1px solid @line@; border-radius: 0; padding: 5px; font-family: monospace; font-size: 11px; selection-background-color: @accent@; selection-color: @on_accent@; }
QMenuBar, QMenu { background: @panel@; color: @ink@; }
QMenu { border: 1px solid @line@; }
QMenu::item:selected, QMenuBar::item:selected { background: @accent@; color: @on_accent@; }
QToolTip { background: @field@; color: @ink@; border: 1px solid @line@; }
QSplitter::handle { background: @line@; width: 1px; }
'''


def stylesheet(theme):
    values = dict(PALETTES[theme], assets=str(Path(__file__).with_name('assets')),
                  arrow_suffix='-light' if theme == 'dark' else '')
    result = STYLE
    for name, value in values.items():
        result = result.replace('@' + name + '@', value)
    return result


class ThemeManager(QObject):
    changed = Signal(str, str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        try:
            self.preferences = json.loads(self.path.read_text())
            if not isinstance(self.preferences, dict):
                self.preferences = {}
        except (OSError, ValueError):
            self.preferences = {}
        self.preference = self.preferences.get('theme', 'system')
        if self.preference not in ('light', 'dark', 'system'):
            self.preference = 'system'
        QApplication.styleHints().colorSchemeChanged.connect(self.system_changed)
        self.apply()

    @Slot(result='QVariantMap')
    def snapshot(self):
        return {'preference': self.preference, 'resolved': self.resolved}

    @Slot(str)
    def setPreference(self, preference):
        if preference not in ('light', 'dark', 'system'):
            return
        self.preference = preference
        self.preferences['theme'] = preference
        atomic_json(self.path, self.preferences)
        self.apply()

    def system_changed(self, scheme):
        if self.preference == 'system':
            self.apply()

    def system_scheme(self):
        return QApplication.styleHints().colorScheme()

    def apply(self):
        self.resolved = self.preference if self.preference != 'system' else (
            'dark' if self.system_scheme() == Qt.ColorScheme.Dark else 'light')
        colors = PALETTES[self.resolved]
        palette = QPalette()
        roles = dict(Window='face', WindowText='ink', Base='field', AlternateBase='row_alt',
                     Text='ink', Button='panel', ButtonText='ink', Highlight='accent',
                     HighlightedText='on_accent', ToolTipBase='field', ToolTipText='ink',
                     PlaceholderText='placeholder', Light='edge', Mid='line', Dark='line',
                     BrightText='ink', Link='focus', LinkVisited='focus')
        for role, token in roles.items():
            palette.setColor(getattr(QPalette.ColorRole, role), QColor(colors[token]))
        for role in ('WindowText', 'Text', 'ButtonText'):
            palette.setColor(QPalette.ColorGroup.Disabled, getattr(QPalette.ColorRole, role),
                             QColor(colors['disabled']))
        app = QApplication.instance()
        app.setPalette(palette)
        app.setStyleSheet(stylesheet(self.resolved))
        self.changed.emit(self.preference, self.resolved)
