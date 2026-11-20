"""Offline lifecycle and persistence tests: never open a real endpoint or database."""
from concurrent.futures import Future
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from smart_tapo import continuous_listener as mod
from smart_tapo.announcement_stream import LiveLease, StreamBatch, StreamObservation
from smart_tapo.capture.page_mode import PageMode, PROFILE
from smart_tapo.config import Config

DEVICE = dict(index=1, name='Output loopback', rate=8000, channels=2)
RUN = 'a' * 32
LIVE = PageMode('live', 'synthetic', PROFILE)


class Channel:
    def __init__(self):
        self.events = []
        self.closed = False
    def send(self, value): self.events.append(value)
    def put_nowait(self,value):self.events.append(value)
    def close(self): self.closed = True
    def cancel_join_thread(self): pass
    def poll(self): return bool(self.events)
    def recv(self): return self.events.pop(0)


class Control(queue.Queue):
    def close(self): pass


class Process:
    exitcode = 0
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.alive = True
        self.terminated = self.killed = self.joined = False
    def start(self): pass
    def is_alive(self): return self.alive
    def terminate(self): self.terminated = True
    def kill(self): self.killed = True; self.alive = False
    def join(self, **kwargs): self.joined = True
    def close(self): pass


class Context:
    def Pipe(self, **kwargs):
        self.recv, self.send = Channel(), Channel()
        return self.recv, self.send
    def Queue(self, maxsize): return Control(maxsize)
    def Event(self):
        import threading
        return threading.Event()
    def Process(self, **kwargs):
        self.process = Process(**kwargs)
        return self.process


class Executor:
    def __init__(self, **kwargs): self.future = Future()
    def submit(self, *args): return self.future
    def shutdown(self, **kwargs): pass


