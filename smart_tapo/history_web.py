"""Read-only, authenticated loopback viewer for deposit evidence.

No listener is created until ``start``. Give the bootstrap ``url`` to the local
browser; its random credential is exchanged for a port-specific session cookie.
"""
from __future__ import annotations

import base64
import hmac
import io
import json
import math
import os
import re
import secrets
import socket
import stat
import threading
from datetime import datetime
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from .history import (HistoryUnavailable, IMAGE_LABELS, KIND_LABELS,
                      get_event, get_images, list_events)

_TITLE = 'ประวัติการหยอดแบงค์'
_MAX_IMAGE = 16 * 1024 * 1024
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_STYLE = ('body{font:16px system-ui,sans-serif;max-width:960px;margin:2em auto;'
          'padding:0 1em;line-height:1.6;color:#18212b;background:#fafafa}'
          'article,figure{margin:1em 0;padding:1em;background:white;border:1px solid #ddd;'
          'border-radius:8px}img{max-width:100%;height:auto}a{color:#0756a0}'
          '.notice{padding:1em;background:#fff3cd}dt{font-weight:bold}dd{margin-bottom:.6em}')


def _link(path, **query):
    return escape(path + ('?' + urlencode(query) if query else ''), quote=True)


def _stamp(value):
    if value is None:
        return '—'
    try:
        return escape(datetime.fromtimestamp(value).astimezone().isoformat(timespec='seconds'))
    except (ValueError, OverflowError, OSError, TypeError):
        return '—'


def _document(body, hours):
    return (f'<!doctype html><html lang="th"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{_TITLE}</title><style>{_STYLE}</style><body><h1>{_TITLE}</h1>'
            '<p class="notice">แสดงการพบวัตถุในช่องรับธนบัตรเท่านั้น '
            'ไม่ใช่การยืนยันการชำระเงินหรือยอดเงิน (occupancy, not payment confirmation)</p>'
            f'<p>เก็บเหตุการณ์ปกติหลังจบ {escape(str(hours))} ชั่วโมง</p>{body}</body></html>').encode('utf-8')


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        # Windows SO_REUSEADDR can steal a live listener, unlike POSIX. Keep
        # the selected port exclusive instead of silently sharing its traffic.
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2.0)
        return connection, address

    def handle_error(self, request, client_address):
        # Never print request URLs, credentials, database values or local paths.
        pass


class HistoryWebServer:
    """GUI-safe lifecycle. ``config_provider`` returns one frozen Config per request.

    ``start`` is idempotent while running; stop closes the listener and joins the
    daemon accept thread for at most one second. In-flight handlers are daemons.
    """

    def __init__(self, config_provider):
        self._config_provider = config_provider
        self._server = None
        self._thread = None
        self._lock = threading.RLock()

    @property
    def running(self):
        with self._lock:
            return self._server is not None

    @property
    def port(self):
        with self._lock:
            return self._server.server_port if self._server else None

    @property
    def url(self):
        with self._lock:
            if self._server is None:
                return None
            return f'http://127.0.0.1:{self._server.server_port}/?access={self._server.access_token}'

    def start(self, port=8080):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('Port must be between 0 and 65535')
        with self._lock:
            if self._server is not None:
                return self.url
            server = _HTTPServer(('127.0.0.1', port), _Handler)
            server.config_provider = self._config_provider
            server.access_token = secrets.token_urlsafe(32)
            server.session_token = secrets.token_urlsafe(32)
            server.cookie_name = f'tapo_history_{server.server_port}'
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={'poll_interval': .1}, daemon=True,
                                      name='history-web')
            try:
                thread.start()
            except BaseException:
                server.server_close()
                raise
            self._server, self._thread = server, thread
            return self.url

    def stop(self):
        with self._lock:
            server, thread = self._server, self._thread
            if server is None:
                return
            self._server = self._thread = None
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)


