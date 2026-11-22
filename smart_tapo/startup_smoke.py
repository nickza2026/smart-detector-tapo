"""Cold-EXE proof using owned folders, fake work and metadata; no registration/audio."""
import json
from pathlib import Path
import threading
import time

from .startup import ENTRY,StartupCoordinator,device_identity,manage_entry,matching_device
from .listener_health import ListenerHealthLog


def smoke_startup(root,app):
    if app.config.adb_serial!='synthetic-device' or app._active() or app.audio_panel.is_running:
        raise RuntimeError('Startup smoke requires isolated stopped synthetic application')
    app.notebook.select(app.startup_panel)
    root.update()
    if not app.startup_panel.winfo_ismapped():raise RuntimeError('Startup tab missing')
    if app.startup_panel.enabled.get() or app.startup_panel.audio.get():
        raise RuntimeError('Startup consent defaulted on')
    folder=app.store.root/'synthetic Windows Startup'
    folder.mkdir()
    executable=app.store.root/'Synthetic installed app'/'SmartTapo.exe'
    executable.parent.mkdir()
    executable.write_bytes(b'not executable; never launched')
    config=executable.parent/'ค่าตั้ง config.json'
    config.write_text('{}',encoding='utf-8')
    foreign=folder/'keep-foreign.txt'
    foreign.write_text('preserve',encoding='utf-8')
    manage_entry(True,executable,config,folder=folder)
    entry=folder/ENTRY
    if '--windows-login' not in entry.read_text(encoding='utf-16'):
        raise RuntimeError('Owned startup entry missing command')
    manage_entry(False,config=config,folder=folder)
    if entry.exists() or foreign.read_text()!='preserve':raise RuntimeError('Startup ownership cleanup failed')
    entry.write_text('foreign entry',encoding='utf-16')
    try:manage_entry(True,executable,config,folder=folder)
    except ValueError:pass
    else:raise RuntimeError('Foreign startup entry overwritten')
    if entry.read_text(encoding='utf-16')!='foreign entry':raise RuntimeError('Foreign entry changed')

    entered,release=threading.Event(),threading.Event()
    def late(stop):
        entered.set()
        if not release.wait(3):raise RuntimeError('Synthetic release timeout')
        return True
    coordinator=StartupCoordinator(late)
    try:
        coordinator.start();coordinator.poll()
        if not entered.wait(2):raise RuntimeError('Synthetic startup did not begin')
        coordinator.stop();release.set();coordinator.thread.join(3)
        if coordinator.is_running or coordinator.poll() is not None or coordinator.enabled:
            raise RuntimeError('Late completion revived stopped startup')
    finally:
        coordinator.stop();release.set()
        if coordinator.thread is not None:coordinator.thread.join(3)
    device=dict(index=1,endpoint_id='{12345678-1234-1234-1234-123456789abc}',
                name='Synthetic dedicated output',rate=48000,channels=2)
    if matching_device(device_identity(device),[dict(device,index=91)])['index']!=91:
        raise RuntimeError('GUID identity incorrectly tied to volatile index')
    try:matching_device(device_identity(device),[dict(device,endpoint_id='{wrong}')])
    except ValueError:pass
    else:raise RuntimeError('Wrong endpoint accepted')
    health=ListenerHealthLog(app.store.root/'synthetic-health')
    try:
        if not health.record_whitelisted({'kind':'attempt_begin'},child_pid=1234,
                device_identity='synthetic-private-identity',profile_identity='a'*64):
            raise RuntimeError('Health metadata not persisted')
        if not health.record_whitelisted({'kind':'listener_retry','code':'audio_clock_discontinuity',
                                          'retry_seconds':.5}):
            raise RuntimeError('Health retry not persisted')
        text=health.path.read_text(encoding='utf-8')
        rows=[json.loads(line) for line in text.splitlines()]
        if len(rows)!=2 or 'synthetic-private-identity' in text:
            raise RuntimeError('Health metadata privacy or count mismatch')
    finally:health.close()
    return dict(startup_gui_rendered=True,startup_owned_fake_registration=True,
        startup_frozen_stop_fenced=True,startup_audio_guid_fenced=True,
        listener_health_frozen_persisted=True,startup_synthetic_only=True)
