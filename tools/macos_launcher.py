"""Create a macOS app launcher for this checkout's virtual environment."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    python = root / '.venv/bin/python'
    if not python.exists():
        raise SystemExit('Create the project virtual environment first.')
    app = root / 'Deckard.app/Contents'
    (app / 'MacOS').mkdir(parents=True, exist_ok=True)
    (app / 'Resources').mkdir(exist_ok=True)
    launcher = app / 'MacOS/Deckard'
    # Launch Services on current macOS requires a native executable entry point.
    subprocess.run(['xcrun', 'clang', '-Os', str(root / 'tools/macos_launcher.c'),
                    '-o', str(launcher)], check=True)
    launcher.chmod(0o755)
    info = dict(CFBundleName='Deckard', CFBundleDisplayName='Deckard', CFBundleExecutable='Deckard',
                CFBundleIdentifier='app.deckard.desktop', CFBundleVersion='0.1.0',
                CFBundleShortVersionString='0.1.0', CFBundlePackageType='APPL',
                CFBundleIconFile='deckard.icns', NSHighResolutionCapable=True,
                NSRequiresAquaSystemAppearance=True)
    (app / 'Info.plist').write_bytes(plistlib.dumps(info))
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtCore import Qt
    from PySide6.QtSvg import QSvgRenderer
    gui = QGuiApplication.instance() or QGuiApplication([])
    svg = QSvgRenderer(str(root / 'deckard/assets/deckard.svg'))
    with tempfile.TemporaryDirectory(prefix='deckard-icon-') as temporary:
        icons = Path(temporary) / 'deckard.iconset'; icons.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                image = QImage(size * scale, size * scale, QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(Qt.GlobalColor.transparent)
                painter = QPainter(image); svg.render(painter); painter.end()
                image.save(str(icons / ('icon_%dx%d%s.png' % (size, size, '@2x' if scale == 2 else ''))))
        subprocess.run(['iconutil', '-c', 'icns', str(icons), '-o', str(app / 'Resources/deckard.icns')], check=True)
    subprocess.run(['codesign', '--force', '--sign', '-', str(app.parent)], check=True)
    print(root / 'Deckard.app')


if __name__ == '__main__':
    main()
