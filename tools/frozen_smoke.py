"""Exercise a packaged GUI, scan helper and embedded report without Python."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')
    from deckard.bootstrap import payload
    from deckard.gui import MainWindow, create_application
    from deckard.storage import Library

    frame, digest = payload()
    assert frame and len(digest) == 64, 'Bundled worker source is missing'
    app = create_application([])
    with tempfile.TemporaryDirectory(prefix='deckard-binary-') as temporary:
        base = Path(temporary)
        os.environ['DECKARD_DATA_DIR'] = str(base / 'library')
        root = base / 'source'
        for name in ('A', 'B'):
            (root / name).mkdir(parents=True)
            (root / name / 'copy.bin').write_bytes(b'Deckard\x00\r\n' * 100)
        window = MainWindow(Library())
        window.show()
        ready = []
        try:
            if sys.platform != 'win32':
                # Exercise the generated SSH_ASKPASS wrapper after relocation.
                window.auth.requested.disconnect()
                window.auth.requested.connect(lambda request: request.answer('smoke-test-secret'))
                environment = dict(os.environ, **window.auth.environment())
                helper = subprocess.Popen([str(window.auth.helper), 'Password:'], env=environment,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                deadline = time.monotonic() + 10
                while helper.poll() is None and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(.02)
                if helper.poll() is None:
                    helper.kill()
                output, error = helper.communicate(timeout=5)
                assert helper.returncode == 0 and output == 'smoke-test-secret\n', error
            window.local_path.setText(str(root))
            window.start_new()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                app.processEvents()
                if window.current and not window.is_running():
                    config = json.loads((window.current / 'job.json').read_text())
                    assert config['status'] == 'complete', config
                    if window.web.url().host() == '127.0.0.1':
                        window.web.page().runJavaScript(
                            "document.title + '|' + document.getElementById('loading')?.classList.contains('hidden')",
                            ready.append,
                        )
                        if 'Deckard|true' in ready:
                            break
                time.sleep(.02)
            else:
                raise AssertionError('Packaged scan or embedded report timed out: ' + window.logs.toPlainText())
            manifest = json.loads((window.current / 'report/manifest.json').read_text())
            assert manifest['files'] == 2, manifest
            assert manifest['blocked_entries'] == 0, manifest
            assert manifest['logical_bytes'] == 2000, manifest
            # Refresh exercises atomic replacement of existing scan reports.
            window.start_job(window.current, refresh=True)
            deadline = time.monotonic() + 30
            while window.is_running() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.02)
            config = json.loads((window.current / 'job.json').read_text())
            assert config['status'] == 'complete', config
            if sys.platform == 'win32':
                # Exercise Windows stdin cancellation and resumption through
                # the same QProcess commands used by the desktop controls.
                job = window.library.create({'kind': 'local', 'root': str(root), 'workers': 1})
                window.start_job(job)
                assert window.process.waitForStarted(10000), window.process.errorString()
                window.pause_scan()
                deadline = time.monotonic() + 20
                while window.is_running() and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(.02)
                config = json.loads((job / 'job.json').read_text())
                assert config['status'] == 'paused', config
                window.start_job(job)
                deadline = time.monotonic() + 30
                while window.is_running() and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(.02)
                config = json.loads((job / 'job.json').read_text())
                assert config['status'] == 'complete', config
        finally:
            if window.is_running():
                window.process.kill()
                window.process.waitForFinished(5000)
            window.close()
            deadline = time.monotonic() + 5
            while window.isVisible() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.02)
    print('Packaged GUI, scan, refresh and embedded report passed.', flush=True)
