"""Owned temporary journal only; updater eligibility never repairs its schema."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import hashlib
import unittest
from smart_tapo.refueling_store import RefuelingStore
from smart_tapo.refueling import ConfirmedAnnouncement
from smart_tapo.update_business import business_state
from smart_tapo.updates import install_allowed


def app_for(directory):
    idle=lambda: SimpleNamespace(is_running=False)
    app = SimpleNamespace(_active=lambda:False, controller=SimpleNamespace(is_running=lambda role:False),
        audio_panel=idle(), busy=False, _calibration_pending=lambda:False, offline_audio_panel=idle(),
        webhook_panel=idle(), notify_test_panel=idle(), refueling_panel=idle(), history_panel=SimpleNamespace(pending=False),
        startup_panel=idle(), config=SimpleNamespace(data_dir=Path(directory), camera_id='cam1'))
    app._candidate = lambda: app.config
    return app


class UpdateBusinessTests(unittest.TestCase):
    def test_confirmed_end_resolves_gate_not_interrupted_history(self):
        with TemporaryDirectory() as directory:
            now=[1000.]
            app=app_for(directory)
            store=RefuelingStore(Path(directory)/'refueling','cam1',clock=lambda:now[0])
            try:
                store.accept('ready',ConfirmedAnnouncement('paid_ready',990.,992.))
                store.mark_audio_gap(at=1000.)
                self.assertFalse(install_allowed(business_state(app)))
                now[0]=1030.
                store.accept('end',ConfirmedAnnouncement('end',1020.,1022.))
                before=store._db.execute('SELECT data FROM sessions').fetchall()
                self.assertTrue(install_allowed(business_state(app)))
                self.assertEqual(before,store._db.execute('SELECT data FROM sessions').fetchall())
                self.assertIsNotNone(store.list_sessions()[0]['source_interrupted_at'])
                self.assertIsNone(store.list_sessions()[0]['ended_at'])
                store.mark_audio_gap(at=1040.)
                self.assertTrue(install_allowed(business_state(app)))
            finally:store.close()

    def test_new_unresolved_gap_not_hidden_by_earlier_end(self):
        with TemporaryDirectory() as directory:
            now=[1000.]
            app=app_for(directory)
            store=RefuelingStore(Path(directory)/'refueling','cam1',clock=lambda:now[0])
            try:
                store.accept('ready',ConfirmedAnnouncement('paid_ready',990.,992.))
                store.accept('end',ConfirmedAnnouncement('end',994.,996.))
                now[0]=1100.
                store.accept('new-ready',ConfirmedAnnouncement('paid_ready',1080.,1082.))
                store.mark_audio_gap(at=1100.)
                self.assertFalse(install_allowed(business_state(app)))
                # Corrupt/missing certainty must not be inferred from an old End.
                store._set_meta('awaiting_end_after_gap',False)
                self.assertFalse(install_allowed(business_state(app)))
            finally:store.close()

    def test_empty_idle_directory_is_not_created(self):
        with TemporaryDirectory() as directory:
            app=app_for(Path(directory)/'not-created')
            self.assertTrue(install_allowed(business_state(app)))
            self.assertFalse(app.config.data_dir.exists())

    def test_pending_boundary_and_unknown_checkpoint_always_block(self):
        for key,value in (('awaiting_end_after_gap',True),('awaiting_end_after_expiry',True),
                          ('awaiting_end_after_gap','false'),('checkpoint',None)):
            with self.subTest(key=key,value=value), TemporaryDirectory() as directory:
                app=app_for(directory)
                store=RefuelingStore(Path(directory)/'refueling','cam1',clock=lambda:1000.)
                try:
                    store._set_meta(key,value)
                    self.assertFalse(install_allowed(business_state(app)))
                finally:store.close()

    def test_open_interrupted_and_unknown_block_without_mutation(self):
        with TemporaryDirectory() as directory:
            app=app_for(directory)
            store=RefuelingStore(Path(directory)/'refueling','cam1',clock=lambda:1000.)
            try:
                store.accept('ready',ConfirmedAnnouncement('paid_ready',990.,992.))
                before=hashlib.sha256(store.db_path.read_bytes()).digest()
                state=business_state(app)
                self.assertTrue(state['open_round'])
                self.assertFalse(install_allowed(state))
                self.assertEqual(before,hashlib.sha256(store.db_path.read_bytes()).digest())
                store.mark_audio_gap()
                self.assertTrue(business_state(app)['interrupted_round'])
                app.config.camera_id='other'
                self.assertIsNone(business_state(app))
            finally:store.close()

    def test_any_running_or_unknown_flag_blocks(self):
        with TemporaryDirectory() as directory:
            app=app_for(directory)
            app.webhook_panel.is_running=True
            self.assertFalse(install_allowed(business_state(app)))
            app.webhook_panel.is_running=None
            self.assertIsNone(business_state(app))
