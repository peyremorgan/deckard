# Build on each target OS: python -m PyInstaller --noconfirm tools/deckard.spec
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).parent
analysis = Analysis(
    [str(root / 'tools/frozen_entry.py')],
    pathex=[str(root)],
    datas=collect_data_files('deckard') + [
        (str(root / 'deckard/engine.py'), 'deckard'),
        (str(root / 'deckard/worker.py'), 'deckard'),
        (str(root / 'LICENSE'), '.'),
    ],
    hiddenimports=[
        'deckard.gui', 'deckard.scan_job', 'deckard.report.__main__',
        'deckard.diagnostics', 'deckard.askpass',
    ],
    excludes=['pytest', 'tkinter'],
)
pyz = PYZ(analysis.pure)
gui = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name='Deckard', console=sys.platform not in ('darwin', 'win32'), upx=False,
)
# A console bootloader preserves stdin/stdout for scanning, HTTP serving and
# SSH_ASKPASS even when the GUI is launched without a terminal.
helper = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name='Deckard-helper', console=True, upx=False,
)
bundle = COLLECT(gui, helper, analysis.binaries, analysis.datas, name='Deckard', upx=False)
if sys.platform == 'darwin':
    app = BUNDLE(
        bundle, name='Deckard.app', bundle_identifier='io.github.peyremorgan.deckard',
        info_plist={'NSHighResolutionCapable': True},
    )
