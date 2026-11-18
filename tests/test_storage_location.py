"""Synthetic-only storage relocation and RAM desktop regressions."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from smart_tapo.config import Config
from smart_tapo.startup import bind, validate_binding
from tests import test_desktop_storage_budget as budget_tests


class StorageConsentTests(unittest.TestCase):
    def test_startup_consent_is_bound_to_data_root(self):
        config = Config(capture_backend='rtsp')
        config = replace(config, windows_startup_binding=bind(config))
        validate_binding(config)
        with self.assertRaises(ValueError):
            validate_binding(replace(config, data_dir=Path('other-data')))

    def test_ram_save_does_not_expand_quota_from_fps(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = budget_tests.SaveBudgetTests().make_app(Path(tmp))
            app.controller = SimpleNamespace(video_storage_mode='ram')
            with patch('smart_tapo.storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=100_000_000_000)):
                self.assertTrue(app.save_settings())
            self.assertEqual(app.config.max_image_storage_mb, 512)


class LocationAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'config';self.root.mkdir()
        self.old=self.root/'data';self.old.mkdir()

    def admit(self,value):
        from smart_tapo.storage_location import preflight_location
        return preflight_location(value,self.root,self.old,self.root/'calibration'/'empty.png')

    def test_relative_empty_destination_and_probe_cleanup(self):
        from smart_tapo.storage_location import normalize_location
        target=self.root/'new'
        with patch.object(Path,'stat',side_effect=AssertionError('candidate metadata')),patch.object(Path,'resolve',side_effect=AssertionError('candidate resolve')):
            self.assertEqual(normalize_location('new',self.root),target)
        self.assertEqual(self.admit('new'),target)
        self.assertEqual(list(target.iterdir()),[])

    def test_blank_volume_root_file_nonempty_and_overlap_rejected(self):
        file=self.root/'file';file.write_bytes(b'owned fixture')
        foreign=self.root/'foreign';foreign.mkdir();(foreign/'foreign-marker').write_bytes(b'unchanged')
        for value in ('',self.root.anchor,file,foreign,self.root,self.old/'nested',self.root/'calibration'/'nested'):
            with self.subTest(value=value),self.assertRaises(ValueError):self.admit(value)
        self.assertEqual((foreign/'foreign-marker').read_bytes(),b'unchanged')

    def test_unchanged_nonempty_root_is_allowed_and_untouched(self):
        fixture=self.old/'existing-marker';fixture.write_bytes(b'old history')
        self.assertEqual(self.admit(self.old),self.old)
        self.assertEqual(list(self.old.iterdir()),[fixture])
        self.assertEqual(fixture.read_bytes(),b'old history')

    def test_reparse_ancestor_checked_before_dotdot_normalization(self):
        from smart_tapo.storage_location import preflight_location
        original=Path.lstat
        link=self.root/'redirect'
        def metadata(path,*args,**kwargs):
            if path==link:return SimpleNamespace(st_mode=0o040755,st_file_attributes=0x400)
            return original(path,*args,**kwargs)
        with patch.object(Path,'lstat',metadata),self.assertRaises(ValueError):
            preflight_location(link/'..'/'new',self.root,self.old)
        self.assertFalse((self.root/'new').exists())

    def test_probe_collision_never_removes_someone_elses_file(self):
        fixture=self.old/'.smart-tapo-write-collision';fixture.write_bytes(b'not our probe')
        with patch('smart_tapo.storage_location.secrets.token_hex',return_value='collision'),self.assertRaises(FileExistsError):
            self.admit(self.old)
        self.assertEqual(fixture.read_bytes(),b'not our probe')

    def test_permission_failure_and_owned_probe_cleanup(self):
        with patch('smart_tapo.storage_location.os.open',side_effect=PermissionError),self.assertRaises(PermissionError):
            self.admit('denied')
        self.assertEqual(list((self.root/'denied').iterdir()),[])
        with patch('smart_tapo.storage_location.os.write',side_effect=OSError),self.assertRaises(OSError):
            self.admit('write-failed')
        self.assertEqual(list((self.root/'write-failed').iterdir()),[])


class RelocationLifecycleTests(unittest.TestCase):
    def make_app(self,root):
        app=budget_tests.SaveBudgetTests().make_app(root)
        app.controller=SimpleNamespace(video_storage_mode='ram')
        app.root=Mock();app.close=Mock()
        app.form={'data_dir':Mock()};app.form['data_dir'].get.return_value=str(root/'new-data')
        app.store.root=root;app.store.config_path=root/'config.json'
        app._candidate=Mock(return_value=replace(app.config,data_dir=root/'new-data'))
        return app

    def test_changed_root_persists_but_never_publishes_and_closes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);app=self.make_app(root);old=app.config
            app.config=replace(old,windows_startup_enabled=True,windows_startup_binding='old consent')
            old=app.config
            sentinel=root/'old-history';sentinel.write_bytes(b'untouched')
            app._candidate.return_value=replace(old,data_dir=root/'new-data')
            with patch('smart_tapo.desktop.messagebox.askyesno',return_value=True),patch('smart_tapo.startup.manage_entry') as entry:
                self.assertFalse(app.save_settings())  # saved, but unsafe for caller continuation
            saved=app.store.save.call_args.args[0]
            self.assertEqual(saved.data_dir,root/'new-data')
            self.assertFalse(saved.windows_startup_enabled);self.assertFalse(saved.windows_startup_audio)
            self.assertEqual(saved.windows_startup_binding,'')
            self.assertIs(app.config,old)
            self.assertEqual(saved.baseline_path,old.baseline_path)
            entry.assert_called_once_with(False,config=app.store.config_path)
            app.close.assert_called_once();app._refresh_calibration.assert_not_called()
            self.assertEqual(sentinel.read_bytes(),b'untouched')
            app.store.save_notify_key.assert_not_called()

    def test_implicit_save_cannot_start_or_capture_after_root_change(self):
        for action in ('capture_reference','start_role'):
            with self.subTest(action=action),tempfile.TemporaryDirectory() as tmp:
                app=self.make_app(Path(tmp));app._async=Mock()
                app.controller.start_capture=Mock();app.controller.start_worker=Mock()
                with patch('smart_tapo.desktop.messagebox.askyesno',return_value=True):
                    if action=='start_role':app.start_role('capture')
                    else:app.capture_reference()
                app.close.assert_called_once();app._async.assert_not_called()
                app.controller.start_capture.assert_not_called();app.controller.start_worker.assert_not_called()

    def test_cancel_picker_and_restart_confirmation_are_noops(self):
        from smart_tapo.desktop import DesktopApp
        with tempfile.TemporaryDirectory() as tmp:
            app=self.make_app(Path(tmp))
            with patch('smart_tapo.desktop.filedialog.askdirectory',return_value=''):
                app.choose_data_folder()
            app.form['data_dir'].set.assert_not_called()
            with patch('smart_tapo.desktop.messagebox.askyesno',return_value=False):
                self.assertFalse(app.save_settings())
            app.store.save.assert_not_called();app.close.assert_not_called()
            self.assertFalse((Path(tmp)/'new-data').exists())

    def test_picker_rechecks_idle_after_modal(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=self.make_app(Path(tmp));app._storage_busy=Mock(side_effect=[False,True])
            with patch('smart_tapo.desktop.filedialog.askdirectory',return_value=str(Path(tmp)/'new-data')):
                app.choose_data_folder()
            app.form['data_dir'].set.assert_not_called()

    def test_candidate_folder_normalization_has_no_metadata_or_probe_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);app=self.make_app(root);del app._candidate
            values={'data_dir':'relative-new','max_image_storage_mb':'128',
                    'stuck_consecutive_frames':str(app.config.stuck_consecutive_frames),
                    'deposit_history_hours':str(app.config.deposit_history_hours)}
            app.form={key:Mock() for key in values}
            for key,value in values.items():app.form[key].get.return_value=value
            app.notify=Mock();app.notify.get.return_value=False
            with patch.object(Path,'stat',side_effect=AssertionError('metadata')),patch.object(Path,'lstat',side_effect=AssertionError('metadata')),patch.object(Path,'resolve',side_effect=AssertionError('resolve')),patch('smart_tapo.storage_location.os.open',side_effect=AssertionError('probe')):
                candidate=app._candidate()
            self.assertEqual(candidate.data_dir,root/'relative-new')
            self.assertEqual(candidate.max_image_storage_mb,128)

    def test_confirmation_rechecks_idle_before_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=self.make_app(Path(tmp));app._storage_busy=Mock(side_effect=[False,False,True])
            with patch('smart_tapo.desktop.messagebox.askyesno',return_value=True):
                self.assertFalse(app.save_settings())
            app.store.save.assert_not_called();app.close.assert_not_called()
            self.assertFalse((Path(tmp)/'new-data').exists())

    def test_every_background_guard_blocks_root_change_before_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=self.make_app(Path(tmp))
            for name in ('startup_panel','audio_panel','offline_audio_panel','webhook_panel','refueling_panel','notify_test_panel','history_panel'):
                with self.subTest(panel=name):
                    setattr(app,name,SimpleNamespace(is_running=True,pending=True))
                    self.assertFalse(app.save_settings())
                    self.assertFalse((Path(tmp)/'new-data').exists())
                    delattr(app,name)
            app.store.save.assert_not_called()

    def test_entry_removal_failure_rolls_back_without_live_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=self.make_app(Path(tmp))
            app.config=replace(app.config,windows_startup_enabled=True)
            old=app.config
            with patch('smart_tapo.desktop.messagebox.askyesno',return_value=True),patch('smart_tapo.startup.manage_entry',side_effect=ValueError('foreign owner')):
                self.assertFalse(app.save_settings())
            self.assertIs(app.config,old);app.close.assert_not_called()
            self.assertEqual(app.store.save.call_args.args[0],old)

    def test_saved_root_survives_relaunch_without_moving_private_files(self):
        from smart_tapo.desktop_storage import DesktopStore
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);app=self.make_app(root)
            store=DesktopStore(root/'config.json');app.store=store
            private=root/'notify-api.dat';private.write_bytes(b'synthetic encrypted fixture')
            with patch('smart_tapo.desktop.messagebox.askyesno',return_value=True):
                self.assertFalse(app.save_settings())  # requires a new process
            loaded=store.load_or_create()
            self.assertEqual(loaded.data_dir,root/'new-data')
            self.assertEqual(loaded.baseline_path,app.config.baseline_path)
            self.assertEqual(private.read_bytes(),b'synthetic encrypted fixture')
            self.assertEqual(list((root/'new-data').iterdir()),[])

    def test_ram_budget_uses_new_volume_and_never_scans_raw_images(self):
        from smart_tapo.storage_budget import apply_event_storage_budget,budget_text
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'new-volume';target.mkdir()
            config=replace(Config(),data_dir=target,max_image_storage_mb=128,capture_interval_seconds=.05)
            with patch('smart_tapo.storage_budget.shutil.disk_usage',return_value=SimpleNamespace(free=10_000_000_000)) as disk,patch('smart_tapo.storage_budget.estimate_storage',side_effect=AssertionError('legacy scan')):
                actual,budget=apply_event_storage_budget(config)
            disk.assert_called_once_with(target)
            self.assertIs(actual,config);self.assertIn('RAM',budget_text(budget))

    def test_calibration_readiness_does_not_include_storage_root(self):
        from smart_tapo.desktop_calibration import mark_ready,is_ready
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);reference=root/'empty.png';reference.write_bytes(b'synthetic reference hash input')
            config=replace(Config(),baseline_path=reference,data_dir=root/'old')
            mark_ready(config,root)
            self.assertTrue(is_ready(replace(config,data_dir=root/'new'),root))
