"""Synthetic health-boundary checks: no Tk root, decoder, or network."""
import unittest


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(value) for name, value in globals().items()
                              if name.startswith('test_') and callable(value))


from smart_tapo.desktop_controller import DesktopController
from smart_tapo.desktop_rtsp_audio import RtspListenerPanel


def test_audio_poll_exposes_stale_pcm_without_decoder_error_event():
    from types import SimpleNamespace
    class Value:
        def __init__(self):self.value=''
        def set(self,value):self.value=value
        def get(self):return self.value
    obj=audio()
    obj.health_status=Value()
    obj.runtime=SimpleNamespace(poll=lambda:[],is_running=False)
    obj._prepare_future=None
    obj.closing=obj._stopping=obj._active=False
    obj.profile_dialog=None
    obj._retry=lambda:None
    obj._buttons=lambda:None
    packet=dict(kind='listener_progress',heartbeat=12.,pcm_started_at=11.,
        pcm_session='a'*32,audio_frames=100,audio_rms=0.,waiting_audio=False,stage='persisted')
    obj._accept_health(packet,12.)
    obj._progress_diagnostics(packet)
    obj.clock=lambda:30.
    obj.poll()
    assert 'audio_no_pcm' in obj.health_status.get()


def vision():
    obj = DesktopController.__new__(DesktopController)
    obj._health_expected = True
    obj._health_generation = 2
    obj._health_since = 10.
    obj._health_observed = obj._health_good = None
    obj._health_reason = 'starting'
    obj._health_max_age = 5.
    obj._roles = {r: {'state': 'running'} for r in ('capture', 'worker')}
    return obj


def audio():
    from smart_tapo.audio_recovery import AudioRecovery
    obj = RtspListenerPanel.__new__(RtspListenerPanel)
    obj._recovery = AudioRecovery()
    obj._watching = False  # Snapshot-only fixture, no consented process/retry owner.
    obj._retry_at = None
    obj.clock = lambda: 12.
    obj._health_expected = True
    obj._health_generation = 1
    obj._health_since = 10.
    obj._health_observed = obj._health_good = obj._health_pcm = None
    obj._health_reason = 'starting'
    return obj


def test_vision_freshness_and_unexpected_clean_exit():
    obj = vision()
    obj._accept_health(dict(sequence=1, session='synthetic', observed_at=12., captured_at=11., healthy=True, reason='unknown'), 12.)
    assert obj.health_snapshot(12.)['healthy']
    assert not obj.health_snapshot(17.)['healthy']
    obj._roles['worker']['state'] = 'stopped'
    assert obj.health_snapshot(12.)['expected']
    assert obj.health_snapshot(12.)['reason'] == 'vision_worker_failed'
    obj.stop('worker')
    assert not obj.health_snapshot(12.)['expected']


def test_vision_rejects_old_future_nonfinite_and_quality():
    obj = vision()
    for captured, observed in ((9., 11.), (11., 13.), (float('nan'), 12.)):
        obj._accept_health(dict(sequence=1, session='synthetic', observed_at=observed, captured_at=captured, healthy=True, reason='unknown'), 12.)
        assert not obj.health_snapshot(12.)['healthy']
    obj._accept_health(dict(sequence=1, session='synthetic', observed_at=12., captured_at=11., healthy=False, reason='vision_alignment'), 12.)
    assert obj.health_snapshot(12.)['reason'] == 'vision_alignment'


def test_quiet_pcm_healthy_status_heartbeat_not_healthy():
    obj = audio()
    packet = dict(kind='listener_progress', heartbeat=12., pcm_started_at=11.,
                  pcm_session='a'*32, audio_frames=100, audio_rms=0., waiting_audio=False, stage='persisted')
    obj._accept_health(packet, 12.)
    assert obj.health_snapshot(12.)['healthy']
    obj._accept_health(dict(kind='listener_status', heartbeat=13., code='rtsp_disconnected', waiting_audio=True), 13.)
    assert not obj.health_snapshot(13.)['healthy']
    obj._accept_health(dict(packet, heartbeat=13.), 13.)
    assert not obj.health_snapshot(13.)['healthy']
    obj._accept_health(dict(packet, heartbeat=14., audio_frames=200), 14.)
    assert obj.health_snapshot(14.)['healthy']
    assert not obj.health_snapshot(23.)['healthy']


def test_storage_permanent_stop_retains_user_intent_and_redaction():
    obj = audio()
    obj._accept_health(dict(kind='error', code='storage_corrupt', message='rtsp://secret'), 12.)
    snapshot = obj.health_snapshot(12.)
    assert snapshot['expected'] and not snapshot['healthy']
    assert snapshot['reason'] == 'audio_storage'
    assert 'secret' not in repr(snapshot)
