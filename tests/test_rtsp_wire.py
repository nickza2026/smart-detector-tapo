"""Actual loopback RTSP/TCP/RTP-JPEG integration; never touches configured cameras.

The tiny RFC2435 fixture uses only an ephemeral owned localhost socket and
synthetic pixels. It is deliberately not a general-purpose media server.
"""
import io
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from dataclasses import replace

from PIL import Image

from smart_tapo.capture.rtsp import RtspReader, capture_rtsp, rtsp_source_fingerprint, valid_rtsp_proof
from smart_tapo.frames import read_frame
from tests.test_rtsp import config_for


def jpeg_payload(color):
    stream = io.BytesIO()
    image=color if isinstance(color,Image.Image) else Image.new('RGB',(64,48),color)
    image.save(stream, 'JPEG', quality=75, subsampling=2)
    width,height=image.size
    if width%8 or height%8 or max(width,height)>2040:raise ValueError('Invalid synthetic JPEG dimensions')
    data = stream.getvalue()
    tables = {}
    offset = 2
    while offset < len(data):
        if data[offset] != 255:
            raise AssertionError('Invalid synthetic JPEG')
        marker = data[offset+1]
        length = int.from_bytes(data[offset+2:offset+4], 'big')
        body = data[offset+4:offset+2+length]
        if marker == 0xdb:
            cursor = 0
            while cursor < len(body):
                table_id = body[cursor] & 15
                tables[table_id] = body[cursor+1:cursor+65]
                cursor += 65
        if marker == 0xda:
            scan = data[offset+2+length:-2]
            return bytes([0, 0, 0, 0, 1, 255, width//8, height//8])+struct.pack('!BBH', 0, 0, 128)+tables[0]+tables[1]+scan
        offset += 2+length
    raise AssertionError('Synthetic JPEG lacks scan')


class SyntheticRtsp:
    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(2)
        self.listener.settimeout(.1)
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        self.requests = []
        self.sent = 0
        self.disconnect_after = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.payloads = [jpeg_payload('red'), jpeg_payload('blue')]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(2)
        self.listener.close()
        if self.thread.is_alive():
            raise AssertionError('Synthetic RTSP fixture did not stop')

    def run(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                try:
                    self.serve(connection)
                except (OSError, ValueError):
                    pass

    def serve(self, connection):
        connection.settimeout(.02)
        buffer = b''
        playing = False
        next_frame = time.monotonic()
        sequence = 0
        while not self.stop.is_set():
            try:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                buffer += chunk
            except socket.timeout:
                pass
            # Ignore client interleaved RTCP without parsing it as an RTSP request.
            while buffer.startswith(b'$') and len(buffer) >= 4:
                length = int.from_bytes(buffer[2:4], 'big')
                if len(buffer) < length+4:
                    break
                buffer = buffer[length+4:]
            while not buffer.startswith(b'$') and b'\r\n\r\n' in buffer:
                request, buffer = buffer.split(b'\r\n\r\n', 1)
                lines = request.decode('ascii').split('\r\n')
                method = lines[0].split()[0]
                self.requests.append(method)
                headers = dict(line.split(':', 1) for line in lines[1:] if ':' in line)
                cseq = headers.get('CSeq', '1').strip()
                body = b''
                extra = 'Session: synthetic\r\n'
                if method == 'OPTIONS':
                    extra += 'Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER\r\n'
                elif method == 'DESCRIBE':
                    body = (f'v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Synthetic\r\n'
                            f'c=IN IP4 127.0.0.1\r\nt=0 0\r\na=control:*\r\n'
                            f'm=video 0 RTP/AVP 26\r\na=rtpmap:26 JPEG/90000\r\n'
                            f'a=framerate:10\r\na=control:trackID=0\r\n').encode()
                    extra += (f'Content-Type: application/sdp\r\n'
                              f'Content-Base: rtsp://127.0.0.1:{self.port}/synthetic/\r\n')
                elif method == 'SETUP':
                    extra += 'Transport: RTP/AVP/TCP;unicast;interleaved=0-1;ssrc=12345678\r\n'
                elif method == 'PLAY':
                    playing = True
                    next_frame = time.monotonic()
                    extra += (f'Range: npt=0.000-\r\n'
                              f'RTP-Info: url=rtsp://127.0.0.1:{self.port}/synthetic/trackID=0;seq=0;rtptime=90000\r\n')
                response = f'RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{extra}Content-Length: {len(body)}\r\n\r\n'.encode()+body
                connection.sendall(response)
                if method == 'TEARDOWN':
                    return
            if playing and time.monotonic() >= next_frame:
                packet = struct.pack('!BBHII', 128, 128+26, sequence % 65536,
                                     90000+sequence*9000, 0x12345678)+self.payloads[sequence % len(self.payloads)]
                connection.sendall(b'$\x00'+struct.pack('!H', len(packet))+packet)
                sequence += 1
                self.sent += 1
                next_frame += .1
                if self.disconnect_after is not None and sequence >= self.disconnect_after:
                    return


class RtspWireTests(unittest.TestCase):
    def test_actual_rtsp_transport_proof_and_owned_stop(self):
        with tempfile.TemporaryDirectory() as directory, SyntheticRtsp() as relay:
            config = config_for(directory)
            config = replace(config, rtsp_url=f'rtsp://127.0.0.1:{relay.port}/synthetic',
                             expected_size=(64, 48), rtsp_open_timeout_seconds=3.,
                             rtsp_read_timeout_seconds=2.)
            config = replace(config, rtsp_source_binding=rtsp_source_fingerprint(config))
            started = time.monotonic()
            result = capture_rtsp(config, threading.Event(), once=True)
            self.assertEqual(result, 0, f'No eligible RTSP frame; methods={relay.requests}; sent={relay.sent}')
            self.assertLess(time.monotonic()-started, 11)
            self.assertIn('DESCRIBE', relay.requests)
            self.assertIn('SETUP', relay.requests)
            self.assertIn('PLAY', relay.requests)
            marker = next((Path(directory)/'frames'/config.camera_id).glob('*.ready.json'))
            frame, image = read_frame(marker, config)
            # capture_rtsp has already stopped/reaped its native decoder. That
            # cleanup can consume the2s live TTL on a loaded Windows host.
            # Check wire provenance at receipt, not pretend cleanup makes an
            # expired frame fresh. The production worker still uses real now.
            self.assertTrue(valid_rtsp_proof(frame, config, image,
                now=frame.captured_at,monotonic=frame.captured_monotonic))
            age=min(config.rtsp_max_receive_age_seconds,config.max_live_age_seconds)
            self.assertFalse(valid_rtsp_proof(frame, config, image,
                now=frame.captured_at+age+1,monotonic=frame.captured_monotonic+age+1))
            with Image.open(image) as source:
                self.assertEqual(source.size, (64, 48))

    def test_actual_reconnect_starts_new_session_and_slow_consumer_gets_latest(self):
        with tempfile.TemporaryDirectory() as directory, SyntheticRtsp() as relay:
            relay.disconnect_after = 40
            config = replace(config_for(directory),
                             rtsp_url=f'rtsp://127.0.0.1:{relay.port}/synthetic',
                             rtsp_reconnect_seconds=.1)
            sessions = set()
            deadline = time.monotonic()+13
            with RtspReader(config) as reader:
                while time.monotonic() < deadline and len(sessions) < 2:
                    row = reader.get()
                    if row is not None and len(row) == 11 and row[5]:
                        sessions.add(row[0])
                self.assertEqual(len(sessions), 2, f'Only {len(sessions)} healthy sessions')
                time.sleep(.5)  # emulate slow vision without blocking decoder
                row = reader.get()
                self.assertIsNotNone(row)
                self.assertEqual(len(row), 11)
                self.assertEqual(row[10], ())
                self.assertLess(time.monotonic()-row[2], .4)
                self.assertGreater(row[1], 40)

    def test_stop_terminates_owned_decoder_during_unresponsive_open(self):
        # Owned random loopback socket deliberately never answers RTSP OPTIONS.
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            config = replace(config_for(directory),
                             rtsp_url=f'rtsp://127.0.0.1:{listener.getsockname()[1]}/silent',
                             rtsp_open_timeout_seconds=30.)
            listener.settimeout(5)
            connection = None
            try:
                with RtspReader(config):
                    connection, _ = listener.accept()
                    started = time.monotonic()
                self.assertLess(time.monotonic()-started, 2.5)
            finally:
                if connection is not None:
                    connection.close()


if __name__ == '__main__':
    unittest.main()
