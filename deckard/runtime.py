"""Commands shared by source checkouts and standalone desktop bundles."""
from pathlib import Path
import sys


def module_command(module, *args):
    if getattr(sys, 'frozen', False):
        suffix = '.exe' if sys.platform == 'win32' else ''
        helper = Path(sys.executable).with_name('Deckard-helper' + suffix)
        return [str(helper), '--module', module, *map(str, args)]
    return [sys.executable, '-u', '-m', module, *map(str, args)]
