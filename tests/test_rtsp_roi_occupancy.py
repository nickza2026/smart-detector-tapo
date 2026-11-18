"""Real PNG/Detector/RTSP-proof/Worker, no bank classifier or live source."""
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image,ImageDraw,ImageEnhance

from smart_tapo.config import Config,Rect
from smart_tapo.capture.rtsp import rtsp_source_fingerprint,sign_rtsp_proof
from smart_tapo.frames import publish_frame,read_frame
from smart_tapo.worker import Worker
from tests.test_alignment_regression import textured_reference


class RoiPipeline:
    def __init__(self,directory,scene=None):
        self.root=Path(directory)
        self.config=replace(Config(),data_dir=self.root/'data',baseline_path=self.root/'empty.png',
            capture_backend='rtsp',expected_size=(1920,1080),video=Rect(0,0,1920,1080),
            roi=Rect(844,484,223,196),clock_roi=Rect(0,0,700,55),notify_enabled=False)
        self.config=replace(self.config,rtsp_source_binding=rtsp_source_fingerprint(self.config))
        self.reference=scene or textured_reference(self.config)
        self.reference.save(self.config.baseline_path)
        self.now=1700000000.;self.mono=1000.;self.sequence=0
        self.worker=Worker(self.config,clock=lambda:self.now,
            sender=lambda *_: (_ for _ in ()).throw(AssertionError('Real provider forbidden')))

    def frame(self,image,*,age=0,healthy=True,scan=False):
        self.sequence+=1;self.now+=1;self.mono+=1
        content=io.BytesIO();image.save(content,'PNG');data=content.getvalue()
        with patch('smart_tapo.capture.rtsp.time.monotonic',return_value=self.mono):
            proof=sign_rtsp_proof(self.config,data,'owned-roi',self.sequence,
                self.mono-age,self.now-age,100.+self.sequence)
            marker=publish_frame(self.config,data,'owned-roi',self.sequence,True,
                captured_at=self.now-age,captured_monotonic=self.mono-age,source_proof=proof,
                health='healthy' if healthy else 'timestamp_unconfirmed')
            if scan:self.worker.scan()
            else:self.worker.process(*read_frame(marker,self.config),marker)

    def occupied(self):
        image=self.reference.copy()
        ImageDraw.Draw(image).rectangle((875,510,1025,650),fill='white')
        return image

    def notifications(self):return self.worker.db.execute('SELECT * FROM notifications').fetchall()
    def close(self):self.worker.close()


class RtspRoiOccupancyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.pipe=RoiPipeline(self.temp.name);self.addCleanup(self.pipe.close)

    def test_generic_object_in_roi_alerts_only_after_ten_continuous_frames(self):
        p=self.pipe
        p.frame(p.reference)
        self.assertEqual(p.worker.count,0)
        for _ in range(9):p.frame(p.occupied())
        self.assertEqual(p.worker.count,9)
        self.assertEqual(len(p.notifications()),0)
        p.frame(p.occupied())
        self.assertEqual(p.worker.count,10)
        self.assertEqual([row['status'] for row in p.notifications()],['shadow'])
        event=p.worker.db.execute('SELECT * FROM events WHERE kind="stuck"').fetchone()
        self.assertIsNotNone(event)
        self.assertGreaterEqual(p.now-event['started'],9)
        p.frame(p.reference);p.frame(p.reference)
        self.assertEqual(p.worker.count,0)
        self.assertIsNotNone(p.worker.db.execute('SELECT closed FROM events WHERE id=?',(event['id'],)).fetchone()[0])

    def test_passing_object_and_outside_object_never_alert(self):
        p=self.pipe
        for _ in range(5):p.frame(p.occupied())
        p.frame(p.reference);p.frame(p.reference)
        outside=p.reference.copy();ImageDraw.Draw(outside).rectangle((1440,740,1610,900),fill='white')
        for _ in range(10):p.frame(outside)
        self.assertEqual(p.worker.count,0)
        self.assertEqual(len(p.notifications()),0)

    def test_unknown_stale_and_unhealthy_break_continuity_not_empty(self):
        p=self.pipe
        for _ in range(5):p.frame(p.occupied())
        p.frame(Image.new('RGB',p.config.expected_size,'gray'))
        self.assertEqual(p.worker.count,0)
        self.assertEqual(p.worker.empty,0)
        p.frame(p.occupied(),age=10)
        p.frame(p.occupied(),healthy=False)
        self.assertEqual(p.worker.count,0)
        for _ in range(9):p.frame(p.occupied())
        self.assertEqual(len(p.notifications()),0)
        p.frame(p.occupied())
        self.assertEqual(len(p.notifications()),1)

    def test_old_portrait_marker_cannot_reset_new_rtsp_proof_forever(self):
        p=self.pipe
        old=replace(p.config,expected_size=(720,1280))
        content=io.BytesIO();Image.new('RGB',old.expected_size,'gray').save(content,'PNG')
        marker=publish_frame(old,content.getvalue(),'old-portrait',1,True,
            captured_at=p.now-100,captured_monotonic=p.mono-100)
        for _ in range(10):p.frame(p.occupied(),scan=True)
        self.assertEqual(p.worker.count,10,'Unchanged rejected marker must not reset every new frame')
        self.assertEqual([row['status'] for row in p.notifications()],['shadow'])
        self.assertTrue(marker.is_file(),'Do not delete old evidence to make scan pass')

    def test_changed_rejected_marker_is_checked_and_resets_again(self):
        p=self.pipe
        old=replace(p.config,expected_size=(720,1280))
        content=io.BytesIO();Image.new('RGB',old.expected_size).save(content,'PNG')
        marker=publish_frame(old,content.getvalue(),'old-portrait',1,True,
            captured_at=p.now-100,captured_monotonic=p.mono-100)
        for _ in range(5):p.frame(p.occupied(),scan=True)
        self.assertEqual(p.worker.count,5)
        # Changed invalid content is new input, not covered by the rejection.
        marker.write_text('{}',encoding='utf-8')
        p.frame(p.occupied(),scan=True)
        self.assertEqual(p.worker.count,1)
        self.assertEqual(len(p.notifications()),0)

    def test_rejection_cache_eviction_does_not_reintroduce_poison(self):
        p=self.pipe
        folder=p.config.data_dir/'frames'/p.config.camera_id
        folder.mkdir(parents=True)
        for index in range(1025):(folder/f'invalid-{index}.ready.json').write_text('{}',encoding='utf-8')
        with patch('smart_tapo.worker.LOG.warning'):
            for _ in range(10):p.frame(p.occupied(),scan=True)
        self.assertEqual(p.worker.count,10)
        self.assertEqual(len(p.notifications()),1)
        self.assertLessEqual(len(p.worker._rejected_markers),1024)


if __name__=='__main__':unittest.main()
