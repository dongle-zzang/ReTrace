"""Frontend build hosting and same-origin Backend API access, without GPU imports."""

from http.client import HTTPConnection, HTTPSConnection, HTTPException
import mimetypes
from pathlib import Path
import re
import socket
import time
from urllib.parse import unquote, urlsplit


def send_body(handler, status, content_type, body, headers=(), content_length=None):
    handler.send_response(status)
    if status != 204:
        handler.send_header('Content-Type', content_type)
        handler.send_header('Content-Length', str(len(body) if content_length is None else content_length))
    for name, value in headers:
        handler.send_header(name, value)
    handler.end_headers()
    if handler.command != 'HEAD' and status != 204:
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
    MAX_REQUEST_BYTES = 65536

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

    def parking_methods(self, path):
        if path in ('/api/parking/spaces', '/api/parking/zones'):
            return ('GET', 'HEAD', 'POST')
        zone = r'/api/parking/zones/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
        if re.fullmatch(zone, path):
            return ('GET', 'HEAD', 'PATCH', 'DELETE')
        if re.fullmatch(zone + '/calibrate', path):
            return ('POST', 'DELETE')
        collection = re.fullmatch(r'/api/cameras/[A-Za-z0-9_-]+/parking-spaces', path)
        item = re.fullmatch(r'/api/cameras/[A-Za-z0-9_-]+/parking-spaces/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', path)
        return ('GET', 'HEAD', 'POST') if collection else ('GET', 'HEAD', 'PATCH', 'DELETE') if item else ()

    def reject(self, handler, status):
        # Close without consuming untrusted/unbounded bytes or reflecting inputs.
        handler.close_connection = True
        send_body(handler, status, 'application/json', b'{"detail":"Preview API request rejected"}')

    def read_body(self, handler):
        lengths = handler.headers.get_all('Content-Length', [])
        if handler.headers.get_all('Transfer-Encoding'):
            self.reject(handler, 400)
            return None
        if handler.headers.get_all('Expect'):
            self.reject(handler, 417)
            return None
        if not lengths and handler.command != 'DELETE':
            self.reject(handler, 411)
            return None
        if lengths and (len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0])):
            self.reject(handler, 400)
            return None
        length = int(lengths[0]) if lengths else 0
        if length > self.MAX_REQUEST_BYTES:
            self.reject(handler, 413)
            return None
        types = handler.headers.get_all('Content-Type', [])
        if (handler.command != 'DELETE' or length) and (len(types) != 1 or types[0].split(';')[0].strip().lower() != 'application/json'):
            self.reject(handler, 415)
            return None
        body = bytearray()
        original_timeout = handler.connection.gettimeout()
        deadline = time.monotonic() + self.timeout
        try:
            while len(body) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout
                handler.connection.settimeout(remaining)
                chunk = handler.rfile.read(min(16384, length - len(body)))
                if not chunk:
                    self.reject(handler, 400)
                    return None
                body.extend(chunk)
        except socket.timeout:
            self.reject(handler, 408)
            return None
        except OSError:
            self.reject(handler, 400)
            return None
        finally:
            handler.connection.settimeout(original_timeout)
        return bytes(body)

    def serve(self, handler):
        try:
            address = urlsplit(handler.path)
        except ValueError:
            self.reject(handler, 400)
            return
        path = address.path
        if address.scheme or address.netloc or address.fragment or not (path == '/api' or path.startswith('/api/')):
            handler.send_error(404)
            return
        if any(part in ('.', '..') for part in unquote(path).split('/')):
            handler.send_error(404)
            return
        target = path + ('?' + address.query if address.query else '')
        method = handler.command
        body = None
        if method not in ('GET', 'HEAD'):
            if method not in self.parking_methods(path):
                self.reject(handler, 405)
                return
            body = self.read_body(handler)
            if body is None:
                return
        connection = self.connection_class(self.host, self.port, timeout=self.timeout)
        try:
            # Fixed upstream, no redirects, no caller-selected host or proxy env.
            headers = {'Accept': 'application/json'}
            for name in ('Authorization', 'Cookie', 'Content-Type'):
                if name in handler.headers:
                    headers[name] = handler.headers[name]
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024 or 300 <= response.status < 400:
                handler.send_error(502)
                return
            content_type = response.getheader('Content-Type', 'application/json')
            status = response.status
            length = response.getheader('Content-Length')
        except (OSError, HTTPException, ValueError):
            # Never reflect private upstream addresses, credentials or exception text.
            handler.send_error(502)
            return
        finally:
            connection.close()
        head_length = int(length) if method == 'HEAD' and length and re.fullmatch(r'[0-9]{1,10}', length) else None
        send_body(handler, status, content_type, body, (), head_length)
