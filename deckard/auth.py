"""Bridge OpenSSH askpass requests onto the Qt main thread."""
import hmac
import json
import os
from pathlib import Path
import secrets
import shlex
import socketserver
import sys
import tempfile
import threading

from PySide6.QtCore import QObject, Signal

from .runtime import module_command


class AuthRequest:
    def __init__(self, prompt, kind, purpose='scan'):
        self.prompt, self.kind = prompt, kind
        self.purpose = purpose
        self.done = threading.Event()
        self.response = None

    def answer(self, value):
        self.response = value
        self.done.set()


class AuthBridge(QObject):
    requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.token = secrets.token_hex(32)
        self.pending = set()
        self.lock = threading.Lock()
        self.closed = False
        self.server = None
        # SSH connection sharing and its private askpass bridge require POSIX.
        if sys.platform == 'win32':
            return
        self.runtime = tempfile.TemporaryDirectory(prefix='dk-auth-', dir='/tmp')
        bridge = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request = None
                try:
                    self.connection.settimeout(610)
                    value = json.loads(self.rfile.readline(65537))
                    if not hmac.compare_digest(str(value.get('token', '')), bridge.token):
                        return
                    request = AuthRequest(str(value.get('prompt', 'SSH authentication')), str(value.get('kind', '')),
                                          str(value.get('purpose', 'scan')))
                    with bridge.lock:
                        if bridge.closed:
                            return
                        bridge.pending.add(request)
                    bridge.requested.emit(request)
                    request.done.wait(600)
                    self.wfile.write((json.dumps({'response': request.response}) + '\n').encode())
                except (OSError, ValueError, TypeError):
                    return
                finally:
                    if request is not None:
                        request.response = None
                        with bridge.lock:
                            bridge.pending.discard(request)

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.socket_path = str(Path(self.runtime.name) / 's')
        self.server = Server(self.socket_path, Handler)
        os.chmod(self.socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.helper = Path(self.runtime.name) / 'askpass'
        self.helper.write_text('#!/bin/sh\nexec ' + shlex.join(module_command('deckard.askpass')) + ' "$@"\n')
        self.helper.chmod(0o700)

    def environment(self):
        if self.server is None:
            return {}
        return {'SSH_ASKPASS': str(self.helper), 'SSH_ASKPASS_REQUIRE': 'force',
                'DISPLAY': os.environ.get('DISPLAY') or 'deckard:0',
                'DECKARD_AUTH_SOCKET': self.socket_path, 'DECKARD_AUTH_TOKEN': self.token}

    def cancel_pending(self, purpose=None):
        with self.lock:
            for request in self.pending:
                if purpose is None or request.purpose == purpose:
                    request.answer(None)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancel_pending()
        if self.server is None:
            return
        self.server.shutdown(); self.server.server_close()
        self.thread.join(timeout=2)
        self.runtime.cleanup()
