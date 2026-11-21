"""Explicit offscreen smoke only; fake receiver, no microphone or transport."""
from pathlib import Path
import sqlite3
from tkinter import Toplevel

from .config import Config
from .desktop_rtsp_audio import RtspListenerPanel
from .storage_diagnostics import storage_failure_event


class _Receiver:
    profile_fingerprint = 'b' * 64
    def __init__(self):
        self.is_running, self.calls, self.events = False, [], []
    def start(self, config, device, path, **options):
        self.is_running = True
        self.calls.append((config, path, options))
    def poll(self):
        events, self.events = self.events, []
        return events
    def stop(self): self.is_running = False
    def close(self): self.is_running = False


def smoke_storage_recovery(root, directory):
    from .smoke_window import install
    install()
    directory = Path(directory)
    directory.mkdir()
    profile = directory / 'synthetic-profile.json'
    profile.write_text('{}', encoding='utf-8')
    config = Config(capture_backend='rtsp', rtsp_url='rtsp://127.0.0.1:1/never-opened', data_dir=directory)
    receiver = _Receiver()
    window = Toplevel(root)
    panel = RtspListenerPanel(window, lambda: config, runtime=receiver)
    panel.pack(fill='both', expand=True)
    now = [100.]
    panel.clock = lambda: now[0]
    try:
        window.geometry('1050x780')
        window.deiconify()
        window.update()
        if not window.winfo_ismapped(): raise RuntimeError('Recovery panel not rendered')
        panel.profile_path.set(str(profile))
        panel.consent.set(True)
        panel.start()
        receiver.events = [dict(kind='listener_progress', observations_total=3, ready_total=2, end_total=1)]
        panel.poll()
        error = sqlite3.OperationalError('PRIVATE must never be displayed')
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        receiver.is_running = False
        receiver.events = [dict(kind='error', **storage_failure_event(error, 'storage_open'))]
        panel.poll()
        if not panel.is_running or 'PRIVATE' in panel.status.get(): raise RuntimeError('Safe recovery watch missing')
        now[0] += 14
        panel.poll()
        if len(receiver.calls) != 1: raise RuntimeError('Recovery restarted before fifteen seconds')
        now[0] += 1
        panel.poll()
        if len(receiver.calls) != 2 or receiver.calls[-1][2]['expected_fingerprint'] != receiver.profile_fingerprint:
            raise RuntimeError('Recovery source/profile fence failed')
        receiver.events = [dict(kind='listener_progress', observations_total=1, ready_total=1, end_total=0)]
        panel.poll()
        if panel._announcements != 4: raise RuntimeError('Recovery counter lost or duplicated')
        if 'ไม่ทราบ' not in panel.health_status.get(): raise RuntimeError('Missing health was invented')
        receiver.is_running = False
        receiver.events = [dict(kind='error', code='storage_permission', stage='storage_commit')]
        panel.poll()
        panel.stop()
        now[0] += 600
        panel.poll()
        if len(receiver.calls) != 2 or panel.is_running: raise RuntimeError('Stop failed to cancel recovery')
        panel.consent.set(True)
        panel.start()
        receiver.is_running = False
        receiver.events = [dict(kind='error', code='storage_corrupt', stage='storage_open')]
        panel.poll()
        for attempt in range(1, 4):
            now[0] += 14
            panel.poll()
            if len(receiver.calls) != 2 + attempt:
                raise RuntimeError('Bounded recovery ran before fifteen seconds')
            now[0] += 1
            panel.poll()
            if len(receiver.calls) != 3 + attempt:
                raise RuntimeError('Bounded recovery missed scheduled attempt')
            if receiver.calls[-1][2]['expected_fingerprint'] != receiver.profile_fingerprint:
                raise RuntimeError('Bounded recovery lost profile fence')
            receiver.is_running = False
            receiver.events = [dict(kind='error', code='storage_corrupt', stage='storage_open')]
            panel.poll()
        now[0] += 600
        panel.poll()
        if len(receiver.calls) != 6 or panel.is_running:
            raise RuntimeError('Recovery exceeded three attempts')
        if panel.health_snapshot(now[0])['recovery_pending'] is not False:
            raise RuntimeError('Exhausted recovery still muted health')
        return dict(storage_classified_recovery_gui=True, storage_retry_counts_preserved_gui=True,
                    storage_retry_stop_fenced_gui=True, storage_permanent_error_stopped_gui=True,
                    storage_unknown_health_not_zero_gui=True,
                    storage_three_attempts_fifteen_seconds_gui=True)
    finally:
        panel.close()
        window.destroy()
