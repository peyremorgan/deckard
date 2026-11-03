"""Exercise mount processes and cleanup without FUSE or a real SSH server."""
import json
import os
from pathlib import Path
import sys
import threading

import pytest

from deckard import folders


@pytest.fixture
def mount_environment(tmp_path, monkeypatch):
    root = tmp_path / 'remote source, # café'; root.mkdir()
    (root / 'A # café').mkdir(); (root / 'B').mkdir()
    (root / 'A # café' / 'keep').write_bytes(b'remote data')
    calls, transports = [], []
    log = tmp_path / 'arguments'
    ssh = tmp_path / 'fake ssh'
    ssh.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys
assert 'DECKARD_AUTH_TOKEN' not in os.environ
assert 'SSH_ASKPASS' not in os.environ
with open(os.environ['TEST_MOUNT_LOG'], 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
''')
    ssh.chmod(0o700)
    sshfs = tmp_path / 'sshfs'
    sshfs.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, subprocess, sys, time
args = sys.argv[1:]
with open(os.environ['TEST_MOUNT_LOG'], 'a') as f:
    f.write(json.dumps(args) + '\\n')
wrapper = args[args.index('-o') + 1].split('=', 1)[1]
subprocess.check_call([wrapper, '-s', 'host', 'sftp'])
if os.environ.get('TEST_MOUNT_FAIL'):
    print('SFTP subsystem unavailable', file=sys.stderr)
    sys.exit(1)
if not os.environ.get('TEST_MOUNT_STALL'):
    path = pathlib.Path(args[1])
    path.rmdir()
    path.symlink_to(os.environ['TEST_REMOTE_ROOT'], target_is_directory=True)
    pathlib.Path(str(path) + '.mounted').touch()
while True:
    time.sleep(.05)
''')
    sshfs.chmod(0o700)
    unmount = tmp_path / 'unmount'
    unmount.write_text('#!' + sys.executable + '\n' + '''
import pathlib, sys
path = pathlib.Path(sys.argv[-1])
if (path / 'busy').exists():
    sys.exit(1)
path.unlink()
path.mkdir()
pathlib.Path(str(path) + '.mounted').unlink()
''')
    unmount.chmod(0o700)

    class Transport:
        def __init__(self, host, user, port, identity, cancelled, environment):
            calls.append((host, user, port, identity))
            self.host = host
            self.base = [str(ssh), '-i', identity, '-l', user, '-p', str(port)]
            self.control = '/tmp/test-control'
            self.environment = environment
            self.closed = False
            transports.append(self)

        def worker_environment(self):
            return {k: v for k, v in self.environment.items()
                    if not k.startswith('DECKARD_AUTH_') and not k.startswith('SSH_ASKPASS')}

        def close(self):
            self.closed = True

    monkeypatch.setattr(folders, 'SSHTransport', Transport)
    monkeypatch.setattr(folders, 'executable', lambda name: str(sshfs if name == 'sshfs' else unmount))
    monkeypatch.setattr(folders.os.path, 'ismount', lambda path: Path(str(path) + '.mounted').exists())
    env = dict(os.environ, TEST_MOUNT_LOG=str(log), TEST_REMOTE_ROOT=str(root),
               DECKARD_AUTH_TOKEN='test-secret', SSH_ASKPASS='private-helper')
    target = dict(host='host', user='user', port=2222, identity=str(tmp_path / 'key with spaces, café'))
    manager = folders.RemoteFolders()
    yield manager, target, root, env, calls, transports, log
    (root / 'busy').unlink(missing_ok=True)
    manager.close()


def test_mount_reuses_connection_preserves_arguments_and_cleans_only_local_state(mount_environment):
    manager, target, root, env, calls, transports, log = mount_environment
    cancel = threading.Event()
    selected = manager.folders(target, os.fsencode(root), [os.fsencode('A # café'), b'B'], env, cancel)
    mount = next(iter(manager.mounts.values()))
    runtime, process = mount.runtime, mount.process
    assert selected == [root / 'A # café', root / 'B']
    assert manager.folders(target, os.fsencode(root), [b'B', b'B'], env, cancel) == [root / 'B']
    assert calls == [('host', 'user', 2222, target['identity'])]
    sshfs_args, ssh_args = map(json.loads, log.read_text().splitlines())
    assert sshfs_args[0] == 'host:' + str(root)
    assert '-f' in sshfs_args and 'idmap=user' in sshfs_args and 'nosuid,nodev' in sshfs_args
    assert 'ro' not in sshfs_args and 'allow_other' not in sshfs_args
    assert ssh_args[:6] == ['-i', target['identity'], '-l', 'user', '-p', '2222']
    assert ssh_args[-3:] == ['-s', 'host', 'sftp']
    assert 'BatchMode=yes' in ssh_args and '/tmp/test-control' in ssh_args
    assert not runtime.stat().st_mode & 0o077
    manager.close()
    assert transports[0].closed and process.poll() is not None
    assert not runtime.exists() and not manager.mounts
    assert (root / 'A # café' / 'keep').read_bytes() == b'remote data'


def test_missing_sshfs_has_setup_message_and_never_connects(mount_environment, monkeypatch):
    manager, target, root, env, calls, *_ = mount_environment
    monkeypatch.setattr(folders, 'executable', lambda name: None)
    with pytest.raises(RuntimeError, match='requires SSHFS'):
        manager.folders(target, os.fsencode(root), [b'B'], env, threading.Event())
    assert not calls and not manager.mounts


@pytest.mark.parametrize('failure', ['TEST_MOUNT_FAIL', 'TEST_MOUNT_STALL'])
def test_failed_or_cancelled_mount_disconnects_and_keeps_remote_files(mount_environment, failure):
    manager, target, root, env, _, transports, _ = mount_environment
    env[failure] = '1'
    cancel = threading.Event()
    timer = threading.Timer(.2, cancel.set) if failure == 'TEST_MOUNT_STALL' else None
    if timer:
        timer.start()
    try:
        with pytest.raises(RuntimeError, match='cancelled|SFTP subsystem unavailable'):
            manager.folders(target, os.fsencode(root), [b'B'], env, cancel)
    finally:
        if timer:
            timer.join()
    assert not manager.mounts and transports[0].closed
    assert (root / 'A # café' / 'keep').exists()


def test_busy_unmount_keeps_mount_and_connection_for_retry(mount_environment):
    manager, target, root, env, _, transports, _ = mount_environment
    manager.folders(target, os.fsencode(root), [b'B'], env, threading.Event())
    mount = next(iter(manager.mounts.values()))
    runtime = mount.runtime
    (root / 'busy').touch()
    with pytest.raises(RuntimeError, match='Close files'):
        manager.close()
    assert runtime.exists() and not transports[0].closed and mount.process.poll() is None
    assert (root / 'A # café' / 'keep').exists()
    (root / 'busy').unlink()
    manager.close()
    assert not runtime.exists() and transports[0].closed


def test_selected_folders_rejects_escaping_symlinks_before_opening(tmp_path):
    root = tmp_path / 'root'; root.mkdir()
    (root / 'inside').mkdir()
    (root / 'outside').symlink_to(tmp_path)
    with pytest.raises(ValueError, match='no longer available'):
        folders.selected_folders(root, [b'inside', b'outside'])
