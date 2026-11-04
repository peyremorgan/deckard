"""Build and archive a standalone bundle for the current OS and architecture."""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    os.environ.setdefault('PYINSTALLER_CONFIG_DIR', str(root / 'build/pyinstaller-cache'))
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', 'tools/deckard.spec'], check=True)
    system = {'darwin': 'macos', 'win32': 'windows', 'linux': 'linux'}[sys.platform]
    machine = platform.machine().lower()
    arch = {'amd64': 'x86_64', 'aarch64': 'arm64'}.get(machine, machine)
    name = f'Deckard-{system}-{arch}'
    release = root / 'release'
    release.mkdir(exist_ok=True)
    if sys.platform == 'darwin':
        archive = release / (name + '.zip')
        subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                        'dist/Deckard.app', str(archive)], check=True)
    elif sys.platform == 'win32':
        archive = Path(shutil.make_archive(str(release / name), 'zip', 'dist', 'Deckard'))
    else:
        archive = Path(shutil.make_archive(str(release / name), 'gztar', 'dist', 'Deckard'))
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    archive.with_name(archive.name + '.sha256').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    print(archive)


if __name__ == '__main__':
    main()