class _Handler(BaseHTTPRequestHandler):
    server_version = 'History'
    sys_version = ''

    def log_message(self, *args):
        pass

    def _reply(self, status, body=b'', content_type='text/plain; charset=utf-8', headers=()):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header('Connection', 'close')
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        if self.command != 'HEAD':
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self._reply(code, b'Request rejected')

    def do_HEAD(self):
        self.do_GET()

    def _unsupported(self):
        self._reply(405, b'Read only', headers=(('Allow', 'GET, HEAD'),))

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = _unsupported

    def _trusted(self):
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        hosts = self.headers.get_all('Host', [])
        if len(hosts) != 1 or hosts[0] not in allowed:
            return False
        origins = self.headers.get_all('Origin', [])
        if origins and (len(origins) != 1 or origins[0] != 'http://' + hosts[0]):
            return False
        sites = self.headers.get_all('Sec-Fetch-Site', [])
        return not sites or (len(sites) == 1 and sites[0] in ('none', 'same-origin'))

    def _authorized(self):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get('Cookie', ''))
            value = cookie.get(self.server.cookie_name)
            return value is not None and hmac.compare_digest(value.value, self.server.session_token)
        except (ValueError, TypeError):
            return False

    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self._reply(503, b'History temporarily unavailable')

    def _get(self):
        if not self._trusted():
            self._reply(403, b'Request rejected')
            return
        if len(self.path) > 4096:
            self._reply(400, b'Invalid route')
            return
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path not in ('/', '/event', '/image'):
            self._reply(404, b'Not found')
            return
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
        except ValueError:
            self._reply(400, b'Invalid query')
            return
        if any(len(values) != 1 for values in query.values()):
            self._reply(400, b'Invalid query')
            return
        if parsed.path == '/' and set(query) == {'access'}:
            token = query['access'][0]
            if not token.isascii() or not hmac.compare_digest(token, self.server.access_token):
                self._reply(403, b'Access denied')
                return
            cookie = (f'{self.server.cookie_name}={self.server.session_token}; Path=/; '
                      'HttpOnly; SameSite=Strict')
            self._reply(303, headers=(('Location', '/'), ('Set-Cookie', cookie)))
            return
        if not self._authorized():
            self._reply(403, b'Access denied')
            return
        config = self.server.config_provider()
        try:
            if parsed.path == '/':
                self._listing(config, query)
            else:
                expected = {'id', 'role'} if parsed.path == '/image' else {'id'}
                if set(query) != expected or not _ID.fullmatch(query.get('id', [''])[0]):
                    self._reply(404, b'Not found')
                    return
                event_id = query['id'][0]
                event = get_event(config, event_id)
                if event is None:
                    self._reply(404, b'Not found')
                elif parsed.path == '/image':
                    self._image(config, event_id, query['role'][0])
                else:
                    self._detail(config, event)
        except HistoryUnavailable:
            self._reply(503, b'History temporarily unavailable')

    def _listing(self, config, query):
        if set(query) - {'before'}:
            self._reply(400, b'Invalid query')
            return
        before = None
        if 'before' in query:
            try:
                raw = query['before'][0]
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,1024}', raw):
                    raise ValueError()
                before = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
                if (not isinstance(before, list) or len(before) != 2
                        or type(before[0]) not in (int, float) or not math.isfinite(before[0])
                        or not isinstance(before[1], str) or len(before[1]) > 128):
                    raise ValueError()
            except (ValueError, TypeError):
                self._reply(400, b'Invalid cursor')
                return
        page = list_events(config, before=before, limit=100)
        body = '<p><a href="/">ล่าสุด</a></p>'
        for event in page.events:
            label = escape(KIND_LABELS.get(event.kind, str(event.kind)))
            body += (f'<article><a href="{_link("/event", id=event.id)}">{_stamp(event.started)}</a>'
                     f'<p>{label}</p>'
                     f'<p>ภาพหลักฐาน: {escape(str(event.image_count))}</p></article>')
        if not page.events:
            body += '<p>ยังไม่มีเหตุการณ์</p>'
        if page.next_before:
            cursor = base64.urlsafe_b64encode(json.dumps(page.next_before).encode()).decode().rstrip('=')
            body += f'<a href="{_link("/", before=cursor)}">เก่ากว่า →</a>'
        headers = (('Refresh', '5'),) if before is None else ()
        self._reply(200, _document(body, config.deposit_history_hours), 'text/html; charset=utf-8', headers)

    def _detail(self, config, event):
        body = ('<a href="/">กลับรายการ</a><h2>รายละเอียดเหตุการณ์จากภาพ</h2>'
                f'<p>{escape(KIND_LABELS.get(event.kind, str(event.kind)))}</p><dl>')
        for label, value in (('เริ่มพบ', event.started), ('พบล่าสุด', event.last_present),
                             ('เริ่มว่าง', event.first_clear), ('จบเหตุการณ์', event.closed)):
            body += f'<dt>{label}</dt><dd>{_stamp(value)}</dd>'
        body += '</dl>'
        images = get_images(config, event.id)
        for image in images:
            label = escape(IMAGE_LABELS[image.role])
            body += (f'<figure><figcaption>{label}</figcaption><img loading="lazy" '
                     f'src="{_link("/image", id=event.id, role=image.role)}" alt="{label}"></figure>')
        if not images:
            body += '<p>ไม่มีภาพหลักฐาน (อาจถูกลบตามอายุการเก็บ)</p>'
        self._reply(200, _document(body, config.deposit_history_hours), 'text/html; charset=utf-8')

    def _image(self, config, event_id, role):
        from PIL import Image

        if role not in IMAGE_LABELS:
            self._reply(404, b'Not found')
            return
        image = next((image for image in get_images(config, event_id) if image.role == role), None)
        try:
            if image is None:
                raise ValueError()
            fd = os.open(image.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_IMAGE:
                    raise ValueError()
                data = stream.read(_MAX_IMAGE + 1)
            if len(data) > _MAX_IMAGE:
                raise ValueError()
            with Image.open(io.BytesIO(data)) as png:
                if (png.format != 'PNG' or png.width > config.expected_size[0]
                        or png.height > config.expected_size[1]):
                    raise ValueError()
                png.verify()
            with Image.open(io.BytesIO(data)) as png:
                png.load()
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            self._reply(404, b'Not found')
            return
        self._reply(200, data, 'image/png')
