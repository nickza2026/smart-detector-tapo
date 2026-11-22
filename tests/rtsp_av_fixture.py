"""Owned synthetic RTSP/TCP fixture, derived from .tools/rtsp_av_fixture_v31.py.

Only ephemeral 127.0.0.1; no camera, credentials or real recordings.
"""
import re
import socket
import struct
import threading
import time

import numpy as np
from tests.test_rtsp_wire import jpeg_payload


class SyntheticAV:
    def __init__(self, image, pcm, *, seconds=90, audio=True, disconnect_after=None):
        self.video = jpeg_payload(image)
        if len(self.video) > 60000:
            raise ValueError('Synthetic JPEG must fit one RTP frame')
        self.pcm = np.clip(np.asarray(pcm).reshape(-1)*32767, -32768, 32767).astype('>i2').tobytes()
        if not self.pcm:
            raise ValueError('Synthetic PCM required')
        self.audio, self.disconnect_after = audio, disconnect_after
        self.seconds = seconds
        self.stop = threading.Event()
        self.errors, self.requests = [], []
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.sock.settimeout(.1)
        self.port = self.sock.getsockname()[1]
        self.url = f'rtsp://127.0.0.1:{self.port}/synthetic-av'
        self.threads, self.connections = [], []
        self.thread = threading.Thread(target=self.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def run(self):
        deadline = time.monotonic()+self.seconds
        while not self.stop.is_set() and time.monotonic() < deadline:
            try:
                connection, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections.append(connection)
            thread = threading.Thread(target=self.serve, args=(connection,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def serve(self, connection):
        buffer = b''
        channels = {}
        playing = False
        sequence = {0: 0, 1: 0}
        due = {0: 0., 1: 0.}
        started = None
        deadline = time.monotonic()+self.seconds
        connection.settimeout(.005)
        try:
            while not self.stop.is_set() and time.monotonic() < deadline:
                if started is not None and self.disconnect_after is not None and time.monotonic()-started >= self.disconnect_after:
                    return
                try:
                    chunk = connection.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                except socket.timeout:
                    pass
                while buffer.startswith(b'$') and len(buffer) >= 4:
                    size = int.from_bytes(buffer[2:4], 'big')
                    if len(buffer) < size+4:
                        break
                    buffer = buffer[size+4:]
                while b'\r\n\r\n' in buffer and not buffer.startswith(b'$'):
                    request, buffer = buffer.split(b'\r\n\r\n', 1)
                    lines = request.decode('ascii', 'replace').split('\r\n')
                    method, uri, *_ = lines[0].split()
                    self.requests.append(method)
                    headers = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip() for line in lines[1:] if ':' in line}
                    extra, body = ['Session: synthetic-av'], b''
                    if method == 'OPTIONS':
                        extra += ['Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER']
                    elif method == 'DESCRIBE':
                        sdp = ('v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Owned synthetic AV\r\n'
                               'c=IN IP4 127.0.0.1\r\nt=0 0\r\na=control:*\r\n'
                               'm=video 0 RTP/AVP 26\r\na=rtpmap:26 JPEG/90000\r\na=framerate:10\r\na=control:trackID=0\r\n')
                        if self.audio:
                            sdp += 'm=audio 0 RTP/AVP 96\r\na=rtpmap:96 L16/16000/1\r\na=control:trackID=1\r\n'
                        body = sdp.encode()
                        extra += ['Content-Type: application/sdp', 'Content-Base: '+self.url+'/']
                    elif method == 'SETUP':
                        track = 1 if uri.endswith('trackID=1') else 0
                        match = re.search(r'interleaved=(\d+)-(\d+)', headers.get('transport', ''))
                        if not match:
                            raise ValueError('Only owned TCP fixture supported')
                        channels[track] = int(match.group(1))
                        extra += ['Transport: RTP/AVP/TCP;unicast;interleaved='+match.group(0).split('=', 1)[1]]
                    elif method == 'PLAY':
                        playing = True
                        started = time.monotonic()
                        due = {0: started, 1: started}
                        extra += ['Range: npt=0.000-', 'RTP-Info: '+','.join(self.url+'/trackID='+str(track)+';seq=0;rtptime=100000' for track in channels)]
                    response = ['RTSP/1.0 200 OK', 'CSeq: '+headers.get('cseq', '1'), *extra, 'Content-Length: '+str(len(body))]
                    connection.sendall(('\r\n'.join(response)+'\r\n\r\n').encode()+body)
                    if method == 'TEARDOWN':
                        return
                now = time.monotonic()
                for track, channel in list(channels.items()):
                    if not playing or now < due[track]:
                        continue
                    index = sequence[track]
                    if track == 0:
                        payload, pt, stamp, period = self.video, 26, index*9000, .1
                    else:
                        # Keep one immutable snapshot and allocate only this
                        # 640-byte packet, not two copies of the full waveform
                        # every 20ms. Preserve exact wrap and RTP timestamps.
                        pcm = self.pcm
                        offset = (index*640) % len(pcm)
                        payload = pcm[offset:offset+640]
                        missing = 640-len(payload)
                        if missing:
                            whole,tail = divmod(missing,len(pcm))
                            payload += pcm*whole + pcm[:tail]
                        pt, stamp, period = 96, index*320, .02
                    packet = struct.pack('!BBHII', 128, 128|pt, index % 65536, stamp+100000, 0x12345678+track)+payload
                    connection.sendall(bytes((36, channel))+struct.pack('!H', len(packet))+packet)
                    sequence[track] += 1
                    due[track] += period
        except (OSError, ConnectionError):
            pass
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            connection.close()

    def __exit__(self, *unused):
        self.stop.set()
        self.sock.close()
        for connection in self.connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.thread.join(2)
        for thread in self.threads:
            thread.join(2)
        if self.thread.is_alive() or any(t.is_alive() for t in self.threads):
            raise RuntimeError('Owned AV fixture did not reap')
