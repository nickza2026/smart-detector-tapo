"""Navigation invalidates audio proof even while acoustic analysis is in flight."""
from dataclasses import replace
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from smart_tapo import continuous_listener as mod
from smart_tapo.announcement_stream import LiveLease,StreamBatch,StreamObservation
from smart_tapo.capture.page_mode import PageMode,PROFILE
from smart_tapo.config import Config
from tests.test_continuous_listener import Channel,DEVICE,RUN


class AudioNavigationTests(unittest.TestCase):
    def test_navigation_during_analysis_never_commits_old_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            config=replace(Config(),data_dir=Path(directory),adb_serial='synthetic-device')
            leases=queue.Queue();leases.put(LiveLease.from_page(PageMode('live','synthetic',PROFILE),RUN,config.camera_id,99.,109.))
            state=SimpleNamespace(generation='initial',recovering=False,changed_monotonic=0.)
            final,api,store=Channel(),Mock(),Mock()
            stream=api.PyAudio.return_value.open.return_value
            stream.get_read_available.return_value=1024
            stream.read.return_value=bytes(1024*4)
            core=Mock()
            def analyze(*args,**kwargs):
                state.generation='b'*32;state.changed_monotonic=100.
                return StreamBatch((StreamObservation('old-audio','must-not-commit'),),123.)
            core.push.side_effect=analyze
            stop=SimpleNamespace(is_set=lambda:core.push.call_count>1)
            with patch.object(mod.time,'monotonic',return_value=100.),patch.object(
                    mod,'load_announcement_profile',return_value=SimpleNamespace(fingerprint='f')),patch.object(
                    mod,'loopback_devices',return_value=[DEVICE]),patch.object(
                    mod,'AnnouncementStream',return_value=core),patch.object(
                    mod,'RefuelingStore',return_value=store),patch.object(
                    mod,'navigation_snapshot',return_value=state,create=True):
                mod.listener_job(final,Channel(),stop,leases,config,DEVICE,'unused','f',RUN,api)
            store.accept.assert_not_called()
            store.tick.assert_not_called()
            self.assertGreaterEqual(store.mark_audio_gap.call_count,2)
            self.assertEqual(final.events[0]['code'],'live_navigation_changed')

    def test_check_rejects_generation_changed_without_fresh_matching_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            config=replace(Config(),data_dir=Path(directory),adb_serial='synthetic-device')
            state=SimpleNamespace(generation='initial',recovering=False,changed_monotonic=0.)
            def read(config):
                state.generation='c'*32
                return PageMode('live','synthetic',PROFILE)
            # Return a new immutable-style snapshot per read, not a shared object.
            def snapshot(config):return SimpleNamespace(**vars(state))
            with patch.object(mod,'navigation_snapshot',side_effect=snapshot,create=True):
                with self.assertRaises(mod.CheckFailure) as caught:
                    mod._prepare(config,'unused',read,False)
            self.assertEqual(caught.exception.code,'live_navigation_changed')


if __name__=='__main__':unittest.main()
