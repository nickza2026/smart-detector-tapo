import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from smart_tapo.listener_health import ListenerHealthLog


class ListenerHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 100.
        self.log = ListenerHealthLog(self.root, clock=lambda: self.now, monotonic=lambda: self.now)

    def rows(self):
        return [json.loads(line) for line in self.log.path.read_text().splitlines()]

    def test_pure_constructor_transitions_and_omitted_unknown_counters(self):
        self.assertFalse(self.log.path.parent.exists())
        for event in ({'kind': 'attempt_begin'}, {'kind': 'listener_progress', 'waiting_audio': True},
                      {'kind': 'error', 'code': 'audio_clock_discontinuity'},
                      {'kind': 'attempt_end'}, {'kind': 'listener_retry', 'retry_seconds': 2}):
            self.assertTrue(self.log.record_whitelisted(event, child_pid=123))
        rows = self.rows()
        self.assertEqual(len(rows), 5)
        self.assertNotIn('frames', rows[1])
        self.assertEqual(rows[2]['code'], 'audio_clock_discontinuity')
        self.assertEqual(rows[-1]['phase'], 'retry_scheduled')

    def test_rate_limit_transitions_and_clock_rollback(self):
        event = {'kind': 'listener_progress', 'frames': 10}
        self.assertTrue(self.log.record_whitelisted(event))
        self.now += 1
        self.assertFalse(self.log.record_whitelisted(event))
        self.now += 4
        self.assertTrue(self.log.record_whitelisted(event))
        self.now = 90
        self.assertFalse(self.log.record_whitelisted(event))
        self.now = 95
        self.assertTrue(self.log.record_whitelisted(event))
        self.assertTrue(self.log.record_whitelisted({**event, 'waiting_audio': True}))
        self.assertTrue(self.log.record_whitelisted(event, child_pid=124))

    def test_secrets_never_serialized_and_unknown_code_sanitized(self):
        secret = 'https://private.invalid/token=SECRET'
        self.assertTrue(self.log.record_whitelisted({'kind': 'error', 'code': secret},
            device_identity=secret, profile_identity=secret))
        text = self.log.path.read_text()
        self.assertNotIn(secret, text)
        self.assertEqual(self.rows()[0]['code'], 'unknown')
        self.assertEqual(len(self.rows()[0]['device_fingerprint']), 64)
        for event in ({'kind': 'state', 'url': secret}, {'kind': 'state', 'diagnostics': {'secret': secret}},
                      {'kind': 'listener_progress', 'frames': -1},
                      {'kind': 'listener_retry', 'retry_seconds': float('nan')},
                      {'kind': 'listener_progress', 'waiting_audio': 1}):
            self.assertFalse(self.log.record_whitelisted(event))
        self.assertEqual(len(self.rows()), 1)

    def test_identical_state_close_and_reopen(self):
        self.assertTrue(self.log.record_whitelisted({'kind': 'state'}))
        self.assertFalse(self.log.record_whitelisted({'kind': 'state'}))
        self.log.close()
        self.assertFalse(self.log.record_whitelisted({'kind': 'attempt_end'}))
        other = ListenerHealthLog(self.root)
        self.assertTrue(other.record_whitelisted({'kind': 'attempt_begin'}))
        self.assertNotEqual(other.run_id, self.log.run_id)
        self.assertEqual(len(self.rows()), 2)

    def test_rotation_bounded_and_foreign_file_untouched(self):
        self.log = ListenerHealthLog(self.root, max_bytes=600)
        for _ in range(20):
            self.assertTrue(self.log.record_whitelisted({'kind': 'attempt_begin'}))
        files = list(self.log.path.parent.iterdir())
        self.assertLessEqual(len(files), 3)
        self.assertTrue(all(p.stat().st_size <= 600 for p in files))
        foreign = self.log.path.with_name('do-not-touch.txt')
        foreign.write_text('secret')
        self.assertTrue(self.log.record_whitelisted({'kind': 'attempt_end'}))
        self.assertEqual(foreign.read_text(), 'secret')
        self.log.path.write_text('foreign data\n')
        self.assertFalse(self.log.record_whitelisted({'kind': 'attempt_end'}))
        self.assertEqual(self.log.path.read_text(), 'foreign data\n')
        self.assertTrue(self.log.snapshot()['io_error'])

    def test_io_and_quota_fail_closed_without_throwing(self):
        with patch.object(Path, 'mkdir', side_effect=OSError('disk full SECRET')):
            self.assertFalse(self.log.record_whitelisted({'kind': 'attempt_begin'}))
        self.assertTrue(self.log.snapshot()['io_error'])
        self.assertEqual(self.log.snapshot()['records_written'], 0)
        with patch.object(Path, 'mkdir', side_effect=AssertionError('must not retry I/O')):
            self.assertFalse(self.log.record_whitelisted({'kind': 'attempt_begin'}))
        tiny = ListenerHealthLog(self.root, max_bytes=256)
        self.assertFalse(tiny.record_whitelisted({'kind': 'error', 'code': 'audio_clock_discontinuity'},
            device_identity='device', profile_identity='profile'))
        self.assertTrue(tiny.snapshot()['io_error'])
        self.assertFalse(tiny.path.exists())

    def test_symlink_and_backup_symlink_guard(self):
        target = self.root / 'foreign'
        target.write_text('unchanged')
        self.log.path.parent.mkdir()
        try:
            self.log.path.symlink_to(target)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        self.assertFalse(self.log.record_whitelisted({'kind': 'attempt_begin'}))
        self.assertEqual(target.read_text(), 'unchanged')
        self.log.path.unlink()
        self.log.path.with_name(self.log.path.name + '.2').symlink_to(target)
        self.log = ListenerHealthLog(self.root)
        self.assertFalse(self.log.record_whitelisted({'kind': 'attempt_begin'}))
        self.assertEqual(target.read_text(), 'unchanged')

    def test_hardlink_guard_and_existing_oversize(self):
        self.log.path.parent.mkdir()
        target = self.root / 'foreign'
        target.write_text('unchanged')
        self.log.path.hardlink_to(target)
        self.assertFalse(self.log.record_whitelisted({'kind': 'attempt_begin'}))
        self.assertEqual(target.read_text(), 'unchanged')
        self.log.path.unlink()
        self.log.path.write_bytes(b'x' * (self.log.max_bytes + 1))
        self.log = ListenerHealthLog(self.root)
        self.assertFalse(self.log.record_whitelisted({'kind': 'attempt_begin'}))


if __name__ == '__main__':
    unittest.main()
