import io
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from PIL import Image
from smart_tapo.clock_epoch import current_clock_epoch
from smart_tapo.config import Config,Rect
from smart_tapo.frames import publish_frame,read_frame


class ClockEpochTests(unittest.TestCase):
    def test_kernel_epoch_is_stable_nonzero_and_not_a_raw_machine_identifier(self):
        value=current_clock_epoch()
        self.assertRegex(value,'^[0-9a-f]{64}$')
        self.assertNotEqual(value,'0'*64)
        self.assertEqual(value,current_clock_epoch())

    def test_legacy_missing_epoch_stays_unproven_not_retagged_this_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            config=replace(Config(),data_dir=Path(directory),expected_size=(4,4),video=Rect(0,0,4,4),
                           roi=Rect(0,0,2,2),clock_roi=Rect(0,0,1,1))
            data=io.BytesIO();Image.new('RGB',(4,4)).save(data,format='PNG')
            marker=publish_frame(config,data.getvalue(),'s',1,True)
            frame,_=read_frame(marker,config)
            self.assertEqual(frame.clock_epoch,current_clock_epoch())
            payload=json.loads(marker.read_text());payload.pop('clock_epoch')
            marker.write_text(json.dumps(payload))
            legacy,_=read_frame(marker,config)
            self.assertIsNone(legacy.clock_epoch)
            marker.write_text('[]')
            with self.assertRaises(ValueError):read_frame(marker,config)
