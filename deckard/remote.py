"""Existing bounded scan protocol backed by a worker streamed into memory."""
import base64
import threading

from . import engine as core
from .bootstrap import payload, remote_command, LOADER
from .protocol import ProtocolSource, Session


class AgentlessSource(ProtocolSource):
    def __init__(self, root, transport=None, *, local_python=None, timeout=180, timing_sample=16):
        self.transport = transport
        self.root = core.raw(root).rstrip(b'/') or b'/'
        self.root_base64 = core.b64(self.root)
        self.lock = threading.Lock()
        self.local = threading.local()
        self.sessions = []
        self.timeout = timeout
        self.bootstrap, self.core_hash = payload()
        args = ['--root-base64', self.root_base64, '--timing-sample', str(timing_sample)]
        self.command = ([str(local_python), '-E', '-S', '-B', '-u', '-c', LOADER] + args
                        if local_python else transport.worker_command(remote_command(args)))
        self.environment = None if local_python else transport.worker_environment()
        try:
            self.main = self.session()
            self.info = self.main.hello
            self.root = base64.b64decode(self.info['root_base64'])
            self.root_base64 = self.info['root_base64']
        except BaseException:
            self.close()
            raise

    def session(self):
        current = getattr(self.local, 'session', None)
        if current is None or current.closed:
            current = Session(self.command, self.core_hash,
                              self.root_base64 if hasattr(self, 'info') else None,
                              self.timeout, bootstrap=self.bootstrap, environment=self.environment)
            with self.lock:
                self.sessions.append(current)
            self.local.session = current
            if not current.hello.get('agentless'):
                current.close()
                raise ValueError('Expected an in-memory Deckard worker.')
            if hasattr(self, 'info') and (current.hello['device'], current.hello['inode']) != (self.info['device'], self.info['inode']):
                current.close()
                raise ValueError('The source folder changed identity between connections.')
        return current

    def close(self):
        try:
            super().close()
        finally:
            if self.transport is not None:
                self.transport.close()
