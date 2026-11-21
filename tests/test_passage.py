"""Full-rate passage classification and its reconciliation with sampled ROI events."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import ImageDraw

from smart_tapo.capture.rtsp import MAX_PROOF_PASSAGES, PassageDelivery, sign_rtsp_proof
from smart_tapo.config import Rect
from smart_tapo.history import list_events
from smart_tapo.passage import RETAIN_COUNT, PassageTracker, parse_passages
from tests.test_rtsp_roi_occupancy import RoiPipeline

SIZE = (1920, 1080)
ROI = Rect(840, 400, 220, 200)


def scene(note=None):
    pixels = np.full((SIZE[1], SIZE[0], 3), 90, np.uint8)
    pixels[::40] = 60
    if note is not None:
        x, y, w, h = note
        pixels[y:y + h, x:x + w] = 230
    return pixels


def run(path, *, idle=10, fps=16.7):
    """path: note rectangles per frame (None = absent); returns verdicts."""
    tracker, t, result = PassageTracker(ROI, SIZE), 0., ()
    for note in [None] * idle + list(path) + [None] * idle:
        result = tracker.update(scene(note), t)
        t += 1 / fps
    return [p.verdict for p in result]


class PassageTrackerTests(unittest.TestCase):
    def test_note_pulled_back_where_it_emerged_is_retracted(self):
        hang = [(930, 430, 160, 160 + 20 * i) for i in range(5)] + [(930, 430, 160, 250 - 40 * i) for i in range(5)]
        self.assertEqual(run(hang), ['retracted'])

    def test_note_leaving_the_slot_is_passed(self):
        fall = [(930 - 60 * i, 430 + 25 * i, 160, 160) for i in range(8)]
        self.assertEqual(run(fall), ['passed'])

    def test_object_entering_from_zone_border_is_never_classified(self):
        walk = [(620 + 60 * i, 420, 160, 160) for i in range(8)]
        self.assertEqual(run(walk), [])

    def test_object_lingering_beyond_limit_is_never_classified(self):
        self.assertEqual(run([(930, 430, 160, 160)] * 80), [])

    def test_whole_zone_change_is_not_a_note(self):
        tracker = PassageTracker(ROI, SIZE)
        tracker.update(scene(), 0.)
        bright = np.full((SIZE[1], SIZE[0], 3), 250, np.uint8)
        for i in range(10):tracker.update(bright, .06 * (i + 1))
        for i in range(10):tracker.update(bright, 1 + .06 * i)
        self.assertEqual(tracker.passages(2.), ())

    def test_roi_outside_frame_is_rejected(self):
        with self.assertRaises(ValueError):PassageTracker(Rect(340, 265, 100, 110), (64, 48))

    def test_signed_field_decoding_is_strict(self):
        good = dict(id='a' * 32, verdict='passed', started=1., ended=1.5)
        self.assertEqual(len(parse_passages([good])), 1)
        for bad in ([dict(good, verdict='maybe')], [dict(good, ended=9.)], [dict(good, extra=1)],
                    [dict(good, id='x')], 'passed', [good] * (RETAIN_COUNT + 1)):
            self.assertEqual(parse_passages(bad), ())

    def test_proof_limit_matches_tracker_retention(self):
        self.assertEqual(MAX_PROOF_PASSAGES, RETAIN_COUNT)

    def test_verdict_is_retained_long_enough_for_a_slow_worker(self):
        tracker = PassageTracker(ROI, SIZE)
        fall = [(930 - 60 * i, 430 + 25 * i, 160, 160) for i in range(8)]
        t = 0.
        for note in [None] * 10 + fall + [None] * 10:
            tracker.update(scene(note), t)
            t += 1 / 16.7
        self.assertEqual([p.verdict for p in tracker.passages(t + 55)], ['passed'])
        self.assertEqual(tracker.passages(t + 61), ())


class PassageReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.pipe = RoiPipeline(self.temp.name);self.addCleanup(self.pipe.close)

    def frame(self, image, passages=()):
        with patch('tests.test_rtsp_roi_occupancy.sign_rtsp_proof',
                   lambda *args: sign_rtsp_proof(*args, passages)):
            self.pipe.frame(image)

    def events(self):
        return [tuple(r) for r in self.pipe.worker.db.execute('SELECT kind FROM events ORDER BY started')]

    def passage(self, verdict, started, ended):
        return dict(id=format(int(started * 1000), '032x'), verdict=verdict, started=started, ended=ended)

    def test_pulled_back_note_is_hidden_from_history(self):
        p = self.pipe
        p.frame(p.reference)
        self.frame(p.occupied());self.frame(p.occupied())
        started = p.worker.db.execute('SELECT started FROM events').fetchone()[0]
        verdict = self.passage('retracted', started - .2, started + 1.2)
        for _ in range(3):self.frame(p.reference, [verdict])
        self.assertEqual(self.events(), [('retracted',)])
        self.assertEqual(list_events(p.config, now=p.now).events, ())

    def test_pass_missed_by_sampled_frames_is_recorded_once(self):
        p = self.pipe
        p.frame(p.reference)
        verdict = self.passage('passed', p.now + .2, p.now + .8)
        for _ in range(3):self.frame(p.reference, [verdict])
        self.assertEqual(self.events(), [('normal_visual',)])
        row = p.worker.db.execute('SELECT * FROM events').fetchone()
        self.assertEqual((row['started'], row['closed']), (verdict['started'], verdict['ended']))
        self.assertEqual({r['role'] for r in p.worker.db.execute('SELECT role FROM evidence')}, {'first', 'first_full'})

    def test_pass_after_incomplete_event_is_confirmed_normal(self):
        p = self.pipe
        p.worker.config = replace(p.config, event_gap_tolerance_seconds=0)
        p.config = p.worker.config
        self.frame(p.occupied())
        started = p.worker.db.execute('SELECT started FROM events').fetchone()[0]
        unknown = p.reference.copy();ImageDraw.Draw(unknown).rectangle((0, 0, 1919, 1079), fill='gray')
        self.frame(unknown)
        self.assertEqual(self.events(), [('incomplete',)])
        self.frame(p.reference, [self.passage('passed', started - .1, started + .6)])
        self.assertEqual(self.events(), [('normal_visual',)])

    def test_retracted_verdict_never_hides_a_stuck_note(self):
        p = self.pipe
        p.frame(p.reference)
        for _ in range(10):self.frame(p.occupied())
        started = p.worker.db.execute('SELECT started FROM events').fetchone()[0]
        self.frame(p.occupied(), [self.passage('retracted', started - .2, started + .5)])
        self.assertEqual(self.events(), [('stuck',)])

    def test_note_still_present_after_retracted_verdict_can_become_stuck(self):
        p = self.pipe
        p.frame(p.reference)
        self.frame(p.occupied())
        started = p.worker.db.execute('SELECT started FROM events').fetchone()[0]
        verdict = self.passage('retracted', started - .2, started + .5)
        for _ in range(10):self.frame(p.occupied(), [verdict])
        self.assertEqual(self.events(), [('stuck',)])

    def test_stored_png_ram_frame_becomes_compact_identical_evidence(self):
        p = self.pipe
        pixels = cv2.cvtColor(np.asarray(p.occupied()), cv2.COLOR_RGB2BGR)
        stored = cv2.imencode('.png', pixels, [cv2.IMWRITE_PNG_COMPRESSION, 0,
                                               cv2.IMWRITE_PNG_FILTER, cv2.IMWRITE_PNG_FILTER_NONE])[1].tobytes()
        with p.worker.db:
            p.worker.snapshot('a' * 32, 'first_full', stored, full_frame=True)
        path = p.worker.db.execute("SELECT path FROM evidence WHERE role='first_full'").fetchone()[0]
        saved = Path(path).read_bytes()
        self.assertLess(len(saved), len(stored) / 4)
        self.assertTrue(np.array_equal(cv2.imdecode(np.frombuffer(saved, np.uint8), cv2.IMREAD_COLOR), pixels))

    def test_worker_warns_when_frames_stop_longer_than_verdict_retention(self):
        p = self.pipe
        p.frame(p.reference)
        with self.assertNoLogs('smart_tapo.worker', 'WARNING'):
            p.frame(p.reference)
        last = p.now
        p.now += 70; p.mono += 70
        with self.assertLogs('smart_tapo.worker', 'WARNING') as logs:
            p.frame(p.reference)
        record = next(r for r in logs.records if r.msg == 'Passage verdicts may be lost')
        self.assertEqual(record.passage_loss, dict(reason='worker_gap', since=last))


class PassageDeliveryTests(unittest.TestCase):
    passage = dict(id='b' * 32, verdict='passed', started=10., ended=10.5)

    def test_published_verdict_never_warns(self):
        delivery = PassageDelivery()
        with self.assertNoLogs('smart_tapo.capture.rtsp', 'WARNING'):
            delivery.observe((self.passage,))
            delivery.published((self.passage,))
            delivery.observe((self.passage,))
            delivery.observe(())
            delivery.reset()

    def test_verdict_expiring_before_any_published_frame_warns(self):
        delivery = PassageDelivery()
        delivery.observe((self.passage,))
        with self.assertLogs('smart_tapo.capture.rtsp', 'WARNING') as logs:
            delivery.observe(())
        self.assertEqual(logs.records[0].passage_loss, dict(reason='expired', since=10.))

    def test_reconnect_with_undelivered_verdict_warns(self):
        delivery = PassageDelivery()
        delivery.observe((self.passage,))
        with self.assertLogs('smart_tapo.capture.rtsp', 'WARNING') as logs:
            delivery.reset()
        self.assertEqual(logs.records[0].passage_loss, dict(reason='reconnect', since=10.))


if __name__ == '__main__':unittest.main()
