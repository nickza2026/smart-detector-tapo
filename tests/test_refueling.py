"""Confirmed business transitions only: synthetic times, no devices/DB/network."""
from dataclasses import FrozenInstanceError
import copy
import json
import unittest

from smart_tapo.refueling import (ConfirmedAnnouncement,RefuelingEngine,MISSING_END,REPEATED_READY)


def ready(at):return ConfirmedAnnouncement('paid_ready',at,at+6)
def thanks(at):return ConfirmedAnnouncement('end',at,at+2)


class RefuelingTests(unittest.TestCase):
    def setUp(self):
        self.ids=iter(('round-1','round-2','round-3'))
        self.engine=RefuelingEngine('cabinet-1',id_factory=lambda:next(self.ids))

    def test_first_ready_means_paid_round_and_thanks_finishes_it(self):
        started=self.engine.accept(ready(100)).updates[0]
        self.assertEqual(started.kind,'started')
        self.assertEqual(started.session.started_at,100)
        self.assertEqual(started.session.ready_count,1)
        self.assertFalse(started.session.has_issue)
        completed=self.engine.accept(thanks(180)).updates[0]
        self.assertEqual(completed.kind,'completed')
        self.assertEqual(completed.session.id,started.session.id)
        self.assertEqual(completed.session.ended_at,180)
        self.assertIsNone(self.engine.current)

    def test_repeated_ready_is_one_round_with_sticky_issue_and_count(self):
        original=self.engine.accept(ready(100)).updates[0]
        for at in (120,140,160):self.engine.accept(ready(at))
        session=self.engine.current
        self.assertEqual(session.id,original.session.id)
        self.assertEqual(session.started_at,100)
        self.assertEqual(session.last_ready_at,160)
        self.assertEqual(session.ready_count,4)
        self.assertEqual(session.repeated_ready_at,120)
        self.assertEqual(session.issue_codes,(REPEATED_READY,))
        self.assertIsNone(session.ended_at)
        ended=self.engine.accept(thanks(200)).updates[0].session
        self.assertTrue(ended.has_issue)
        self.assertEqual(ended.ready_count,4)

    def test_timeout_strictly_more_than_five_minutes_from_latest_ready(self):
        self.engine.accept(ready(100))
        self.engine.accept(ready(350))
        self.assertEqual(self.engine.tick(401),())  # Not from first ready.
        self.assertEqual(self.engine.tick(650),())  # Exactly five, not over.
        update=self.engine.tick(650.01)[0]
        self.assertEqual(update.kind,'completion_missing')
        self.assertIsNone(update.session.ended_at)
        self.assertEqual(update.session.missing_end_at,650.01)
        self.assertEqual(set(update.session.issue_codes),{MISSING_END,REPEATED_READY})

    def test_background_ticks_do_not_reset_last_ready_or_spam_issue_updates(self):
        self.engine.accept(ready(100))
        for at in range(101,401):self.assertEqual(self.engine.tick(at),())
        problem=self.engine.tick(401)[0]
        self.assertEqual(problem.session.last_ready_at,100)
        for at in (402,500,1000):self.assertEqual(self.engine.tick(at),())
        self.assertEqual(self.engine.current.missing_end_at,401)

    def test_late_thanks_closes_actual_round_but_keeps_timeout_history(self):
        self.engine.accept(ready(100))
        self.engine.tick(401)
        completed=self.engine.accept(thanks(500)).updates[-1].session
        self.assertEqual(completed.ended_at,500)
        self.assertEqual(completed.missing_end_at,401)
        self.assertEqual(completed.issue_codes,(MISSING_END,))
        self.assertEqual(completed.state,'completed')

    def test_late_ready_marks_elapsed_silence_before_reset_and_stays_same_round(self):
        self.engine.accept(ready(100))
        updates=self.engine.accept(ready(450)).updates
        self.assertEqual([u.kind for u in updates],['completion_missing','ready_repeated'])
        self.assertEqual(updates[-1].session.id,'round-1')
        self.assertEqual(updates[-1].session.last_ready_at,450)
        self.assertEqual(updates[-1].session.ready_count,2)
        self.assertIsNone(updates[-1].session.ended_at)

    def test_late_completion_checks_missing_timeout_even_without_prior_tick(self):
        self.engine.accept(ready(100))
        updates=self.engine.accept(thanks(401)).updates
        self.assertEqual([u.kind for u in updates],['completion_missing','completed'])
        self.assertIsNone(updates[0].session.ended_at)
        self.assertEqual(updates[1].session.ended_at,401)

    def test_rolling_window_duplicates_and_stale_audio_do_not_count_as_repeats(self):
        self.engine.accept(ready(100))
        for observation in (ready(100),ready(100.02),ready(99)):
            result=self.engine.accept(observation)
            self.assertFalse(result.updates)
            self.assertIsNotNone(result.ignored_reason)
        self.assertEqual(self.engine.current.ready_count,1)
        self.assertFalse(self.engine.current.has_issue)
        self.assertEqual(self.engine.current.last_ready_at,100)

    def test_unknown_or_orphan_audio_never_fabricates_payment_round(self):
        result=self.engine.accept(thanks(100))
        self.assertEqual(result.ignored_reason,'completion_without_start')
        self.assertIsNone(self.engine.current)
        with self.assertRaises(ValueError):ConfirmedAnnouncement('car_noise',110,111)
        self.assertEqual(self.engine.tick(1000),())

    def test_next_customer_only_gets_new_round_after_thanks(self):
        self.engine.accept(ready(100));self.engine.accept(thanks(200))
        next_round=self.engine.accept(ready(250)).updates[0].session
        self.assertEqual(next_round.id,'round-2')
        self.assertFalse(next_round.has_issue)
        self.assertEqual(next_round.ready_count,1)

    def test_updates_are_immutable_distinct_revision_snapshots_for_future_outbox(self):
        first=self.engine.accept(ready(100)).updates[0]
        repeated=self.engine.accept(ready(200)).updates[0]
        self.assertEqual(first.session.ready_count,1)
        self.assertFalse(first.session.has_issue)
        self.assertNotEqual(first.event_key,repeated.event_key)
        with self.assertRaises(FrozenInstanceError):first.session.ready_count=99

    def test_invalid_input_does_not_mutate_current_round(self):
        self.engine.accept(ready(100));before=self.engine.current
        for value in (-1,float('nan'),float('inf'),True):
            with self.assertRaises(ValueError):self.engine.tick(value)
        with self.assertRaises(ValueError):self.engine.accept({'kind':'paid_ready'})
        with self.assertRaises(ValueError):ConfirmedAnnouncement('paid_ready',200,199)
        self.assertIs(self.engine.current,before)

    def test_old_audio_after_processed_timeline_does_not_open_or_reset_a_round(self):
        self.engine.tick(1000)
        self.assertFalse(self.engine.accept(ready(100)).updates)
        self.assertIsNone(self.engine.current)
        self.engine.accept(ready(1100))
        self.engine.tick(1300)
        self.assertFalse(self.engine.accept(ready(1200)).updates)
        self.assertEqual(self.engine.current.last_ready_at,1100)
        self.engine.tick(1000)  # A backwards clock must not rewind the watermark.
        self.assertFalse(self.engine.accept(ready(1200)).updates)
        self.assertEqual(self.engine.current.ready_count,1)

    def test_adjacent_real_repeat_with_small_boundary_overlap_is_not_duplicate(self):
        self.engine.accept(ready(100))
        # 50ms overlap from acoustic-frame boundary jitter, not the same phrase.
        self.engine.accept(ready(105.95))
        self.assertEqual(self.engine.current.ready_count,2)
        self.assertEqual(self.engine.current.issue_codes,(REPEATED_READY,))

    def test_checkpoint_round_trip_preserves_dedupe_timer_and_late_completion(self):
        self.engine.accept(ready(100));self.engine.accept(ready(200));self.engine.tick(501)
        snapshot=json.loads(json.dumps(self.engine.checkpoint()))
        restarted=RefuelingEngine('cabinet-1')
        restarted.restore_checkpoint(snapshot)
        self.assertEqual(restarted.checkpoint(),snapshot)
        self.assertFalse(restarted.accept(ready(200)).updates)
        completed=restarted.accept(thanks(600)).updates[-1].session
        self.assertEqual(completed.ready_count,2)
        self.assertEqual(set(completed.issue_codes),{REPEATED_READY,MISSING_END})
        self.assertEqual(completed.ended_at,600)

    def test_bad_checkpoint_is_rejected_without_mutating_current_state(self):
        self.engine.accept(ready(100));before=self.engine.checkpoint()
        changes=[('camera_id','other'),('schema_version',True),('processed_until',50)]
        for name,value in changes:
            bad=copy.deepcopy(before);bad[name]=value
            with self.assertRaises(ValueError):self.engine.restore_checkpoint(bad)
            self.assertEqual(self.engine.checkpoint(),before)
        for name,value in [('ended_at',120),('ready_count',True),('repeated_ready_at',120),('last_ready_at',999)]:
            bad=copy.deepcopy(before);bad['current'][name]=value
            with self.assertRaises(ValueError):self.engine.restore_checkpoint(bad)
            self.assertEqual(self.engine.checkpoint(),before)

    def test_empty_and_completed_checkpoints_restore_without_fabricating_round(self):
        other=RefuelingEngine('cabinet-1')
        other.restore_checkpoint(self.engine.checkpoint())
        self.engine.accept(ready(100));self.engine.accept(thanks(200))
        other.restore_checkpoint(self.engine.checkpoint())
        self.assertIsNone(other.current)
        self.assertFalse(other.accept(thanks(200)).updates)

    def test_checkpoint_returns_detached_data(self):
        self.engine.accept(ready(100))
        snapshot=self.engine.checkpoint();snapshot['current']['ready_count']=200
        self.assertEqual(self.engine.current.ready_count,1)
