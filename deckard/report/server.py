"""Loopback-only report viewer. No scan or source-volume access is needed."""
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
import traceback
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .model import Report, json_bytes


def serve(directory, port=8765, open_browser=False):
    directory = Path(directory).resolve()
    model = Report(directory / 'report.sqlite3')
    lock = threading.Lock()

    @lru_cache(maxsize=12)
    def view(x, y, xo, yo, metric):
        return json_bytes(model.view(x, y, xo, yo, metric))

    @lru_cache(maxsize=32)
    def keepers(token):
        return model.keepers(token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            started = time.monotonic()
            if self.headers.get('Host') not in ('127.0.0.1:' + str(self.server.server_port),
                                                'localhost:' + str(self.server.server_port)):
                self.send_error(403)
                return
            parsed = urlsplit(self.path)
            try:
                if parsed.path == '/api/view':
                    if len(parsed.query) > 512:
                        raise ValueError('query too long')
                    args = parse_qs(parsed.query)
                    get = lambda key, default: args.get(key, [default])[0]
                    with lock:
                        data = view(get('x', 'd:1'), get('y', 'd:1'), int(get('xo', 0)),
                                    int(get('yo', 0)), get('metric', 'allocated'))
                    content_type = 'application/json'
                elif parsed.path in ('/api/covered', '/api/keepers'):
                    args = parse_qs(parsed.query)
                    get = lambda key, default: args.get(key, [default])[0]
                    with lock:
                        result = (model.covered(int(get('offset', 0)), metric=get('metric', 'allocated'))
                                  if parsed.path == '/api/covered' else keepers(get('folder', 'd:1')))
                    data = json_bytes(result)
                    content_type = 'application/json'
                elif parsed.path == '/api/manifest':
                    data = json_bytes(model.manifest())
                    content_type = 'application/json'
                elif parsed.path in ('/', '/index.html', '/app.js', '/theme.js', '/style.css'):
                    name = 'index.html' if parsed.path == '/' else parsed.path[1:]
                    # Use the app's current UI for archived reports as well;
                    # only report data comes from the selected snapshot.
                    data = (Path(__file__).parent / 'web' / name).read_bytes()
                    content_type = {'index.html': 'text/html', 'app.js': 'text/javascript', 'theme.js': 'text/javascript',
                                    'style.css': 'text/css'}[name]
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
            except (ValueError, KeyError) as exc:
                data = json_bytes({'error': str(exc)})
                content_type = 'application/json'
                self.send_response(400)
            except Exception:
                traceback.print_exc()
                data = json_bytes({'error': 'Report query failed; check the report database.'})
                content_type = 'application/json'
                self.send_response(500)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Server-Timing', 'report;dur=%.3f' % ((time.monotonic() - started) * 1000))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    url = 'http://127.0.0.1:{}/'.format(server.server_port)
    print('Deckard report: ' + url, flush=True)
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
