"""Only synthetic evidence and ephemeral loopback HTTP; never uses devices."""
import http.client
import re
import socket
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from PIL import Image

from smart_tapo.config import Config
from smart_tapo.history_web import HistoryWebServer


class HistoryWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = replace(Config(), data_dir=self.root)
        self.db_path = self.root / 'worker.sqlite3'
        with sqlite3.connect(self.db_path) as db:
            db.executescript('''
                CREATE TABLE settings(name TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE events(id TEXT PRIMARY KEY,kind TEXT,started REAL,
                    last_present REAL,first_clear REAL,closed REAL);
                CREATE TABLE evidence(event TEXT,role TEXT,path TEXT);
            ''')
            db.execute('INSERT INTO settings VALUES (?,?)', ('camera_id', self.config.camera_id))
        self.calls = 0
        def config_provider():
            self.calls += 1
            return self.config
        self.server = HistoryWebServer(config_provider)
        self.addCleanup(self.server.stop)
        self.server.start(0)
        self.cookie = None

    def event(self, event_id='evt', kind='stuck', started=None, closed=None):
        with sqlite3.connect(self.db_path) as db:
            db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',
                       (event_id, kind, time.time() if started is None else started,
                        None, None, closed))

    def image(self, event_id='evt', role='first_full', size=(8, 8)):
        directory = self.root / 'events' / self.config.camera_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'{event_id}-{role}.png'
        Image.new('RGB', size, 'red').save(path)
        with sqlite3.connect(self.db_path) as db:
            db.execute('INSERT INTO evidence VALUES (?,?,?)', (event_id, role, str(path)))
        return path

    def request(self, path='/', method='GET', headers=None):
        values = dict(headers or {})
        if self.cookie and 'Cookie' not in values:
            values['Cookie'] = self.cookie
        conn = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=3)
        try:
            conn.request(method, path, headers=values)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def login(self):
        url = urlsplit(self.server.url)
        status, headers, body = self.request(url.path + '?' + url.query)
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/')
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.cookie = headers['Set-Cookie'].split(';', 1)[0]
        self.assertEqual(body, b'')

    def test_auth_bootstrap_security_headers_and_snapshot(self):
        self.assertEqual(self.request()[0], 403)
        self.assertEqual(self.request('/image?id=evt&role=first')[0], 403)
        self.assertEqual(self.request('/?access=invalid')[0], 403)
        self.login()
        initial = self.calls
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(self.calls, initial + 1)
        self.assertIn('ประวัติการหยอดแบงค์', body.decode())
        self.assertIn(b'not payment confirmation', body)
        self.assertNotIn(urlsplit(self.server.url).query.encode(), body)
        for name, value in {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                            'Referrer-Policy': 'no-referrer',
                            'Cross-Origin-Resource-Policy': 'same-origin'}.items():
            self.assertEqual(headers[name], value)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertFalse(any(key.lower().startswith('access-control') for key in headers))
        self.assertEqual(self.request(method='HEAD')[2], b'')

    def test_list_order_pagination_escape_detail(self):
        for index in range(103):
            self.event(f'e{index:03}', started=1000 + index,
                       kind='<script>alert("x")</script>' if index == 102 else 'stuck')
        self.login()
        status, headers, body = self.request()
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertLess(text.index('e102'), text.index('e101'))
        self.assertEqual(text.count('<article>'), 100)
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)
        cursor = re.search(r'href="(/\?before=[^"]+)"', text).group(1)
        status, headers, body = self.request(cursor)
        self.assertEqual(status, 200)
        self.assertEqual(body.count(b'<article>'), 3)
        self.assertNotIn('Refresh', headers)
        self.assertNotIn(b'e102', body)
        status, headers, body = self.request('/event?id=e102')
        self.assertEqual(status, 200)
        self.assertNotIn('Refresh', headers)
        self.assertIn(b'&lt;script&gt;', body)
        self.assertEqual(self.request('/?before=invalid')[0], 400)

    def test_images_retention_missing_and_bad_png(self):
        self.event()
        path = self.image()
        self.login()
        status, headers, body = self.request('/image?id=evt&role=first_full')
        self.assertEqual((status, headers['Content-Type']), (200, 'image/png'))
        self.assertEqual(body, path.read_bytes())
        status, headers, body = self.request('/event?id=evt')
        self.assertEqual(status, 200)
        self.assertIn(b'/image?id=evt&amp;role=first_full', body)
        self.assertNotIn(str(path).encode(), body)
        path.unlink()
        self.assertEqual(self.request('/image?id=evt&role=first_full')[0], 404)
        path.write_bytes(b'not png')
        self.assertEqual(self.request('/image?id=evt&role=first_full')[0], 404)
        Image.new('RGB', (721, 1)).save(path)
        self.assertEqual(self.request('/image?id=evt&role=first_full')[0], 404)
        self.event('old', 'normal_visual', started=1, closed=time.time() - 25 * 3600)
        self.image('old')
        self.assertEqual(self.request('/event?id=old')[0], 404)
        self.assertEqual(self.request('/image?id=old&role=first_full')[0], 404)
        self.assertEqual(self.request('/event?id=missing')[0], 404)
        self.assertEqual(self.request('/image?id=evt&role=clear')[0], 404)

    def test_image_limit_cleanup_race_and_configurable_retention(self):
        self.event()
        path = self.image()
        self.login()
        with patch('smart_tapo.history_web._MAX_IMAGE', 8):
            self.assertEqual(self.request('/image?id=evt&role=first_full')[0], 404)
        from smart_tapo.history import get_images
        images = get_images(self.config, 'evt')
        path.unlink()
        with patch('smart_tapo.history_web.get_images', return_value=images):
            self.assertEqual(self.request('/image?id=evt&role=first_full')[0], 404)
        self.event('normal', 'normal_visual', started=time.time() - 7200,
                   closed=time.time() - 3600)
        self.assertEqual(self.request('/event?id=normal')[0], 200)
        self.config = replace(self.config, deposit_history_hours=.5)
        self.assertEqual(self.request('/event?id=normal')[0], 404)
        self.assertIn(b'0.5', self.request()[2])

    def test_strict_routes_hosts_origins_and_read_only(self):
        self.login()
        for headers in ({'Host': 'evil.test'}, {'Host': '127.0.0.1:1'},
                        {'Origin': 'https://evil.test'}, {'Origin': 'null'},
                        {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}):
            self.assertEqual(self.request(headers=headers)[0], 403)
        for path in ('/../worker.sqlite3', '/%2e%2e/worker.sqlite3',
                     '/event?id=..%2Fworker.sqlite3', '/image?id=evt&role=../../file',
                     '/event?id=one&id=two', '/?access=one&before=two'):
            self.assertIn(self.request(path)[0], (400, 404))
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE', 'CONNECT'):
            status, headers, body = self.request(method=method)
            self.assertEqual(status, 405)
            self.assertEqual(headers['Allow'], 'GET, HEAD')
            self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_lifecycle_restart_occupied_port(self):
        first_url = self.server.url
        port = self.server.port
        self.assertTrue(self.server.running)
        self.assertEqual(self.server.start(0), first_url)
        self.login()
        old_cookie = self.cookie
        self.server.stop()
        self.server.stop()
        self.assertFalse(self.server.running)
        self.assertIsNone(self.server.port)
        self.assertIsNone(self.server.url)
        with socket.socket() as client:
            client.settimeout(.5)
            self.assertNotEqual(client.connect_ex(('127.0.0.1', port)), 0)
        self.server.start(port)
        self.assertNotEqual(first_url, self.server.url)
        self.cookie = old_cookie
        self.assertEqual(self.request()[0], 403)
        self.login()
        self.assertEqual(self.request()[0], 200)
        other = HistoryWebServer(lambda: self.config)
        self.addCleanup(other.stop)
        with self.assertRaises(OSError):
            other.start(self.server.port)
        self.assertFalse(other.running)
        self.assertIsNone(other.url)
        for invalid in (-1, 65536, '8080', True):
            with self.assertRaises(ValueError):
                other.start(invalid)

    def test_idle_client_does_not_block_stop(self):
        sock = socket.create_connection(('127.0.0.1', self.server.port), timeout=1)
        self.addCleanup(sock.close)
        sock.sendall(b'GET / HTTP/1.1\r\n')
        start = time.monotonic()
        self.server.stop()
        self.assertLess(time.monotonic() - start, 1.5)


if __name__ == '__main__':
    unittest.main()
