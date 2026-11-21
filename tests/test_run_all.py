import unittest
from contextlib import ExitStack
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from smart_tapo.desktop_run_all import run_all


class Var:
    def __init__(self, value=''):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class RunAllTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        config = NS(capture_backend='rtsp', notify_enabled=False, camera_id='synthetic',
                    data_dir='unused-owned-fake', validate=lambda: None)
        self.running = set()
        def start(role, *args):
            self.events.append(role)
            self.running.add(role)
            return True
        self.controller = NS(is_running=lambda r: r in self.running,
            start_capture=lambda *a: start('capture'), start_worker=lambda *a: start('worker'),
            stop=lambda role: (self.events.append('stop-' + role), self.running.discard(role)))
        self.listener = NS(is_running=False, profile_path=Var('synthetic-profile'),
            prepared_manifest='synthetic-kit', stop=lambda: self.events.append('stop-audio'),
            start_bound=lambda *a, **k: self.events.append('audio'))
        self.plan = NS(active=False)
        self.panel = NS(prepare_start=Mock(return_value=self.plan), validate_prepared=Mock(),
            start_prepared=lambda p: self.events.append('api'), stop=lambda: self.events.append('stop-api'))
        self.app = NS(config=config, closing=False, busy=False, _candidate=lambda: config,
            _calibration_pending=lambda: False, store=NS(root='unused'),
            offline_audio_panel=NS(is_running=False), startup_panel=NS(is_running=False),
            notify_key=Var(), notify_base=Var(), _health_credentials=('', ''), status=Var(),
            controller=self.controller, audio_panel=NS(listener_panel=self.listener), webhook_panel=self.panel)
        self.stack = ExitStack()
        for name, value in [('is_ready', True), ('find_spec', object()),
                            ('profile_digest', 'digest'), ('load_announcement_profile', NS(fingerprint='fingerprint'))]:
            self.stack.enter_context(patch('smart_tapo.desktop_run_all.' + name, return_value=value))
        self.addCleanup(self.stack.close)

    def test_one_confirmation_api_last(self):
        confirm = Mock(return_value=True)
        self.assertTrue(run_all(self.app, confirm=confirm))
        self.assertEqual(self.events, ['capture', 'worker', 'audio', 'api'])
        confirm.assert_called_once()

    def test_managed_old_prepared_profile_upgrade_is_after_one_consent(self):
        with patch('smart_tapo.desktop_run_all.needs_prepared_upgrade',return_value=True,create=True), \
                patch('smart_tapo.desktop_run_all.prepare_profile',return_value='prepared-v2') as prepare:
            ask=Mock(return_value=True)
            self.assertTrue(run_all(self.app,confirm=ask),self.app.status.get())
            ask.assert_called_once()
            self.assertIn('โปรไฟล์',ask.call_args.args[0])
            prepare.assert_called_once()
            self.assertEqual(self.listener.profile_path.get(),'prepared-v2')
            self.assertEqual(self.events,['capture','worker','audio','api'])

    def test_declining_upgrade_does_not_copy_or_change_selected_profile(self):
        with patch('smart_tapo.desktop_run_all.needs_prepared_upgrade',return_value=True,create=True), \
                patch('smart_tapo.desktop_run_all.prepare_profile') as prepare:
            self.assertFalse(run_all(self.app,confirm=lambda _:False))
            prepare.assert_not_called()
            self.assertEqual(self.listener.profile_path.get(),'synthetic-profile')
            self.assertEqual(self.events,[])

    def test_unknown_active_role_fails_closed(self):
        self.running.add('capture')
        self.assertFalse(run_all(self.app, confirm=Mock()))
        self.assertEqual(self.events, [])

    def test_all_matching_running_no_confirmation(self):
        self.running.update(('capture', 'worker'))
        self.app._run_all_role_configs = dict.fromkeys(('capture', 'worker'), self.app.config)
        self.listener.is_running = self.listener._watching = True
        self.listener._watch_config = self.app.config
        self.listener._watch_path = 'synthetic-profile'
        self.listener._watch_digest = 'digest'
        self.listener._watch_fingerprint = 'fingerprint'
        self.plan.active = True
        confirm = Mock()
        self.assertTrue(run_all(self.app, confirm=confirm))
        confirm.assert_not_called()
        self.assertEqual(self.events, [])

    def test_decline_starts_nothing(self):
        self.assertFalse(run_all(self.app, confirm=lambda _: False))
        self.assertEqual(self.events, [])

    def test_preflight_failure_no_confirm_or_start(self):
        for field in ('busy', 'closing'):
            setattr(self.app, field, True)
            confirm = Mock()
            self.assertFalse(run_all(self.app, confirm=confirm))
            confirm.assert_not_called()
            setattr(self.app, field, False)
        self.assertEqual(self.events, [])

    def test_changes_during_modal_cancel(self):
        for change in (lambda: setattr(self.app, 'closing', True),
                       lambda: setattr(self.app, '_run_all_generation', 1),
                       lambda: self.listener.profile_path.set('changed')):
            self.app.closing = False
            self.app._run_all_generation = 0
            self.listener.profile_path.set('synthetic-profile')
            def confirm(_):
                change()
                return True
            self.assertFalse(run_all(self.app, confirm=confirm))
        self.assertEqual(self.events, [])

    def test_owned_rollback_preserves_existing_capture(self):
        self.running.add('capture')
        self.app._run_all_role_configs = {'capture': self.app.config}
        self.listener.start_bound = Mock(side_effect=RuntimeError('secret-source'))
        self.assertFalse(run_all(self.app, confirm=lambda _: True))
        self.assertEqual(self.events, ['worker', 'stop-audio', 'stop-worker'])
        self.assertIn('capture', self.running)
        self.assertNotIn('secret-source', self.app.status.get())

    def test_failed_process_return_prevents_api(self):
        self.controller.start_worker = lambda *a: False
        self.assertFalse(run_all(self.app, confirm=lambda _: True))
        self.assertEqual(self.events, ['capture', 'stop-worker', 'stop-capture'])

    def test_api_partial_failure_cleanup(self):
        self.panel.start_prepared = Mock(side_effect=RuntimeError('secret-key'))
        self.assertFalse(run_all(self.app, confirm=lambda _: True))
        self.assertEqual(self.events[-4:], ['stop-api', 'stop-audio', 'stop-worker', 'stop-capture'])
        self.assertNotIn('secret-key', self.app.status.get())

    def test_double_click_is_ignored(self):
        def confirm(_):
            self.assertFalse(run_all(self.app, confirm=Mock()))
            return True
        self.assertTrue(run_all(self.app, confirm=confirm))
        self.assertEqual(self.events, ['capture', 'worker', 'audio', 'api'])

    def test_default_profile_import_only_after_consent(self):
        self.listener.profile_path.set('')
        with patch('smart_tapo.desktop_run_all.prepare_profile', return_value='imported') as prepare:
            self.assertFalse(run_all(self.app, confirm=lambda _: False))
            prepare.assert_not_called()
            self.assertTrue(run_all(self.app, confirm=lambda _: True))
            prepare.assert_called_once_with(self.app.config.data_dir, 'synthetic', 'synthetic-kit')
            self.assertEqual(self.listener.profile_path.get(), 'imported')

    def test_explicit_bad_profile_never_falls_back(self):
        with patch('smart_tapo.desktop_run_all.load_announcement_profile', side_effect=ValueError('secret-path')), \
                patch('smart_tapo.desktop_run_all.prepare_profile') as prepare:
            self.assertFalse(run_all(self.app, confirm=Mock()))
            prepare.assert_not_called()
        self.assertEqual(self.events, [])
        self.assertNotIn('secret-path', self.app.status.get())
