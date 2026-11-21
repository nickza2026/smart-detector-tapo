"""Explicit TEST-only Windows output capture. PCM never leaves the owned child."""
from copy import deepcopy
from dataclasses import asdict
import multiprocessing
import queue
import re
import time

from .offline_audio import _analyze_chunks, DiagnosticCancelled
from .offline_test_rounds import TestRoundTracker


def validate_device(device):
    if (not isinstance(device, dict) or not isinstance(device.get('endpoint_id'), str)
            or re.fullmatch(r'\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}', device['endpoint_id']) is None
            or type(device.get('index')) is not int or device['index'] < 0
            or not isinstance(device.get('name'), str) or not device['name']
            or type(device.get('rate')) is not int or not 8000 <= device['rate'] <= 192000
            or device['rate'] % 100 or type(device.get('channels')) is not int
            or not 1 <= device['channels'] <= 8):
        raise ValueError('explicit_wasapi_output_required')
    return deepcopy(device)


def analyze_stream(chunks, rate, channels, profile, stop, publish):
    """One continuous matcher timeline; shared offline onset/lookahead rules."""
    import numpy as np
    tracker = TestRoundTracker()
    state = dict(kind='playback_test', mode='TEST_NOT_LIVE_NOT_CAMERA_PROOF',
                 received=False, rms=None, peak=None, elapsed=0., scores={},
                 rounds=tracker.snapshot(), incomplete=True, maxima={})
    def emit():
        state['rounds'] = tracker.snapshot()
        publish(deepcopy(state))
    def candidate(row):
        tracker.consume(row)
        state['scores'][row.label] = asdict(row)
        emit()
    def diagnostics(rows):
        state['maxima']={row.label:asdict(row) for row in rows}
        emit()
    def measured():
        frames = 0
        for part in chunks:
            x = np.asarray(part, dtype='float32')
            if (x.ndim != 2 or x.shape[1] != channels or not 0 < len(x) <= rate//2
                    or not np.isfinite(x).all() or np.max(np.abs(x)) > 1.001):
                raise ValueError('invalid_pcm')
            frames += len(x)
            state.update(received=True, rms=float(np.sqrt(np.mean(x.astype('float64')**2))),
                         peak=float(np.max(np.abs(x))), elapsed=frames/rate)
            emit()
            yield x
        if not stop.is_set():
            raise ValueError('unexpected_capture_end')
    emit()
    try:
        _analyze_chunks(measured(), rate, channels, 0, profile, stop,
                        'direct_windows_output_TEST', candidate, None, diagnostics=diagnostics)
    except DiagnosticCancelled:
        pass
    finally:
        state['rounds'] = tracker.snapshot()
        state['incomplete'] = True  # Stop/EOF never invents an end phrase.
        emit()
    return state


