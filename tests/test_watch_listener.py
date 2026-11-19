"""Virtual-clock recovery tests, no SDK/audio/filesystem/DB operations."""
import unittest
from smart_tapo.config import Config
from smart_tapo.watch_listener import WatchListener

DEVICE=dict(index=1,name='chosen loopback',rate=48000,channels=2)


class Attempt:
    def __init__(self):
        self.is_running=False;self.events=[];self.calls=[];self.hold=False
        self.profile_fingerprint=None
    def start(self,*args,**kwargs):
        self.calls.append((args,kwargs));self.is_running=True
    def poll(self):
        events,self.events=self.events,[]
        return events
    def stop(self):
        self.calls.append('stop')
        if not self.hold:self.is_running=False
    def close(self):
        self.calls.append('close')
        if not self.hold:self.is_running=False
    def fail(self,code='audio_clock_discontinuity'):
        self.events.append({'kind':'error','code':code});self.is_running=self.hold


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.now=100.;self.created=[]
        def factory():
            attempt=Attempt();self.created.append(attempt);return attempt
        self.watch=WatchListener(factory=factory,clock=lambda:self.now)
        self.addCleanup(self.watch.close)
    def start(self):self.watch.start(Config(),DEVICE,'unused.json',consent=True)

    def test_no_auto_start_or_capture_on_construction(self):
        self.assertFalse(self.watch.is_running);self.assertEqual(self.watch.poll(),[])
        self.assertEqual(self.created,[])
        for value in (False,1,'yes'):
            with self.assertRaises(ValueError):self.watch.start(Config(),DEVICE,'unused',consent=value)
        self.assertEqual(self.created,[])

    def test_start_once_then_sixty_faults_recover_with_bounded_backoff(self):
        self.start()
        for index in range(60):
            attempt=self.created[-1];attempt.fail('profile_invalid')
            events=self.watch.poll()
            delay=(1,2,5,10,30)[min(index,4)]
            self.assertEqual(events[-1]['kind'],'listener_retry')
            self.assertEqual(events[-1]['retry_seconds'],delay)
            self.assertTrue(self.watch.is_running)
            previous=len(self.created)
            self.now+=delay-.01;self.watch.poll();self.assertEqual(len(self.created),previous)
            self.now+=.01;self.watch.poll();self.assertEqual(len(self.created),previous+1)
            self.assertIn('close',attempt.calls)
            args,kwargs=self.created[-1].calls[0]
            self.assertEqual(args[1],DEVICE);self.assertIs(kwargs['consent'],True)

    def test_recoverable_audio_fault_has_no_thirty_second_blind_cooldown(self):
        self.start()
        for index in range(60):
            attempt=self.created[-1];attempt.fail('audio_clock_discontinuity')
            events=self.watch.poll()
            retry=next(event for event in events if event['kind']=='listener_retry')
            delay=(0,.25,.5,1,2)[min(index,4)]
            self.assertEqual(retry['retry_seconds'],delay)
            self.now+=delay;self.watch.poll()
            self.assertEqual(len(self.created),index+2)
            self.assertIn('close',attempt.calls)

    def test_fast_retry_never_overlaps_unreaped_audio_and_stop_prevents_restart(self):
        self.start();attempt=self.created[-1];attempt.hold=True;attempt.fail('audio_clock_discontinuity')
        self.watch.poll();self.now+=100;self.watch.poll()
        self.assertEqual(len(self.created),1)
        self.watch.stop();attempt.is_running=False;self.watch.poll()
        self.now+=100;self.watch.poll();self.assertEqual(len(self.created),1)

    def test_stop_during_retry_cancels_all_future_attempts(self):
        self.start();self.created[-1].fail('profile_invalid');self.watch.poll()
        self.watch.stop();self.now+=1000
        self.watch.poll();self.assertEqual(len(self.created),1);self.assertFalse(self.watch.is_running)

    def test_pending_sdk_remains_owned_no_overlap_and_stop_wins(self):
        self.start();attempt=self.created[-1];attempt.hold=True;attempt.fail('live_check_unavailable')
        self.watch.poll();self.now+=100;self.watch.poll()
        self.assertEqual(len(self.created),1);self.assertTrue(self.watch.is_running)
        self.watch.stop();attempt.is_running=False;self.watch.poll();self.now+=100;self.watch.poll()
        self.assertEqual(len(self.created),1);self.assertFalse(self.watch.is_running)

    def test_recovery_freezes_device_config_and_first_validated_profile(self):
        config=Config();device=dict(DEVICE)
        self.watch.start(config,device,'unused',consent=True)
        device['name']='other';object.__setattr__(config,'camera_id','other')
        attempt=self.created[-1];attempt.profile_fingerprint='a'*64;attempt.fail()
        self.watch.poll();self.now+=1;self.watch.poll()
        args,kwargs=self.created[-1].calls[0]
        self.assertEqual(args[0].camera_id,'cabinet-1');self.assertEqual(args[1],DEVICE)
        self.assertEqual(kwargs['expected_fingerprint'],'a'*64)

    def test_playback_pauses_then_rechecks_without_navigation_or_manual_start(self):
        self.start();self.created[-1].fail('live_playback_detected')
        self.watch.poll();self.now+=1
        self.assertEqual(self.watch.poll()[-1]['kind'],'listener_rechecking')
        self.assertEqual(len(self.created),2)

    def test_close_never_rearms_and_healthy_attempt_resets_backoff(self):
        self.start();self.created[-1].fail('profile_invalid');self.watch.poll();self.now+=1;self.watch.poll()
        self.now+=31;self.created[-1].events=[{'kind':'listener_progress','waiting_audio':True}]
        self.watch.poll();self.created[-1].fail('profile_invalid');self.assertEqual(self.watch.poll()[-1]['retry_seconds'],1)
        self.watch.close();self.now+=100;self.watch.poll();self.assertEqual(len(self.created),2)

    def test_observation_counter_is_cumulative_not_new_human_payments(self):
        self.start();self.created[-1].events=[{'kind':'listener_progress','observations_total':2}]
        self.assertEqual(self.watch.poll()[0]['observations_total'],2)
        self.created[-1].fail();self.watch.poll();self.now+=1;self.watch.poll()
        self.created[-1].events=[{'kind':'listener_progress','observations_total':1}]
        self.assertEqual(self.watch.poll()[0]['observations_total'],3)
