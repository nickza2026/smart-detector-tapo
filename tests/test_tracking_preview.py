import time
from types import SimpleNamespace

import unittest
from PIL import Image

from smart_tapo.desktop_tracking import make_packet, validate_packet
from smart_tapo.desktop_controller import DesktopController


def packet():
    return make_packet(Image.new('RGB', (1920, 1080), 'blue'),
                       SimpleNamespace(session_id='session', sequence=1, captured_monotonic=10.),
                       SimpleNamespace(view_corners=(100, 100, 900, 100, 900, 800, 100, 800)),
                       generation=2, observed_monotonic=11., safe=True)


def valid(p):
    return validate_packet(p, now=12., max_age=5., size=(1920, 1080), generation=2)


def test_same_frame_geometry_and_bounded_thumbnail():
    p = valid(packet())
    assert p['size'] == (1920, 1080)
    assert p['corners'] == (100, 100, 900, 100, 900, 800, 100, 800)
    assert len(p['image']) <= 128 * 1024
    assert (p['sequence'], p['generation']) == (1, 2)


INVALID_FIELDS = [
    ('sequence', True), ('generation', True), ('generation', 1),
    ('sequence', -1), ('captured_monotonic', float('nan')),
    ('captured_monotonic', True), ('captured_monotonic', 13.),
    ('captured_monotonic', 1.), ('observed_monotonic', 13.),
    ('image', b'not image'), ('image', b'x' * (128 * 1024 + 1)),
    ('size', (True, 1080)), ('size', (640, 360)),
    ('corners', (float('nan'),) * 8), ('corners', (2000,) * 8),
    ('reason', 'secret/path'), ('alignment_ok', 1), ('session', 'rtsp://private'),
]
def test_untrusted_fields_rejected():
    for key, value in INVALID_FIELDS:
        p = packet()
        p[key] = value
        assert valid(p) is None, key


def test_unknown_keys_and_invalid_alignment():
    p = packet()
    p['token'] = 'private'
    assert valid(p) is None
    p = packet()
    p.update(alignment_ok=False, corners=(), reason='unknown')
    assert valid(p)['corners'] == ()
    p['corners'] = packet()['corners']
    assert valid(p) is None


def controller():
    c = DesktopController.__new__(DesktopController)
    c._transport_closed = False
    c._roles = {r: dict(state='running', process=SimpleNamespace(is_alive=lambda: True))
                for r in ('worker', 'capture')}
    c._frame_channel = SimpleNamespace(generation=2)
    c._tracking_size = (1920, 1080)
    c._tracking_since = 9.
    c._health_max_age = 5.
    c._tracking_order = c._tracking_packet = None
    return c


def test_controller_latest_order_generation_stop_stale():
    c = controller()
    c._accept_tracking(packet(), 12.)
    assert c.tracking_snapshot(12.) is not None
    c._accept_tracking(packet(), 12.)
    assert c.tracking_snapshot(12.) is None
    p = packet()
    p.update(sequence=2, captured_monotonic=11.)
    c._accept_tracking(p, 12.)
    assert c.tracking_snapshot(12.) is not None
    c._frame_channel.generation = 3
    assert c.tracking_snapshot(12.) is None
    c = controller()
    c._accept_tracking(packet(), 12.)
    assert c.tracking_snapshot(16.) is None
    c = controller()
    c._accept_tracking(packet(), 12.)
    c._roles['capture']['state'] = 'stopped'
    assert c.tracking_snapshot(12.) is None


def test_native_popup_polygon_and_clear():
    from smart_tapo.smoke_window import install
    install()
    import tkinter as tk
    from smart_tapo.desktop_tracking import DesktopTrackingPopup
    root = tk.Tk()
    root.geometry('1x1+-20000+-20000')
    p = packet()
    now = time.monotonic()
    p.update(captured_monotonic=now - .1, observed_monotonic=now)
    current = [p]
    popup = DesktopTrackingPopup(root, lambda: current[0])
    popup.window.geometry('640x390+-20000+-20000')
    root.update()
    popup.poll()
    kinds = [popup.canvas.type(i) for i in popup.canvas.find_all()]
    assert kinds == ['image', 'polygon']
    coords = popup.canvas.coords(popup.canvas.find_all()[-1])
    assert abs(coords[0] - 100 * popup._photo.width() / 1920) < .01
    current[0] = dict(p, sequence=2, alignment_ok=False, corners=(), reason='unknown')
    popup.poll()
    assert [popup.canvas.type(i) for i in popup.canvas.find_all()] == ['image']
    current[0] = dict(p, captured_monotonic=now - 100.)
    popup.poll()
    assert popup.canvas.find_all() == ()
    current[0] = dict(p, image=b'invalid')
    popup.poll()
    assert popup.canvas.find_all() == ()
    current[0] = None
    popup.poll()
    assert popup.canvas.find_all() == ()
    popup.close()
    assert popup.closed
    root.destroy()


def test_worker_optional_observer_after_decision_and_lock():
    from unittest.mock import patch
    from contextlib import contextmanager
    from smart_tapo.worker import Worker
    from smart_tapo.ram_frames import MemoryFrame, binding
    from PIL import Image
    cfg = SimpleNamespace(expected_size=(1920, 1080), max_live_age_seconds=5.)
    frame = SimpleNamespace(session_id='session', sequence=1, captured_monotonic=10.)
    envelope = MemoryFrame(2, binding(cfg), b'', b'fixture')
    observation = SimpleNamespace(view_corners=(100, 100, 900, 100, 900, 800, 100, 800))
    for enabled in (False, True):
        worker = Worker.__new__(Worker)
        worker.config = cfg
        worker.clock = lambda: 100.
        worker._memory_generation = None
        worker.detector = SimpleNamespace(observe_image=lambda image: observation)
        state = {'locked': False, 'decided': False, 'called': False}
        @contextmanager
        def guard():
            state['locked'] = True
            yield
            state['locked'] = False
        def decision(*args):
            assert state['locked']
            state['decided'] = True
            worker._preview_safe = True
            return 'decision-result'
        def observer(p):
            assert state['decided'] and not state['locked']
            assert p['sequence'] == 1 and p['generation'] == 2
            state['called'] = True
            raise RuntimeError('isolated callback failure')
        worker._process_decision = decision
        worker.preview_observer = observer if enabled else None
        clocks = [11., 11.1] if enabled else [11.]
        with patch.object(MemoryFrame, 'unpack', return_value=frame), \
             patch('smart_tapo.capture.rtsp.valid_rtsp_proof', return_value=True), \
             patch('smart_tapo.vision.load_png', return_value=Image.new('RGB', cfg.expected_size)), \
             patch('smart_tapo.worker.time.monotonic', side_effect=clocks) as clock:
            assert worker.process_memory(envelope, generation=2, generation_guard=guard()) == 'decision-result'
            assert clock.call_count == (2 if enabled else 1)
        assert state['called'] == enabled


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(value) for name, value in globals().items()
                              if name.startswith('test_') and callable(value))
