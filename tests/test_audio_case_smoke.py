"""Failed private-media acquisition must not reuse an earlier success report."""
import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from smart_tapo.audio_case_smoke import smoke_audio_cases

class AudioCaseSmokeTests(unittest.TestCase):
    def test_stale_success_report_is_preserved_but_never_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);output=root/'previous-success.json';stale=b'{"passed":true}'
            output.write_bytes(stale)
            with self.assertRaisesRegex(ValueError,'already_exists'):
                smoke_audio_cases(root/'missing-manifest.json',output,root/'owned-data')
            self.assertEqual(output.read_bytes(),stale)
            self.assertFalse((root/'owned-data').exists())

    def test_bad_input_hash_fails_before_decoder_store_or_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);clip=root/'not-valid.mp4';clip.write_bytes(b'not media')
            manifest=root/'cases.json';output=root/'fresh.json'
            manifest.write_text(json.dumps(dict(profile='explicit-test-only',cases=[dict(path=str(clip),sha256='0'*64,expected=['paid_ready','end'])])))
            profile=SimpleNamespace(templates=(SimpleNamespace(temporal_threshold=.3),))
            with patch('smart_tapo.announcement_profile.load_announcement_profile',return_value=profile), \
                    patch('smart_tapo.refueling_store.RefuelingStore') as store, \
                    self.assertRaisesRegex(ValueError,'hash_mismatch'):
                smoke_audio_cases(manifest,output,root/'owned-data')
            store.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual(clip.read_bytes(),b'not media')

if __name__=='__main__':unittest.main()