class ListenerTests(unittest.TestCase):
    def test_selected_loopback_adb_reconnect_is_not_itself_live_proof(self):
        from dataclasses import replace
        from smart_tapo.capture import CaptureError
        adb=Mock();adb.read_page_mode.side_effect=[CaptureError('unavailable'),LIVE]
        with patch('smart_tapo.capture.Adb',return_value=adb):
            with self.assertRaises(CaptureError):mod._read_mode(replace(Config(),adb_serial='127.0.0.1:21503'))
            adb.run.assert_called_once_with('connect','127.0.0.1:21503',selected=False)
            self.assertEqual(mod._read_mode(replace(Config(),adb_serial='127.0.0.1:21503')),
                             replace(LIVE,navigation_generation='initial'))

    def test_reconnect_never_targets_external_or_unselected_transport(self):
        from dataclasses import replace
        from smart_tapo.capture import CaptureError
        for serial in ('10.1.2.3:5555','127.0.0.1:99999','emulator-5554'):
            adb=Mock();adb.read_page_mode.side_effect=CaptureError('unavailable')
            with patch('smart_tapo.capture.Adb',return_value=adb):
                with self.assertRaises(CaptureError):mod._read_mode(replace(Config(),adb_serial=serial))
            adb.run.assert_not_called()

    def setUp(self):
        self.now = 100.
        self.context = Context()
        self.listener = mod.ContinuousListener(context=self.context, clock=lambda: self.now,
                                               mode_reader=lambda config: LIVE)
        self.executor = Executor()
        self.patch = patch.object(mod, 'ThreadPoolExecutor', return_value=self.executor)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.listener.close)

    def start(self):
        self.listener.start(Config(), DEVICE, 'not-opened.json', consent=True)

    def prepared(self):
        self.start()
        self.executor.future.set_result(('fingerprint', LIVE))
        self.listener.poll()

    def test_no_autostart_and_explicit_consent(self):
        self.assertFalse(self.listener.is_running)
        self.assertEqual(self.listener.poll(), [])
        for consent in (False, 1, 'yes'):
            with self.assertRaises(ValueError):
                self.listener.start(Config(), DEVICE, 'missing', consent=consent)
        self.assertIsNone(self.listener._executor)

    def test_playback_rejected_before_child(self):
        self.start()
        self.executor.future.set_result(('fingerprint', PageMode('playback', 'fixture', PROFILE)))
        self.assertEqual(self.listener.poll(), [{'kind':'error','code':'live_playback_detected','diagnostics':{'check_seconds':0.0}}])
        self.assertFalse(hasattr(self.context, 'process'))

    def test_stale_initial_result_never_starts(self):
        self.start()
        self.now += 10
        self.executor.future.set_result(('fingerprint', LIVE))
        self.assertEqual(self.listener.poll()[0]['code'], 'initial_check_expired')
        self.assertFalse(hasattr(self.context, 'process'))

    def test_profile_failure_is_not_reported_as_live_failure(self):
        self.start()
        with patch.object(mod,'load_announcement_profile',side_effect=ValueError('PRIVATE file path')):
            try:mod._prepare(Config(),'not-opened',Mock(),True)
            except Exception as error:self.executor.future.set_exception(error)
        event=self.listener.poll()[0]
        self.assertEqual(event['code'],'profile_invalid')
        self.assertNotIn('PRIVATE',repr(event))
        self.assertFalse(hasattr(self.context,'process'))

    def test_transport_failure_is_not_reported_as_profile_failure(self):
        self.start()
        profile=SimpleNamespace(fingerprint='f'*64,templates=[SimpleNamespace(features=SimpleNamespace(shape=(32,200)))])
        with patch.object(mod,'load_announcement_profile',return_value=profile):
            try:mod._prepare(Config(),'not-opened',Mock(side_effect=RuntimeError('PRIVATE ADB output')),True)
            except Exception as error:self.executor.future.set_exception(error)
        event=self.listener.poll()[0]
        self.assertEqual(event['code'],'live_check_unavailable')
        self.assertEqual(self.listener.profile_fingerprint,'f'*64)
        self.assertNotIn('PRIVATE',repr(event))
        self.assertFalse(hasattr(self.context,'process'))

    def test_real_core_clock_fault_is_reported_without_committing_or_opening_another_device(self):
        from dataclasses import replace
        from tests.test_announcement_stream import profile
        loaded=replace(profile(),camera_id='cabinet-1')
        leases=queue.Queue();leases.put(LiveLease.from_page(LIVE,RUN,'cabinet-1',99.,109.))
        final,api,store=Channel(),Mock(),Mock()
        self.now=100.
        def delayed_read(*args,**kwargs):
            self.now=103.
            return bytes(1024*2*2)
        api.PyAudio.return_value.open.return_value.get_read_available.return_value=1024
        api.PyAudio.return_value.open.return_value.read.side_effect=delayed_read
        with patch.object(mod.time,'monotonic',side_effect=lambda:self.now),patch.object(
                mod,'load_announcement_profile',return_value=loaded),patch.object(
                mod,'loopback_devices',return_value=[DEVICE]),patch.object(mod,'RefuelingStore',return_value=store):
            mod.listener_job(final,Channel(),Mock(is_set=lambda:False),leases,Config(),DEVICE,'unused',loaded.fingerprint,RUN,api)
        self.assertEqual(final.events[0]['code'],'audio_clock_discontinuity')
        self.assertAlmostEqual(final.events[0]['diagnostics']['lag_seconds'],2.872,places=3)
        self.assertEqual(final.events[0]['diagnostics']['read_seconds'],3.)
        self.assertEqual(final.events[0]['diagnostics']['audio_seconds'],.128)
        store.accept.assert_not_called();store.tick.assert_not_called()
        api.PyAudio.return_value.open.assert_called_once()

    def test_process_start_failure_is_not_reported_as_page_failure(self):
        self.start()
        self.executor.future.set_result(('fingerprint',LIVE))
        with patch.object(self.context,'Process',side_effect=RuntimeError('PRIVATE OS output')):
            event=self.listener.poll()[0]
        self.assertEqual(event['code'],'listener_start_failed')
        self.assertNotIn('PRIVATE',repr(event))
        self.assertFalse(self.listener.is_running)
        self.assertTrue(self.context.recv.closed)

    def test_unknown_mode_reason_never_exposes_free_text(self):
        self.start()
        self.executor.future.set_result((None,PageMode('unknown','PRIVATE name/path',None)))
        event=self.listener.poll()[0]
        self.assertEqual(event['code'],'live_mode_unconfirmed')
        self.assertNotIn('PRIVATE',repr(event))
        self.assertFalse(hasattr(self.context,'process'))

    def test_scope_freeze_and_single_flight(self):
        device = dict(DEVICE)
        self.listener.start(Config(), device, 'missing', consent=True)
        device['name'] = 'Microphone'
        self.assertEqual(self.listener._device, DEVICE)
        with self.assertRaises(ValueError): self.start()
        self.listener.stop()
        self.assertTrue(self.listener.is_running)
        self.executor.future.set_result(('fingerprint', LIVE))
        self.assertEqual(self.listener.poll(), [])
        self.assertFalse(self.listener.is_running)
        self.assertFalse(hasattr(self.context, 'process'))

    def test_cooperative_stop_and_late_terminal_after_reap(self):
        self.prepared()
        self.listener.stop()
        self.assertTrue(self.listener.is_running)
        process = self.context.process
        def reap(**kwargs):
            process.joined = True
            self.context.recv.send({'kind': 'finished'})
        process.join = reap
        process.alive = False
        self.assertEqual(self.listener.poll(), [{'kind': 'finished'}])
        self.assertTrue(process.joined)
        self.assertFalse(self.listener.is_running)

    def test_escalation_discards_partial_success(self):
        self.prepared()
        self.context.recv.send({'kind': 'finished'})
        self.assertEqual(self.listener.poll(), [])
        self.listener.close()
        self.now += 2
        self.listener.poll()
        self.assertTrue(self.context.process.terminated)
        self.now += 1
        self.assertEqual(self.listener.poll(), [{'kind': 'error', 'code': 'stopped'}])
        self.assertTrue(self.context.process.killed)
        self.assertTrue(self.context.process.joined)

    def test_queued_old_heartbeat_does_not_extend_watchdog(self):
        self.prepared()
        self.listener._progress.put_nowait({'kind': 'listener_progress', 'heartbeat': 100.})
        self.now = 105.
        self.listener.poll()
        self.assertTrue(self.listener._stop.is_set())
        self.assertEqual(self.listener._forced, 'audio_timeout')

    def test_renewal_playback_stops_child_and_owns_pending_check(self):
        self.prepared()
        renewal = Future()
        self.executor.future = renewal
        self.now = 105.
        self.listener._progress.put_nowait({'kind': 'listener_progress', 'heartbeat': 105.})
        self.listener.poll()
        self.assertIs(self.listener._future, renewal)
        renewal.set_result((None, PageMode('playback', 'synthetic', PROFILE)))
        self.listener.poll()
        self.assertTrue(self.listener._stop.is_set())
        self.assertEqual(self.listener._forced, 'live_playback_detected')

    def test_full_control_queue_fails_closed(self):
        self.prepared()
        # Initial lease plus another item fills the bounded control queue.
        self.listener._leases.put_nowait('not consumed')
        renewal = Future()
        self.executor.future = renewal
        self.now = 105.
        self.listener._progress.put_nowait({'kind': 'listener_progress', 'heartbeat': 105.})
        self.listener.poll()
        renewal.set_result((None, LIVE))
        self.listener.poll()
        self.assertTrue(self.listener._stop.is_set())
        self.assertEqual(self.listener._forced, 'live_control_backlog')

    def test_commit_order_is_observations_then_watermark(self):
        store = Mock()
        batch = StreamBatch((StreamObservation('one', 'first'), StreamObservation('two', 'second')), 123., 1)
        mod._commit(store, batch)
        self.assertEqual([tuple(call)[:2] for call in store.mock_calls],
                         [('accept', ('one', 'first')), ('accept', ('two', 'second')), ('tick', (123.,))])
        store.reset_mock()
        store.accept.side_effect = RuntimeError('failure')
        with self.assertRaises(RuntimeError): mod._commit(store, batch)
        store.tick.assert_not_called()

    def test_invalid_leases_never_open_hardware(self):
        for lease in (LiveLease.from_page(LIVE, RUN, 'other', 99., 109.),
                      LiveLease.from_page(LIVE, RUN, 'cabinet-1', 80., 90.),
                      LiveLease.from_page(LIVE, 'b' * 32, 'cabinet-1', 99., 109.)):
            leases = queue.Queue(); leases.put(lease)
            final, progress, api = Channel(), Channel(), Mock()
            with patch.object(mod.time, 'monotonic', return_value=100.):
                mod.listener_job(final, progress, Mock(is_set=lambda: False), leases,
                                 Config(), DEVICE, 'unused', 'f', RUN, api)
            api.PyAudio.assert_not_called()
            self.assertEqual(final.events, [{'kind': 'error', 'code': 'lease_invalid'}])

    def test_profile_changed_never_opens_hardware(self):
        leases = queue.Queue()
        leases.put(LiveLease.from_page(LIVE, RUN, 'cabinet-1', 99., 109.))
        final, api = Channel(), Mock()
        with patch.object(mod.time, 'monotonic', return_value=100.), patch.object(
                mod, 'load_announcement_profile', return_value=SimpleNamespace(fingerprint='changed')):
            mod.listener_job(final, Channel(), Mock(is_set=lambda: False), leases,
                             Config(), DEVICE, 'unused', 'expected', RUN, api)
        api.PyAudio.assert_not_called()
        self.assertEqual(final.events, [{'kind': 'error', 'code': 'profile_invalid'}])

    def test_analysis_expiry_prevents_any_persistence(self):
        leases = queue.Queue()
        leases.put(LiveLease.from_page(LIVE, RUN, 'cabinet-1', 99., 109.))
        final, api, store = Channel(), Mock(), Mock()
        api.PyAudio.return_value.open.return_value.get_read_available.return_value=1024
        api.PyAudio.return_value.open.return_value.read.return_value = bytes(1024 * 2 * 2)
        batch = StreamBatch((StreamObservation('key', 'announcement'),), 123.)
        core = Mock()
        def analyze(*args, **kwargs):
            self.now = 110.
            return batch
        core.push.side_effect = analyze
        self.now = 100.
        with patch.object(mod.time, 'monotonic', side_effect=lambda: self.now), patch.object(
                mod, 'load_announcement_profile', return_value=SimpleNamespace(fingerprint='f')), patch.object(
                mod, 'loopback_devices', return_value=[DEVICE]), patch.object(
                mod, 'AnnouncementStream', return_value=core), patch.object(
                mod, 'RefuelingStore', return_value=store):
            mod.listener_job(final, Channel(), Mock(is_set=lambda: False), leases,
                             Config(), DEVICE, 'unused', 'f', RUN, api)
        store.accept.assert_not_called()
        store.tick.assert_not_called()
        store.close.assert_called_once()
        api.PyAudio.return_value.open.return_value.close.assert_called_once()
        api.PyAudio.return_value.terminate.assert_called_once()
        self.assertEqual(final.events, [{'kind': 'error', 'code': 'lease_invalid'}])

    def test_fingerprint_pin_rejects_changed_profile_before_page_check(self):
        profile=SimpleNamespace(fingerprint='b'*64,templates=[SimpleNamespace(features=SimpleNamespace(shape=(32,200)))])
        reader=Mock()
        with patch.object(mod,'load_announcement_profile',return_value=profile):
            with self.assertRaises(mod.CheckFailure) as caught:
                mod._prepare(Config(),'unused',reader,True,'a'*64)
        self.assertEqual(caught.exception.code,'profile_changed')
        reader.assert_not_called()
        self.assertIsNone(self.listener.profile_fingerprint)
        with self.assertRaises(ValueError):
            self.listener.start(Config(),DEVICE,'unused',consent=True,expected_fingerprint='bad')

    def test_long_idle_renews_without_reads_or_ticks_and_resumes_new_epoch(self):
        leases=queue.Queue();leases.put(LiveLease.from_page(LIVE,RUN,'cabinet-1',99.,109.))
        final,progress,api,store=Channel(),Channel(),Mock(),Mock()
        self.now=100.
        stopped=[False];renewed=[False]
        def wait(delay):
            self.now+=.02
            if self.now>=105 and not renewed[0]:
                leases.put(LiveLease.from_page(LIVE,RUN,'cabinet-1',105.,115.))
                renewed[0]=True
        stop=SimpleNamespace(is_set=lambda:stopped[0],wait=wait)
        stream=api.PyAudio.return_value.open.return_value
        stream.get_read_available.side_effect=lambda:1024 if self.now>=112 else 0
        def read(count,**kwargs):
            self.assertGreaterEqual(self.now,112)
            stopped[0]=True
            return bytes(count*4) # RECEIVED digital silence, not synthetic idle samples.
        stream.read.side_effect=read
        cores=[]
        def make_core(*args,**kwargs):
            core=Mock();core.push.return_value=StreamBatch();core.settings=kwargs
            cores.append(core);return core
        with patch.object(mod.time,'monotonic',side_effect=lambda:self.now),patch.object(
                mod,'load_announcement_profile',return_value=SimpleNamespace(fingerprint='f')),patch.object(
                mod,'loopback_devices',return_value=[DEVICE]),patch.object(
                mod,'AnnouncementStream',side_effect=make_core),patch.object(mod,'RefuelingStore',return_value=store),patch.object(
                mod,'_progress',side_effect=lambda channel,event:channel.send(event)):
            mod.listener_job(final,progress,stop,leases,Config(),DEVICE,'unused','f',RUN,api)
        self.assertEqual(final.events,[{'kind':'finished','mode':'listener'}])
        stream.read.assert_called_once()
        self.assertGreater(len(progress.events),30)
        self.assertTrue(all(e['waiting_audio'] and e['frames']==0 for e in progress.events))
        store.tick.assert_not_called();store.accept.assert_not_called()
        self.assertEqual(store.mark_audio_gap.call_count,3) # startup, idle fence, finally
        self.assertEqual(len(cores),2)
        cores[0].discard.assert_called_once();cores[0].push.assert_not_called()
        self.assertNotEqual(cores[0].settings['run_id'],cores[1].settings['run_id'])
        args,kwargs=cores[1].push.call_args
        self.assertEqual(kwargs['lease'].issued_at,105.)
        self.assertEqual(kwargs['lease'].expires_at,115.)
        self.assertFalse(args[0].any())
        self.assertEqual(kwargs['start_frame'],0)

    def test_idle_stop_and_invalid_availability_are_bounded(self):
        for available,expected in ((0,'finished'),(-1,'audio_availability_failed'),('private','audio_availability_failed')):
            leases=queue.Queue();leases.put(LiveLease.from_page(LIVE,RUN,'cabinet-1',99.,109.))
            final,api,store=Channel(),Mock(),Mock()
            stopped=[False]
            stop=SimpleNamespace(is_set=lambda:stopped[0],wait=lambda delay:stopped.__setitem__(0,True))
            stream=api.PyAudio.return_value.open.return_value
            stream.get_read_available.return_value=available
            with patch.object(mod.time,'monotonic',return_value=100.),patch.object(
                    mod,'load_announcement_profile',return_value=SimpleNamespace(fingerprint='f')),patch.object(
                    mod,'loopback_devices',return_value=[DEVICE]),patch.object(mod,'AnnouncementStream'),patch.object(
                    mod,'RefuelingStore',return_value=store):
                mod.listener_job(final,Channel(),stop,leases,Config(),DEVICE,'unused','f',RUN,api)
            self.assertEqual(final.events[0].get('code',final.events[0]['kind']),expected)
            stream.read.assert_not_called();store.tick.assert_not_called()

    def test_one_hour_virtual_no_packets_keeps_watching_and_retention_without_fake_ticks(self):
        final,progress,api,store=Channel(),Channel(),Mock(),Mock()
        self.now=100.;stopped=[False]
        initial=LiveLease.from_page(LIVE,RUN,'cabinet-1',99.,109.)
        class Controls:
            issued=99.
            def get(inner,**kwargs):return initial
            def get_nowait(inner):
                issued=99.+int((self.now-99)//5)*5
                if issued<=inner.issued:raise queue.Empty
                inner.issued=issued
                return LiveLease.from_page(LIVE,RUN,'cabinet-1',issued,issued+10)
        def wait(delay):
            self.assertEqual(delay,.02)
            self.now+=1.
        stop=SimpleNamespace(is_set=lambda:stopped[0],wait=wait)
        stream=api.PyAudio.return_value.open.return_value
        stream.get_read_available.side_effect=lambda:1024 if self.now>=3700 else 0
        def read(count,**kwargs):
            stopped[0]=True
            return bytes(count*4)
        stream.read.side_effect=read
        core=Mock();core.push.return_value=StreamBatch()
        with patch.object(mod.time,'monotonic',side_effect=lambda:self.now),patch.object(
                mod,'load_announcement_profile',return_value=SimpleNamespace(fingerprint='f')),patch.object(
                mod,'loopback_devices',return_value=[DEVICE]),patch.object(
                mod,'AnnouncementStream',return_value=core),patch.object(mod,'RefuelingStore',return_value=store):
            mod.listener_job(final,progress,stop,Controls(),Config(),DEVICE,'unused','f',RUN,api)
        self.assertEqual(final.events,[{'kind':'finished','mode':'listener'}])
        self.assertEqual(store.cleanup.call_count,120)
        self.assertEqual(store.mark_audio_gap.call_count,3)
        stream.read.assert_called_once();store.tick.assert_not_called();store.accept.assert_not_called()
        self.assertEqual(len(progress.events),3600)

    def test_driver_failure_is_sanitized_and_closed(self):
        leases = queue.Queue()
        leases.put(LiveLease.from_page(LIVE, RUN, 'cabinet-1', 99., 109.))
        final, api = Channel(), Mock()
        api.PyAudio.side_effect = RuntimeError('secret / private device')
        with patch.object(mod.time, 'monotonic', return_value=100.), patch.object(
                mod, 'load_announcement_profile', return_value=SimpleNamespace(fingerprint='f')):
            mod.listener_job(final, Channel(), Mock(is_set=lambda: False), leases,
                             Config(), DEVICE, 'unused', 'f', RUN, api)
        self.assertEqual(final.events, [{'kind': 'error', 'code': 'audio_unavailable'}])
        self.assertTrue(final.closed)


if __name__ == '__main__':
    unittest.main()
