"""Virtual-clock retention checks; isolated DB/files, never real histories."""
from pathlib import Path
import unittest

from smart_tapo.frames import publish_frame
from tests import test_worker


class StorageRetentionCleanupTests(unittest.TestCase):
    setUp=test_worker.WorkerTests.setUp
    tearDown=test_worker.WorkerTests.tearDown
    make_worker=test_worker.WorkerTests.make_worker
    frame=test_worker.WorkerTests.frame
    notifications=test_worker.WorkerTests.notifications

    def test_five_minute_raw_boundary_preserves_evidence_and_unprocessed_input(self):
        frames=[self.frame(index) for index in range(1,11)]
        first=frames[0]
        evidence=[Path(row[0]) for row in self.worker.db.execute('SELECT path FROM evidence')]
        self.assertTrue(evidence)
        pending=publish_frame(self.config,self.png,'not-processed',1,True,
            captured_at=first[0].captured_at-100)
        self.now=first[0].captured_at+300
        self.worker.cleanup()
        self.assertTrue(first[1].is_file(),'Exactly five minutes is not older than five minutes')
        self.now+=.001
        self.worker.cleanup()
        self.assertFalse(first[1].exists())
        self.assertFalse(first[2].exists())
        self.assertTrue(frames[-1][1].is_file())
        self.assertTrue(pending.is_file(),'Unprocessed input is not owned by cleanup')
        self.assertTrue(all(path.is_file() for path in evidence))

    def test_failed_notification_evidence_is_not_deleted_to_fit_raw_quota(self):
        for index in range(1,11):self.frame(index)
        self.frame(11,'EMPTY');self.frame(12,'EMPTY')
        evidence=[Path(row[0]) for row in self.worker.db.execute('SELECT path FROM evidence')]
        self.assertTrue(evidence)
        with self.worker.db:
            self.worker.db.execute("UPDATE notifications SET status='failed'")
        self.now+=8*86400
        self.worker.cleanup()
        self.assertTrue(all(path.is_file() for path in evidence))
        self.assertEqual(len(self.notifications()),1)


if __name__=='__main__':unittest.main()
