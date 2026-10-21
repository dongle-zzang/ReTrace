"""Bounded parking proxy and real HTTP requests, using an isolated fake Backend."""
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from preview_web import BackendAPIProxy
from test_preview_http import PreviewHandler


COLLECTION = '/api/cameras/parking-a/parking-spaces'
ITEM = COLLECTION + '/11111111-1111-4111-8111-111111111111'
ORIGIN = 'http://localhost:3000'


class ParkingProxyTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.spaces = []
        owner = self

        class BackendHandler(BaseHTTPRequestHandler):
            def serve(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                owner.requests.append((self.command, self.path, dict(self.headers), body))
                if (self.path.endswith('/download') or '/files/' in self.path) and hasattr(owner, 'archive'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/zip' if self.path.endswith('/download') else 'application/octet-stream')
                    self.send_header('Content-Length', str(len(owner.archive)))
                    self.send_header('Content-Disposition', 'attachment; filename="dataset_fixture.zip"')
                    self.end_headers()
                    if self.command != 'HEAD': self.wfile.write(owner.archive)
                    return
                status = 200
                result = owner.spaces
                if self.command in ('POST', 'PATCH', 'PUT'):
                    try:
                        data = json.loads(body)
                        if not isinstance(data, dict):
                            raise ValueError
                    except ValueError:
                        status, result = 422, {'detail': 'Invalid request'}
                    else:
                        if self.command == 'POST':
                            status = 201
                            owner.spaces.append({'space_id': ITEM.rsplit('/', 1)[1], **data})
                        else:
                            owner.spaces[0].update(data)
                        result = owner.spaces[0]
                elif self.command == 'DELETE':
                    status = 204
                    owner.spaces.clear()
                payload = b'' if status == 204 else json.dumps(result).encode()
                self.send_response(status)
                if status != 204:
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(payload)

            do_GET = serve
            do_HEAD = serve
            do_POST = serve
            do_PUT = serve
            do_PATCH = serve
            do_DELETE = serve

            def log_message(self, *_args):
                pass

        self.backend = ThreadingHTTPServer(('127.0.0.1', 0), BackendHandler)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), PreviewHandler)
        self.server.mjpeg_streams = {}
        self.proxy = BackendAPIProxy(f'http://127.0.0.1:{self.backend.server_port}', timeout=.3)
        self.server.backend_api_proxy = self.proxy
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.backend, self.server)]
        for thread in self.threads:
            thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def test_color_routes_use_existing_write_proxy(self):
        for path in ('/api/parking/spaces', '/api/parking/zones',
                     '/api/parking/zones/11111111-1111-4111-8111-111111111111/calibrate'):
            response = self.request('POST', path, b'{}')
            self.assertEqual(response[0], 201)
            self.assertEqual(self.requests[-1][1], path)
        path = '/api/parking/zones/11111111-1111-4111-8111-111111111111'
        self.assertEqual(self.request('PATCH', path, b'{"enabled":false}')[0], 200)
        self.assertEqual(self.request('DELETE', path)[0], 204)

    def test_label_management_proxy_is_limited_to_uuid_items(self):
        self.spaces.append({'label': 'Original'})
        path = '/api/parking/spaces/11111111-1111-4111-8111-111111111111'
        self.assertEqual(self.request('PATCH', path, b'{"label":"Renamed"}')[0], 200)
        self.assertEqual(self.requests[-1][1], path)
        self.assertEqual(self.request('DELETE', path)[0], 204)
        self.assertNotIn('PATCH', self.proxy.parking_methods('/api/parking/spaces/not-a-uuid'))

    def tearDown(self):
        for server in (self.backend, self.server):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def request(self, method, path=COLLECTION, data=None, headers=None):
        values = {'Origin': ORIGIN}
        if data is not None:
            values['Content-Type'] = 'application/json; charset=utf-8'
        values.update(headers or {})
        try:
            response = urlopen(Request(self.base + path, method=method, data=data, headers=values), timeout=2)
        except HTTPError as error:
            response = error
        with response:
            return response.status, dict(response.headers), response.read()

    def raw(self, headers, body=b'', method='POST', path=COLLECTION, half_close=False):
        sock = socket.create_connection(self.server.server_address, timeout=2)
        sock.sendall((f'{method} {path} HTTP/1.1\r\nHost: localhost\r\n' + headers + '\r\n').encode() + body)
        if half_close:
            sock.shutdown(socket.SHUT_WR)
        sock.settimeout(2)
        response = bytearray()
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
        finally:
            sock.close()
        return int(response.split(b' ', 2)[1])

    def test_crud_preserves_method_query_json_headers_status_and_204(self):
        data = {'name': 'A-01', 'polygon': [{'x': .1, 'y': .2}, {'x': .8, 'y': .2}, {'x': .8, 'y': .9}]}
        body = json.dumps(data).encode()
        status, headers, payload = self.request('POST', COLLECTION + '?value=a%2Fb', body,
                                                {'Authorization': 'Bearer fixture', 'Cookie': 'fixture=yes'})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(payload)['name'], 'A-01')
        method, path, forwarded, received = self.requests[-1]
        self.assertEqual((method, path, received), ('POST', COLLECTION + '?value=a%2Fb', body))
        self.assertEqual(forwarded['Content-Type'], 'application/json; charset=utf-8')
        self.assertEqual(forwarded['Authorization'], 'Bearer fixture')
        self.assertEqual(forwarded['Cookie'], 'fixture=yes')
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual(self.request('GET')[0], 200)
        self.assertEqual(self.request('HEAD')[2], b'')
        self.assertEqual(self.requests[-1][0], 'HEAD')
        status, _, payload = self.request('PATCH', ITEM, b'{"name":"A-02"}')
        self.assertEqual((status, json.loads(payload)['name']), (200, 'A-02'))
        status, headers, payload = self.request('DELETE', ITEM)
        self.assertEqual((status, payload), (204, b''))
        self.assertNotIn('Content-Length', headers)
        self.assertEqual(json.loads(self.request('GET')[2]), [])

    def test_writes_need_no_peer_or_origin_allowlist_and_add_no_cors(self):
        for origin in (ORIGIN, 'http://other.example', 'null', None):
            with self.subTest(origin=origin):
                headers = {'Content-Type': 'application/json'}
                if origin:
                    headers['Origin'] = origin
                with urlopen(Request(self.base + COLLECTION, data=b'{}', headers=headers), timeout=2) as response:
                    self.assertEqual(response.status, 201)
                    self.assertNotIn('Access-Control-Allow-Origin', response.headers)
        self.assertEqual(self.raw('Content-Length: 2\r\nContent-Type: application/json\r\n',
                                  b'{}', path='http://['), 400)

    def test_options_preflight_is_not_served(self):
        status, headers, _ = self.request('OPTIONS', headers={
            'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'Content-Type'})
        self.assertEqual(status, 501)
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual(self.requests, [])

    def test_other_write_paths_methods_and_encoded_bypasses_are_rejected(self):
        for method, path in [('POST', '/api/cameras'), ('PATCH', '/api/health'),
                             ('DELETE', COLLECTION), ('POST', ITEM), ('PATCH', COLLECTION),
                             ('POST', '/ws'), ('POST', COLLECTION + '/'),
                             ('POST', COLLECTION.replace('parking-a', 'parking-a%2f..')),
                             ('POST', COLLECTION.replace('parking-a', 'parking-a%252f..'))]:
            with self.subTest(method=method, path=path):
                self.assertIn(self.request(method, path, b'{}')[0], (404, 405))
        self.assertEqual(self.requests, [])

    def test_invalid_framing_oversize_truncation_and_timeout_never_reach_backend(self):
        for headers, body, close, expected in [
            ('Content-Type: application/json\r\n', b'', False, 411),
            ('Content-Length: -1\r\n', b'', False, 400),
            ('Content-Length: x\r\n', b'', False, 400),
            ('Content-Length: 2\r\nContent-Length: 2\r\n', b'{}', False, 400),
            ('Content-Length: 65537\r\n', b'', False, 413),
            ('Transfer-Encoding: chunked\r\n', b'', False, 400),
            ('Content-Length: 2\r\nTransfer-Encoding: chunked\r\n', b'{}', False, 400),
            ('Content-Length: 2\r\nExpect: 100-continue\r\n', b'{}', False, 417),
            ('Content-Length: 2\r\nContent-Type: text/plain\r\n', b'{}', False, 415),
            ('Content-Length: 2\r\nContent-Type: application/json\r\nContent-Type: application/json\r\n', b'{}', False, 415),
            ('Content-Length: 5\r\nContent-Type: application/json\r\n', b'{}', True, 400),
            ('Content-Length: 5\r\nContent-Type: application/json\r\n', b'', False, 408),
        ]:
            with self.subTest(headers=headers):
                self.assertEqual(self.raw(headers, body, half_close=close), expected)
        self.assertEqual(self.requests, [])

    def test_backend_validation_and_connection_failure_are_preserved_and_sanitized(self):
        status, _, payload = self.request('POST', data=b'invalid-json')
        self.assertEqual((status, json.loads(payload)), (422, {'detail': 'Invalid request'}))
        self.backend.shutdown()
        self.backend.server_close()
        status, _, payload = self.request('POST', data=b'{}')
        self.assertEqual(status, 502)
        self.assertNotIn(b'127.0.0.1', payload)

    def test_body_limit_accepts_exact_boundary_and_handles_unbuffered_short_reads(self):
        body = b'{}' + b' ' * (self.proxy.MAX_REQUEST_BYTES - 2)
        self.assertEqual(self.request('POST', data=body)[0], 201)
        self.assertEqual(self.requests[-1][3], body)

    def test_backend_timeout_and_oversized_response_fail_without_private_details(self):
        connection = Mock()
        connection.getresponse.side_effect = socket.timeout('private upstream')
        with patch.object(self.proxy, 'connection_class', return_value=connection):
            status, _, body = self.request('POST', data=b'{}')
            self.assertEqual(status, 502)
            self.assertNotIn(b'private upstream', body)
        response = Mock(status=200)
        response.read.return_value = b'x' * (4 * 1024 * 1024 + 1)
        connection.getresponse.side_effect = None
        connection.getresponse.return_value = response
        with patch.object(self.proxy, 'connection_class', return_value=connection):
            self.assertEqual(self.request('GET')[0], 502)

    def test_dataset_write_routes_and_payload_preserve_existing_bounds(self):
        image = '/api/datasets/images/11111111-1111-4111-8111-111111111111'
        self.spaces.append({})
        body = b'{"revision":1,"boxes":[],"status":"in_progress"}'
        status, _, payload = self.request('PUT', image + '/annotations', body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)['revision'], 1)
        self.assertEqual(self.requests[-1][0], 'PUT')
        self.assertEqual(self.requests[-1][3], body)
        for path in ('/api/datasets/capture', '/api/datasets/exports'):
            self.assertEqual(self.request('POST', path, b'{}')[0], 201)
        self.assertEqual(self.request('DELETE', image)[0], 204)
        export = image.replace('/images/', '/exports/')
        self.assertEqual(self.request('DELETE', export)[0], 204)
        self.assertEqual(self.request('PATCH', export, b'{}')[0], 405)
        self.assertEqual(self.request('DELETE', '/api/datasets/exports/not-a-uuid')[0], 405)
        self.assertEqual(self.request('PUT', image, body)[0], 405)
        self.assertEqual(self.request('PUT', image + '/annotations', b'x' * 65537)[0], 413)

    def test_dataset_zip_stream_exceeds_json_limit_and_preserves_download_headers(self):
        self.archive = b'PK' + bytes(range(256)) * 20000
        path = '/api/datasets/exports/11111111-1111-4111-8111-111111111111/download'
        status, headers, body = self.request('GET', path)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.archive)
        self.assertEqual(headers['Content-Disposition'], 'attachment; filename="dataset_fixture.zip"')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        status, headers, body = self.request('HEAD', path)
        self.assertEqual((status, body), (200, b''))
        self.assertEqual(int(headers['Content-Length']), len(self.archive))

    def test_dataset_download_rejects_invalid_upstream_file_headers(self):
        connection = Mock()
        response = Mock(status=200)
        response.getheader.side_effect = lambda name, default='': {'Content-Length': '12', 'Content-Type': 'text/html'}.get(name, default)
        connection.getresponse.return_value = response
        with patch.object(self.proxy, 'connection_class', return_value=connection):
            self.assertEqual(self.request('GET', '/api/datasets/exports/11111111-1111-4111-8111-111111111111/download')[0], 502)

    def test_training_control_routes_forward_authorization_and_block_worker_api(self):
        job = '/api/training/jobs/11111111-1111-4111-8111-111111111111'
        for path in ('/api/training/jobs', '/api/training/auth', '/api/training/evaluations', '/api/training/deployments',
                     '/api/training/autolabels', job + '/stop'):
            self.assertEqual(self.request('POST', path, b'{}', {'Authorization': 'Bearer token'})[0], 201)
            self.assertEqual(self.requests[-1][2]['Authorization'], 'Bearer token')
        self.assertEqual(self.request('GET', '/api/training/autolabels/plan?model_id=yolo11m-coco&camera_id=cam')[0], 200)
        self.assertEqual(self.requests[-1][1], '/api/training/autolabels/plan?model_id=yolo11m-coco&camera_id=cam')
        self.assertEqual(self.request('DELETE', job)[0], 405)
        self.assertEqual(self.request('POST', job + '/artifacts/best.pt', b'{}')[0], 405)
        count = len(self.requests)
        for path in ('/api/training/worker/heartbeat', '/api/training/worker/claim',
                     '/api/training/%77orker/claim', '/api/training/worker'):
            self.assertEqual(self.request('POST', path, b'{}')[0], 404)
            self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(len(self.requests), count)

    def test_training_model_download_is_streamed(self):
        self.archive = bytes(range(256)) * 20000  # larger than the 4 MiB JSON cap once repeated below
        self.archive *= 2
        status, headers, body = self.request('GET', '/api/training/models/11111111-1111-4111-8111-111111111111/files/best')
        self.assertEqual((status, body), (200, self.archive))
        self.assertEqual(headers['Content-Type'], 'application/octet-stream')


if __name__ == '__main__':
    unittest.main()
