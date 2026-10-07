"""CPU-only regression checks for Preview page, MJPEG and JSON serving."""
import ast
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from preview_mjpeg import FrameStore, serve_mjpeg
from preview_web import FrontendFiles, BackendAPIProxy

ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / "preview.py").read_text())
handler_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PreviewHandler")
namespace = {"BaseHTTPRequestHandler": BaseHTTPRequestHandler, "urlsplit": urlsplit,
             "WEB_ROOT": ROOT / "web",
             "json": json, "serve_mjpeg": serve_mjpeg, "sys": __import__("sys")}
exec(compile(ast.Module(body=[handler_node], type_ignores=[]), "preview.py", "exec"), namespace)
PreviewHandler = namespace["PreviewHandler"]


class PreviewHttpTests(unittest.TestCase):
    def test_uploaded_frontend_assets_navigation_and_file_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = root / 'frontend'
            build.mkdir()
            (build / 'index.html').write_text('<html>uploaded frontend</html>')
            (build / '_nuxt').mkdir()
            (build / '_nuxt' / 'app.js').write_text('console.log("frontend");')
            (build / 'cameras').mkdir()
            (build / 'cameras' / 'index.html').write_text('<html>generated route</html>')
            (root / 'private.txt').write_text('private credentials')
            (build / 'escape.txt').symlink_to(root / 'private.txt')
            (build / '.env').write_text('private credentials')
            server = ThreadingHTTPServer(('127.0.0.1', 0), PreviewHandler)
            server.mjpeg_streams = {}
            server.streams = [{'id': 0}]
            server.frontend_files = FrontendFiles(build)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                for path, expected in (('/', b'uploaded frontend'), ('/cameras', b'generated route'),
                                       ('/dashboard/floor1', b'uploaded frontend')):
                    with urlopen(Request(base + path, headers={'Accept': 'text/html'}), timeout=2) as response:
                        self.assertIn(expected, response.read())
                with urlopen(base + '/_nuxt/app.js?v=1', timeout=2) as response:
                    self.assertIn('javascript', response.headers['Content-Type'])
                    self.assertIn(b'frontend', response.read())
                for path, filename in (('/diagnostics', 'index.html'), ('/preview.js', 'preview.js')):
                    with urlopen(base + path, timeout=2) as response:
                        self.assertEqual(response.read(), (ROOT / 'web' / filename).read_bytes())
                with urlopen(Request(base + '/', method='HEAD'), timeout=2) as response:
                    self.assertEqual(response.read(), b'')
                    self.assertGreater(int(response.headers['Content-Length']), 0)
                with urlopen(base + '/streams.json', timeout=2) as response:
                    self.assertEqual(json.load(response), server.streams)
                for path in ('/.env', '/%2e%2e/private.txt', '/escape.txt', '/missing.js',
                             '/_nuxt/missing.js', '/mjpeg/source99', '/%00', '/%5c..%5cprivate.txt'):
                    with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                        urlopen(Request(base + path, headers={'Accept': 'text/html'}), timeout=2)
                    self.assertEqual(caught.exception.code, 404)
                    caught.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_same_origin_backend_proxy_query_status_and_failure(self):
        requests = []
        class BackendHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                status = 503 if self.path == '/api/unavailable' else 404 if self.path == '/api/missing' else 200
                if self.path == '/api/redirect':
                    status = 302
                body = json.dumps({'path': self.path}).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                if status == 302:
                    self.send_header('Location', 'http://private.example/')
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_args):
                pass
        backend = ThreadingHTTPServer(('127.0.0.1', 0), BackendHandler)
        server = ThreadingHTTPServer(('127.0.0.1', 0), PreviewHandler)
        server.mjpeg_streams = {}
        server.backend_api_proxy = BackendAPIProxy(f'http://127.0.0.1:{backend.server_port}')
        threads = [threading.Thread(target=item.serve_forever, daemon=True) for item in (backend, server)]
        for thread in threads:
            thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            path = '/api/cameras/first/tracks?limit=5&value=a%2Fb'
            with urlopen(base + path, timeout=2) as response:
                self.assertEqual(json.load(response)['path'], path)
            with urlopen(Request(base + '/api/health', method='HEAD'), timeout=2) as response:
                self.assertEqual(response.read(), b'')
            for path, status in (('/api/missing', 404), ('/api/unavailable', 503),
                                 ('/api/redirect', 502), ('/api/%2e%2e/private', 404)):
                with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                    urlopen(base + path, timeout=2)
                self.assertEqual(caught.exception.code, status)
                caught.exception.close()
            self.assertNotIn('/api/%2e%2e/private', requests)
            backend.shutdown()
            backend.server_close()
            with self.assertRaises(HTTPError) as caught:
                urlopen(base + '/api/health', timeout=2)
            self.assertEqual(caught.exception.code, 502)
            self.assertNotIn(b'127.0.0.1', caught.exception.read())
            caught.exception.close()
            for path in ('/', '/index.html'):
                with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                    urlopen(base + path, timeout=2)
                self.assertEqual(caught.exception.code, 404)
                caught.exception.close()
            with urlopen(base + '/diagnostics', timeout=2) as response:
                self.assertEqual(response.read(), (ROOT / 'web' / 'index.html').read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            backend.shutdown()
            backend.server_close()
            for thread in threads:
                thread.join(timeout=2)

    def test_only_client_disconnects_are_suppressed(self):
        handler = PreviewHandler.__new__(PreviewHandler)
        for method in ("handle", "finish"):
            for error in (BrokenPipeError(), ConnectionResetError()):
                with patch.object(BaseHTTPRequestHandler, method, side_effect=error):
                    getattr(handler, method)()
                self.assertTrue(handler.close_connection)
            with patch.object(BaseHTTPRequestHandler, method, side_effect=ValueError("real error")):
                with self.assertRaises(ValueError):
                    getattr(handler, method)()

    def test_http_mjpeg_json_and_allowlisted_ui(self):
        store = FrameStore()
        jpeg = b"\xff\xd8test\xff\xd9"
        store.put(jpeg)
        server = ThreadingHTTPServer(("127.0.0.1", 0), PreviewHandler)
        server.mjpeg_streams = {"/mjpeg/source0": store}
        server.streams = [{"id": 0, "format": "mjpeg", "url": "/mjpeg/source0"}]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            for path, filename, content_type in (("/diagnostics", "index.html", "text/html"),
                                                 ("/preview.js?v=1", "preview.js", "text/javascript")):
                with urlopen(base + path, timeout=2) as response:
                    self.assertEqual(response.read(), (ROOT / "web" / filename).read_bytes())
                    self.assertIn(content_type, response.headers["Content-Type"])
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
            with urlopen(base + "/streams.json", timeout=2) as response:
                self.assertEqual(json.load(response), server.streams)
            server.stream_snapshot = lambda: [{**server.streams[0], "runtime": {"state": "offline", "reconnect_count": 3}}]
            with urlopen(base + "/streams.json", timeout=2) as response:
                self.assertEqual(json.load(response)[0]["runtime"], {"state": "offline", "reconnect_count": 3})
            server.metadata_snapshot = lambda: {"runtime_session": "a" * 32, "frames": []}
            with urlopen(base + "/metadata.json", timeout=2) as response:
                self.assertEqual(json.load(response), server.metadata_snapshot())
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            with urlopen(base + "/mjpeg/source0?v=1", timeout=2) as response:
                self.assertIn("boundary=frame", response.headers["Content-Type"])
                self.assertEqual(response.readline(), b"--frame\r\n")
                headers = {}
                while True:
                    line = response.readline()
                    if line == b"\r\n":
                        break
                    key, value = line.decode().split(":", 1)
                    headers[key] = value.strip()
                self.assertEqual(response.read(int(headers["Content-Length"])), jpeg)
            errors = io.StringIO()
            with patch("sys.stderr", errors):
                for path in ("/style.css", "/private.txt", "/.env", "/preview.py",
                             "/../README.md", "/web/index.html", "/mjpeg/source99",
                             "/", "/index.html", "/webrtc.js"):
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(base + path, timeout=2)
                    self.assertEqual(caught.exception.code, 404)
                    caught.exception.close()
            self.assertIn("HTTP error status=404", errors.getvalue())
        finally:
            store.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