def playback_job(final, updates, stop, device, config, profile_path, manifest, api=None):
    audio = stream = None
    result = dict(kind='stopped', incomplete=True)
    def publish(state):
        # Every update contains the complete bounded pairing state. Replace old
        # levels rather than losing a candidate when the GUI is busy.
        for _ in range(3):
            try:
                updates.put_nowait(state)
                return
            except queue.Full:
                try: updates.get_nowait()
                except queue.Empty: pass
    try:
        from .announcement_profile import load_announcement_profile
        if not profile_path:
            from .prepared_profile import prepare_profile
            profile_path = prepare_profile(config.data_dir, config.camera_id, manifest)
        profile = load_announcement_profile(profile_path, config.camera_id)
        if stop.is_set(): return
        if api is None:
            import pyaudiowpatch as api
        from .audio import loopback_devices
        audio = api.PyAudio()
        available = loopback_devices(audio, api)
        matches = [d for d in available if all(d.get(k) == device[k]
                   for k in ('endpoint_id', 'name', 'rate', 'channels'))]
        if len(matches) != 1: raise ValueError('device_identity_changed')
        chosen = matches[0]
        rate, channels = chosen['rate'], chosen['channels']
        if stop.is_set(): return
        stream = audio.open(format=api.paInt16, channels=channels, rate=rate, input=True,
                            input_device_index=chosen['index'], frames_per_buffer=rate//4)
        def chunks():
            import numpy as np
            last_data = last_tick = time.monotonic()
            while not stop.is_set():
                now = time.monotonic()
                if now-last_tick > 3: raise ValueError('capture_discontinuity')
                last_tick = now
                count = stream.get_read_available()
                if type(count) is not int or count < 0 or count > rate*2:
                    raise ValueError('capture_discontinuity')
                if not count:
                    if now-last_data > 3: raise ValueError('capture_unreceived_timeout')
                    stop.wait(.01)
                    continue
                count = min(count, rate//4)
                raw = stream.read(count, exception_on_overflow=True)
                if len(raw) != count*channels*2: raise ValueError('capture_short_read')
                last_data = time.monotonic()
                yield np.frombuffer(raw, dtype='<i2').reshape(-1, channels).astype('float32')/32768
        result['snapshot'] = analyze_stream(chunks(), rate, channels, profile, stop, publish)
    except Exception as exc:
        result = dict(kind='error', code=str(exc) if isinstance(exc, ValueError) else 'capture_failed', incomplete=True)
    finally:
        try:
            if stream is not None: stream.close()
            if audio is not None: audio.terminate()
        except Exception: result = dict(kind='error', code='capture_cleanup_failed', incomplete=True)
        updates.cancel_join_thread()
        try: final.send(result)
        finally: final.close()


class PlaybackTest:
    def __init__(self, *, context=None, clock=time.monotonic, target=playback_job):
        self.context, self.clock, self.target = context, clock, target
        self.process = None
        self.closing = False
        self.snapshot = None

    @property
    def is_running(self): return self.process is not None

    def start(self, config, device, profile_path='', manifest='', *, consent=False, other_running=False):
        if self.closing or self.is_running or other_running: raise ValueError('busy_or_closing')
        if consent is not True: raise ValueError('explicit_current_action_consent_required')
        device = validate_device(device)
        ctx = self.context or multiprocessing.get_context('spawn')
        self.recv, send = ctx.Pipe(duplex=False)
        self.updates = ctx.Queue(maxsize=8)
        self.cancel = ctx.Event()
        self.stop_at = None
        self.last_update = self.clock()
        self.snapshot = None
        process = ctx.Process(target=self.target, args=(send, self.updates, self.cancel,
                              device, deepcopy(config), str(profile_path), str(manifest)), daemon=True)
        try: process.start()
        except Exception:
            send.close(); self.recv.close(); self.updates.close()
            raise
        send.close()
        self.process = process

    def stop(self):
        if self.process is not None:
            self.cancel.set()
            if self.stop_at is None: self.stop_at = self.clock()

    def poll(self):
        if self.process is None: return []
        events = []
        for _ in range(32):
            try:
                self.snapshot = self.updates.get_nowait()
                self.last_update = self.clock()
            except queue.Empty: break
        if self.snapshot is not None: events.append(self.snapshot)
        now = self.clock()
        if now-self.last_update > 15 and self.stop_at is None:
            self.stop(); events.append(dict(kind='error', code='capture_worker_timeout', incomplete=True))
        if self.stop_at is not None and now-self.stop_at > 1 and self.process.is_alive():
            self.process.terminate()
            self.process.join(.1)
            if self.process.is_alive(): self.process.kill()
        if not self.process.is_alive():
            self.process.join(.1)
            try:
                if self.recv.poll(): events.append(self.recv.recv())
                else: events.append(dict(kind='stopped', incomplete=True))
            # Windows raises BrokenPipeError, not EOFError, once a killed child's write end is gone.
            except (EOFError, BrokenPipeError): events.append(dict(kind='error', code='capture_worker_exit', incomplete=True))
            self.recv.close(); self.updates.close(); self.updates.cancel_join_thread()
            self.process.close(); self.process = None
        return events

    def close(self):
        self.closing = True
        self.stop()
        if self.process is not None:
            self.process.join(.2)
            if self.process.is_alive(): self.process.terminate(); self.process.join(.3)
            if self.process.is_alive(): self.process.kill(); self.process.join(.3)
            self.poll()
