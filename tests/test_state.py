from __future__ import annotations

import unittest
from tests import test_worker


class RecoveryTests(unittest.TestCase):
    setUp = test_worker.WorkerTests.setUp
    tearDown = test_worker.WorkerTests.tearDown
    make_worker = test_worker.WorkerTests.make_worker
    frame = test_worker.WorkerTests.frame

    def test_restart_candidate_incomplete_and_pending_ambiguous(self):
        self.frame(1)
        event = self.worker.active()['id']
        with self.worker.db:
            self.worker.db.execute("INSERT INTO notifications VALUES('crashed',?,?, 'pending')", (event, self.now))
        self.worker.close()
        self.worker = self.make_worker()
        self.assertIsNone(self.worker.active())
        row = self.worker.db.execute('SELECT * FROM events WHERE id=?', (event,)).fetchone()
        self.assertEqual(row['kind'], 'incomplete')
        self.assertEqual(self.worker.db.execute("SELECT status FROM notifications WHERE id='crashed'").fetchone()[0], 'ambiguous')
        self.now += 1000000
        self.worker.cleanup()
        self.assertGreater(self.worker.db.execute('SELECT count(*) FROM evidence').fetchone()[0], 0)
