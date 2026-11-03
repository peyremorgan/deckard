"""Client-only scan library. Passwords and passphrases never enter this model."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import uuid


def data_home():
    if os.environ.get('DECKARD_DATA_DIR'):
        return Path(os.environ['DECKARD_DATA_DIR']).expanduser()
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/Deckard'
    return Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'deckard'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.new')
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True)
        stream.flush(); os.fsync(stream.fileno())
    tmp.chmod(0o600)
    os.replace(tmp, path)


class Library:
    def __init__(self, root=None):
        self.root = Path(root) if root else data_home()
        self.scans = self.root / 'scans'
        self.scans.mkdir(parents=True, exist_ok=True, mode=0o700)

    def create(self, source):
        allowed = ('kind', 'root', 'host', 'user', 'port', 'identity', 'workers')
        if any(key not in allowed for key in source):
            raise ValueError('Unexpected scan settings; credentials must not be saved.')
        ident = uuid.uuid4().hex
        folder = self.scans / ident
        folder.mkdir(mode=0o700)
        value = {'id': ident, 'created': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 'source': dict(source), 'status': 'new', 'phase': 'Ready to scan',
                 'report_ready': False, 'scan_complete': False}
        atomic_json(folder / 'job.json', value)
        return folder

    def jobs(self):
        result = []
        for path in self.scans.glob('*/job.json'):
            try:
                value = json.loads(path.read_text())
                value['folder'] = str(path.parent)
                result.append(value)
            except (OSError, ValueError):
                continue
        return sorted(result, key=lambda item: item['created'], reverse=True)

    def remove(self, folder):
        """Remove only a library-owned scan, after its scanner releases the lock."""
        folder = Path(folder).absolute()
        scans = self.scans.resolve()
        if (folder.is_symlink() or folder.parent.resolve() != scans or
                folder.resolve().parent != scans):
            raise ValueError('Only scans in this library can be removed.')
        with (folder / 'scan.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('This scan is still running. Stop it in its window before removing it.') from None
            shutil.rmtree(folder)
