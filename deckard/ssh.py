"""System OpenSSH: normal keys/config first, GUI askpass only as fallback."""
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time


def validate_target(host, user='', port=0):
    if not host or host.startswith('-') or any(c.isspace() or c in '\x00/\\' for c in host):
        raise ValueError('Enter a hostname or SSH config alias, without spaces.')
    if '@' in host:
        if user:
            raise ValueError('Enter the username separately or use user@host, not both.')
        user, host = host.rsplit('@', 1)
    if not host or host.startswith('-') or not 0 <= int(port) <= 65535:
        raise ValueError('Invalid SSH host or port.')
    if user and (user.startswith('-') or any(c.isspace() or c in '\x00/\\@:' for c in user)):
        raise ValueError('Invalid SSH username.')
    return host, user, int(port)


class SSHTransport:
    def __init__(self, host, user='', port=0, identity='', cancelled=None,
                 environment=None, existing_control=None):
        host, user, port = validate_target(host, user, port)
        self.host = host
        self.cancelled = cancelled or threading.Event()
        self.env = dict(os.environ if environment is None else environment)
        self.base = ['ssh', '-T', '-a', '-x', '-o', 'ConnectTimeout=10',
                     '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                     '-o', 'ClearAllForwardings=yes', '-o', 'PermitLocalCommand=no',
                     '-o', 'RemoteCommand=none', '-o', 'LogLevel=ERROR']
        if user:
            self.base += ['-l', user]
        if port:
            self.base += ['-p', str(port)]
        if identity:
            self.base += ['-i', str(Path(identity).expanduser()), '-o', 'IdentitiesOnly=yes']
        self.process = None
        self.runtime = None
        self.errors = bytearray()
        self.reader = None
        if existing_control:
            self.control = str(existing_control)
            self.borrowed = True
            return
        self.borrowed = False
        # macOS UNIX-domain socket paths are short. This is LOCAL runtime state.
        self.runtime = tempfile.TemporaryDirectory(prefix='dk-ssh-', dir='/tmp')
        self.control = str(Path(self.runtime.name) / 's')
        try:
            ok = self._connect(batch=True)
            if not ok:
                reason = bytes(self.errors).decode('utf-8', 'replace')
                fallback = any(text in reason.lower() for text in
                               ('permission denied', 'host key verification failed', 'authentication failed'))
                if not fallback or not self.env.get('DECKARD_AUTH_SOCKET'):
                    raise RuntimeError(reason.strip() or 'SSH authentication failed. Open Deckard to sign in interactively.')
                self._stop_master()
                self.errors.clear()
                if not self._connect(batch=False):
                    raise RuntimeError(bytes(self.errors).decode('utf-8', 'replace').strip() or 'SSH sign-in was cancelled.')
        except BaseException:
            self.close()
            raise

    def _stderr(self):
        while True:
            chunk = self.process.stderr.read(1024)
            if not chunk:
                return
            self.errors.extend(chunk)
            if len(self.errors) > 8192:
                del self.errors[:-8192]

    def _connect(self, batch):
        command = self.base + ['-M', '-N', '-S', self.control,
                               '-o', 'ControlMaster=yes', '-o', 'ControlPersist=no',
                               '-o', 'ForkAfterAuthentication=no',
                               '-o', 'BatchMode=' + ('yes' if batch else 'no'), self.host]
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, env=self.env)
        self.reader = threading.Thread(target=self._stderr, daemon=True)
        self.reader.start()
        deadline = time.monotonic() + (30 if batch else 600)
        while time.monotonic() < deadline:
            if self.cancelled.is_set():
                raise KeyboardInterrupt()
            if self.process.poll() is not None:
                self.reader.join(1)
                return False
            if Path(self.control).exists():
                return True
            time.sleep(.05)
        raise RuntimeError('SSH connection timed out.')

    def worker_command(self, command):
        return self.base + ['-S', self.control, '-o', 'ControlMaster=no',
                            '-o', 'BatchMode=yes', self.host, command]

    def worker_environment(self):
        # Workers use the authenticated master. Never forward askpass IPC secrets.
        return {key: value for key, value in self.env.items()
                if not key.startswith('DECKARD_AUTH_') and key not in
                ('SSH_ASKPASS', 'SSH_ASKPASS_REQUIRE')}

    def _stop_master(self):
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

    def close(self):
        if not self.borrowed:
            self._stop_master()
        if self.runtime is not None:
            self.runtime.cleanup()
            self.runtime = None
