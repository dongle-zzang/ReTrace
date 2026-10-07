"""Frontend build hosting and same-origin Backend API access, without GPU imports."""

from http.client import HTTPConnection, HTTPSConnection, HTTPException
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit


def send_body(handler, status, content_type, body):
    handler.send_response(status)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    if handler.command != 'HEAD':
        handler.wfile.write(body)


class FrontendFiles:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()

    def serve(self, handler, path):
        """Return false only when no frontend build has been deployed yet."""
        root = self.directory
        index = root / 'index.html'
        if not index.is_file():
            return False
        decoded = unquote(path)
        parts = decoded.split('/')
        if '\x00' in decoded or '\\' in decoded or any(part.startswith('.') for part in parts if part):
            handler.send_error(404)
            return True
        candidate = (root / decoded.lstrip('/')).resolve()
        if not candidate.is_relative_to(root):
            handler.send_error(404)
            return True
        if candidate.is_dir():
            candidate = (candidate / 'index.html').resolve()
        if not candidate.is_file():
            # Missing assets must be a 404, while browser SPA navigation serves index.
            if Path(decoded).suffix or 'text/html' not in handler.headers.get('Accept', ''):
                handler.send_error(404)
                return True
            candidate = index.resolve()
        if not candidate.is_relative_to(root):
            handler.send_error(404)
            return True
        content_type = mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream'
        if candidate.suffix in ('.js', '.mjs'):
            content_type = 'text/javascript'
        if content_type.startswith('text/'):
            content_type += '; charset=utf-8'
        try:
            body = candidate.read_bytes()
        except OSError:
            handler.send_error(404)
            return True
        send_body(handler, 200, content_type, body)
        return True


class BackendAPIProxy:
    def __init__(self, base_url, timeout=3):
        try:
            address = urlsplit(base_url)
            if (address.scheme not in ('http', 'https') or not address.hostname or
                    address.username is not None or address.password is not None or
                    address.path not in ('', '/') or address.query or address.fragment):
                raise ValueError
            self.host, self.port = address.hostname, address.port
            self.connection_class = HTTPSConnection if address.scheme == 'https' else HTTPConnection
        except ValueError:
            raise ValueError('Invalid PREVIEW_BACKEND_URL; use an HTTP(S) origin without credentials') from None
        self.timeout = timeout

    def serve(self, handler):
        address = urlsplit(handler.path)
        path = address.path
        if not (path == '/api' or path.startswith('/api/')):
            handler.send_error(404)
            return
        if any(part in ('.', '..') for part in unquote(path).split('/')):
            handler.send_error(404)
            return
        target = path + ('?' + address.query if address.query else '')
        connection = self.connection_class(self.host, self.port, timeout=self.timeout)
        try:
            # Current Backend APIs are GET only. Fixed upstream, no redirects or env proxy.
            headers = {'Accept': 'application/json'}
            for name in ('Authorization', 'Cookie'):
                if name in handler.headers:
                    headers[name] = handler.headers[name]
            connection.request('GET', target, headers=headers)
            response = connection.getresponse()
            body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024 or 300 <= response.status < 400:
                handler.send_error(502)
                return
            content_type = response.getheader('Content-Type', 'application/json')
            status = response.status
        except (OSError, HTTPException, ValueError):
            # Never reflect private upstream addresses, credentials or exception text.
            handler.send_error(502)
            return
        finally:
            connection.close()
        send_body(handler, status, content_type, body)
