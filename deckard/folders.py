"""Optional, session-scoped SSHFS mounts for opening remote scan results."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .ssh import SSHTransport


def executable(name):
    found = shutil.which(name)
    if found:
        return found
    # Finder-launched applications often have a minimal PATH.
    for directory in ('/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/sbin'):
        path = Path(directory) / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def selected_folders(root, paths):
    """Validate all locations before opening any, including changed symlinks."""
    root = Path(root).resolve(strict=True)
    folders = []
    for path in paths:
        folder = (root / os.fsdecode(path)).resolve(strict=True)
        if not folder.is_relative_to(root) or not folder.is_dir():
            raise ValueError('The selected folder is no longer available in the scan source.')
        if folder not in folders:
            folders.append(folder)
    return folders


class SSHFSMount:
    def __init__(self):
        self.runtime = None
        self.path = None
        self.transport = None
        self.process = None
        self.reader = None
        self.errors = bytearray()

    def start(self, target, root, environment, cancelled):
        if sys.platform == 'win32':
            raise RuntimeError('Opening SSH folders currently requires macOS or Linux.')
        sshfs = executable('sshfs')
        if not sshfs:
            setup = ('Install macFUSE and SSHFS from https://macfuse.io.' if sys.platform == 'darwin'
                     else 'Install your distribution’s sshfs package.')
            raise RuntimeError('Opening SSH folders requires SSHFS on this computer. ' + setup)
        self.transport = SSHTransport(target['host'], target.get('user', ''), target.get('port', 0),
                                      target.get('identity', ''), cancelled, environment)
        self.runtime = Path(tempfile.mkdtemp(prefix='dk-mount-', dir='/tmp'))
        self.path = self.runtime / 'files'
        self.path.mkdir(mode=0o700)
        # SSHFS has its own command tokenizer, not shell quoting. A private
        # wrapper preserves spaces/commas in identity paths and SSH arguments.
        wrapper = self.runtime / 'ssh'
        command = self.transport.base + ['-S', self.transport.control, '-o', 'ControlMaster=no',
                                         '-o', 'BatchMode=yes']
        wrapper.write_bytes(os.fsencode('#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n'))
        wrapper.chmod(0o700)
        host = self.transport.host
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'
        # Mount the report's canonical absolute root, never the local expansion
        # of the user-entered '~'. No shell processes this remote pathname.
        self.process = subprocess.Popen(
            [sshfs, host + ':' + os.fsdecode(root), str(self.path), '-f',
             '-o', 'ssh_command=' + str(wrapper), '-o', 'idmap=user', '-o', 'nosuid,nodev'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=self.transport.worker_environment())
        self.reader = threading.Thread(target=self._read_errors, daemon=True)
        self.reader.start()
        deadline = time.monotonic() + 30
        while not cancelled.is_set():
            if self.process.poll() is not None:
                self.reader.join(1)
                detail = bytes(self.errors).decode('utf-8', 'replace').strip()
                raise RuntimeError('Could not mount the SSH folder. Check SSHFS/FUSE and SFTP support on the server.' +
                                   ('\n' + detail if detail else ''))
            if os.path.ismount(self.path):
                return self.path
            if time.monotonic() >= deadline:
                raise RuntimeError('Opening the SSH folder timed out.')
            cancelled.wait(.05)
        raise RuntimeError('Opening the SSH folder was cancelled.')

    def _read_errors(self):
        while chunk := self.process.stderr.read(1024):
            self.errors.extend(chunk)
            if len(self.errors) > 8192:
                del self.errors[:-8192]

    def close(self):
        if self.path is not None and os.path.ismount(self.path):
            helper = None if sys.platform == 'darwin' else (executable('fusermount3') or executable('fusermount'))
            command = [helper, '-u', str(self.path)] if helper else [executable('umount') or 'umount', str(self.path)]
            try:
                result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, timeout=10)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError('Unmount timed out: ' + str(self.path)) from exc
            if result.returncode or os.path.ismount(self.path):
                raise RuntimeError('Could not unmount ' + str(self.path) +
                                   '. Close files and file manager windows using it, then try again.')
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait(timeout=5)
            if self.reader is not None:
                self.reader.join(1)
            self.process.stderr.close()
            self.process = None
        # Check again after stopping a mount that was still starting up.
        if self.path is not None and os.path.ismount(self.path):
            raise RuntimeError('The SSH folder is still mounted: ' + str(self.path))
        if self.transport is not None:
            self.transport.close(); self.transport = None
        if self.runtime is not None:
            # Never recursively clean a directory that may contain remote data.
            (self.runtime / 'ssh').unlink(missing_ok=True)
            if self.path.exists():
                self.path.rmdir()
            self.runtime.rmdir(); self.runtime = None


class RemoteFolders:
    """Accessed by one background worker at a time; mounts outlive reports."""
    def __init__(self):
        self.mounts = {}

    def folders(self, target, root, paths, environment, cancelled):
        if not root.startswith(b'/') or b'\0' in root:
            raise ValueError('The report has no absolute SSH source folder.')
        key = (target['host'], target.get('user', ''), target.get('port', 0), target.get('identity', ''), root)
        mount = self.mounts.get(key)
        if mount is not None and (mount.process is None or mount.process.poll() is not None or
                                  not os.path.ismount(mount.path)):
            mount.close()
            del self.mounts[key]
            mount = None
        if mount is None:
            mount = SSHFSMount()
            self.mounts[key] = mount
            try:
                mount.start(target, root, environment, cancelled)
            except BaseException:
                mount.close()
                del self.mounts[key]
                raise
        return selected_folders(mount.path, paths)

    def close(self):
        errors = []
        for key, mount in list(self.mounts.items()):
            try:
                mount.close()
                del self.mounts[key]
            except (OSError, RuntimeError) as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError('\n'.join(errors))
