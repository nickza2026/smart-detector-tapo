"""Synthetic report contracts only: no Tk, packaged process, keys or HTTP."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import smoke_packaged


def synthetic_report():
    # Versioned fixture for the current GUI/RTSP report shape, NOT execution
    # evidence. windows_dpapi_roundtrip belongs to desktop.smoke_gui's actual
    # Notify DesktopStore save/load path, not rtsp_av_smoke's fake delivery.
    return dict(
        fixture_version=1,
        gui_created=True, auto_config_created=True, worker_stopped=True,
        shadow_notification=True, calibration_ready=True,
        windows_dpapi_roundtrip=True, rtsp_three_distinct_pids=True,
        history_menu_created=True, history_event_visible=True,
        history_full_image=True, history_web_served=True,
        rtsp_audio_backend=True, rtsp_pcm_real_decoder=True,
        rtsp_audio_started_first=True, rtsp_audio_stopped_reaped=True,
        notify_test_fake_only=True, real_notifications=0,
        notify_history_gui=True, notify_history_both_outcomes=True,
        notify_history_redacted=True, notify_history_fake_only=True,
        webhook_observation_delivery_completed=True,
        real_windows_audio=False, real_adb=False, real_user_stream=False,
        external_delivery=False, webhook_real_http=False,
    )


class PackagedSmokeContractTests(unittest.TestCase):
    def test_complete_synthetic_report_passes(self):
        smoke_packaged.validate_smoke_report(synthetic_report())

    def test_missing_dpapi_roundtrip_fails(self):
        report = synthetic_report()
        del report['windows_dpapi_roundtrip']
        with self.assertRaisesRegex(RuntimeError, 'windows_dpapi_roundtrip'):
            smoke_packaged.validate_smoke_report(report)

    def test_missing_observation_ack_cannot_pass_frozen_smoke(self):
        report = synthetic_report()
        del report['webhook_observation_delivery_completed']
        with self.assertRaisesRegex(RuntimeError, 'webhook_observation_delivery_completed'):
            smoke_packaged.validate_smoke_report(report)

    def test_other_credential_or_delivery_evidence_cannot_replace_roundtrip(self):
        for substitute in ('webhook_scoped_dpapi', 'notify_test_fake_only',
                           'pushover_dpapi_roundtrip'):
            with self.subTest(substitute=substitute):
                report = synthetic_report()
                del report['windows_dpapi_roundtrip']
                report[substitute] = True
                with self.assertRaisesRegex(RuntimeError, 'windows_dpapi_roundtrip'):
                    smoke_packaged.validate_smoke_report(report)

    def test_roundtrip_requires_literal_true(self):
        for value in (False, None, 1, 'true', {}):
            with self.subTest(value=value):
                report = synthetic_report()
                report['windows_dpapi_roundtrip'] = value
                with self.assertRaisesRegex(RuntimeError, 'windows_dpapi_roundtrip'):
                    smoke_packaged.validate_smoke_report(report)

    def test_every_existing_positive_gate_remains_required(self):
        for key, value in synthetic_report().items():
            if value is not True:
                continue
            with self.subTest(key=key):
                report = synthetic_report()
                del report[key]
                with self.assertRaisesRegex(RuntimeError, key):
                    smoke_packaged.validate_smoke_report(report)

    def test_endpoint_flags_must_explicitly_deny_real_access(self):
        for key in ('real_windows_audio', 'real_adb', 'real_user_stream',
                    'external_delivery', 'webhook_real_http'):
            for value in (None, True, 0, 'false'):
                with self.subTest(key=key, value=value):
                    report = synthetic_report()
                    if value is None:
                        del report[key]
                    else:
                        report[key] = value
                    with self.assertRaisesRegex(RuntimeError, key):
                        smoke_packaged.validate_smoke_report(report)

    def test_real_notification_count_is_exact_integer_zero(self):
        for value in (1, -1, None, False, 0.0, '0'):
            with self.subTest(value=value):
                report = synthetic_report()
                report['real_notifications'] = value
                with self.assertRaisesRegex(RuntimeError, 'live notifications'):
                    smoke_packaged.validate_smoke_report(report)

    def test_non_object_report_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'must be an object'):
            smoke_packaged.validate_smoke_report([])

    @unittest.skipUnless(smoke_packaged.os.name == 'nt', 'Windows packaged entry point')
    def test_entry_point_checks_actual_subprocess_report(self):
        # A fake subprocess writes the JSON exactly where main requested it.
        # RED: AV-only result cannot satisfy the credential gate.
        # GREEN: GUI-owned Notify roundtrip evidence completes the same report.
        for with_roundtrip in (False, True):
            with self.subTest(with_roundtrip=with_roundtrip):
                report = synthetic_report()
                if not with_roundtrip:
                    del report['windows_dpapi_roundtrip']
                with tempfile.TemporaryDirectory() as directory:
                    executable = Path(directory) / 'SmartTapo.exe'
                    executable.touch()

                    def fake_run(command, **kwargs):
                        self.assertEqual(command[0], str(executable.resolve()))
                        self.assertEqual(command[1], '--smoke-report')
                        self.assertIs(kwargs['check'], False)
                        Path(command[2]).write_text(json.dumps(report), encoding='utf-8')
                        return subprocess.CompletedProcess(command, 0, '', '')

                    with patch('sys.argv', ['smoke_packaged', '--dist', directory]), \
                            patch.object(smoke_packaged.subprocess, 'run', side_effect=fake_run) as run, \
                            patch('builtins.print'):
                        if with_roundtrip:
                            smoke_packaged.main()
                        else:
                            with self.assertRaisesRegex(RuntimeError, 'windows_dpapi_roundtrip'):
                                smoke_packaged.main()
                        run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
