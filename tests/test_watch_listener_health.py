import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from smart_tapo.config import Config
from smart_tapo.listener_health import ListenerHealthLog
from smart_tapo.watch_listener import WatchListener
from tests.test_watch_listener import Attempt,DEVICE


class WatchHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.now=100.
        self.attempts=[]
        def factory():
            attempt=Attempt()
            attempt._process=SimpleNamespace(pid=5000+len(self.attempts))
            self.attempts.append(attempt)
            return attempt
        self.watch=WatchListener(factory=factory,clock=lambda:self.now,
            health_factory=lambda directory:ListenerHealthLog(directory,
                clock=lambda:1000+self.now,monotonic=lambda:self.now))
        self.addCleanup(self.watch.close)

    def start(self):
        self.watch.start(Config(data_dir=self.root),DEVICE,'private-profile-path.json',
                         consent=True,expected_fingerprint='a'*64)

    def rows(self):
        return [json.loads(line) for line in (self.root/'logs/listener-health.jsonl').read_text().splitlines()]

    def test_construction_does_not_log_or_start(self):
        self.watch.poll()
        self.assertFalse((self.root/'logs').exists())
        self.assertEqual(self.attempts,[])

    def test_actual_watch_error_reap_retry_and_pid_are_persisted(self):
        self.start()
        self.attempts[0].events.append({'kind':'listener_progress','frames':3,'observations_total':0})
        self.watch.poll()
        self.now+=1
        self.attempts[0].fail('audio_clock_discontinuity')
        self.watch.poll()
        rows=self.rows()
        self.assertEqual([r['kind'] for r in rows].count('attempt_begin'),2)
        self.assertEqual([r['kind'] for r in rows].count('attempt_end'),1)
        self.assertTrue(any(r.get('code')=='audio_clock_discontinuity' for r in rows))
        self.assertTrue(any(r.get('child_pid')==5000 for r in rows))
        self.assertTrue(any(r.get('child_pid')==5001 for r in rows))
        self.assertTrue(self.watch.is_running)
        text=(self.root/'logs/listener-health.jsonl').read_text()
        self.assertNotIn('private-profile-path',text)
        self.assertNotIn('chosen loopback',text)
        self.assertFalse(self.watch.health_snapshot['io_error'])

    def test_attempt_end_waits_until_owned_child_reaped(self):
        self.start()
        self.attempts[0].hold=True
        self.attempts[0].fail()
        self.watch.poll()
        self.assertFalse(any(r['kind']=='attempt_end' for r in self.rows()))
        self.attempts[0].hold=False
        self.attempts[0].is_running=False
        self.watch.poll()
        self.assertEqual(sum(r['kind']=='attempt_end' for r in self.rows()),1)

    def test_logger_failure_is_latched_and_does_not_stop_watch(self):
        class Broken:
            def record_whitelisted(self,*args,**kwargs):raise OSError('private message')
            def snapshot(self):return {'io_error':False}
            def close(self):pass
        self.watch._health_factory=lambda directory:Broken()
        self.start()
        self.attempts[0].events.append({'kind':'listener_progress','frames':2})
        self.assertTrue(self.watch.poll())
        self.assertTrue(self.watch.is_running)
        self.assertTrue(self.watch.health_snapshot['io_error'])
