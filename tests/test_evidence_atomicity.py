"""An aborted new event must not leave its just-created images consuming quota."""
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest

from tests.test_rtsp import config_for,png
from smart_tapo.capture.rtsp import sign_rtsp_proof
from smart_tapo.evidence_budget import EvidenceStorageError
from smart_tapo.ram_frames import MemoryFrame
from smart_tapo.vision import Observation
from smart_tapo.worker import Worker


class EvidenceAtomicityTests(unittest.TestCase):
    def test_new_event_rollback_removes_only_its_new_images(self):
        with tempfile.TemporaryDirectory() as directory:
            content=png();config=replace(config_for(directory),max_image_storage_mb=len(content)/1_000_000)
            class Detector:
                def observe_image(self,image):return Observation('PRESENT')
            worker=Worker(config,detector=Detector())
            try:
                wall,received=time.time(),time.monotonic()
                proof=sign_rtsp_proof(config,content,'atomicity',1,received,wall,1.)
                envelope=MemoryFrame.create(1,config,content,'atomicity',1,wall,received,proof)
                with self.assertRaises(EvidenceStorageError):worker.process_memory(envelope,generation=1)
                self.assertEqual(worker.db.execute('SELECT count(*) FROM evidence').fetchone()[0],0)
                self.assertEqual(worker.db.execute('SELECT count(*) FROM events').fetchone()[0],0)
                self.assertEqual(list((Path(directory)/'events').rglob('*.png')),[],
                    'Only the images newly written by the aborted event must be rolled back')
            finally:worker.close()
