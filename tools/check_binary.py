"""Extract and smoke-test the exact archive that CI will publish."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    archives = [p for p in Path('release').resolve().iterdir()
                if p.name.endswith(('.zip', '.tar.gz'))]
    assert len(archives) == 1, archives
    with tempfile.TemporaryDirectory(prefix='deckard-install-') as temporary:
        folder = Path(temporary)
        if sys.platform == 'darwin':
            subprocess.run(['ditto', '-x', '-k', str(archives[0]), temporary], check=True)
            helper = folder / 'Deckard.app/Contents/MacOS/Deckard-helper'
        else:
            shutil.unpack_archive(str(archives[0]), temporary)
            helper = folder / 'Deckard' / ('Deckard-helper.exe' if sys.platform == 'win32' else 'Deckard-helper')
        environment = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'QT_'))}
        subprocess.run([str(helper), '--smoke-test'], cwd=temporary, env=environment, check=True, timeout=120)


if __name__ == '__main__':
    main()
