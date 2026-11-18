"""Owned smoke only: real mainloop Save-root exit, then reopen saved root."""
import json
from pathlib import Path
import tempfile
import sys
import traceback
from unittest.mock import patch


def _unexpected_main_error(*args, **kwargs):
    # main() intentionally redacts user-facing startup failures. This fixture
    # owns only synthetic config, so retain the cause rather than masking it.
    error=sys.exception()
    frames=traceback.extract_tb(error.__traceback__) if error is not None else []
    location=' > '.join(f'{Path(f.filename).name}:{f.lineno}' for f in frames[-3:])
    raise RuntimeError(f'Unexpected main error: {type(error).__name__}: {error} [{location}]') from error


def smoke_storage_restart():
    from . import desktop as ui
    real_app=ui.DesktopApp
    with tempfile.TemporaryDirectory(prefix='smart-tapo-storage-main-smoke-') as directory:
        base=Path(directory);config_path=base/'config.json';target=base/'new-dataset'
        apps=[];errors=[];sentinels=[];loaded=[]
        def construct(root,store):
            app=real_app(root,store);apps.append(app)
            loaded.append(app.config.data_dir)
            def expire():
                errors.append('Storage mainloop deadline')
                root.destroy()
            watchdog=root.after(8000,expire)
            root.bind('<Destroy>',lambda event:root.after_cancel(watchdog) if event.widget is root else None,add='+')
            def action():
                if app._storage_busy():
                    root.after(30,action);return
                try:
                    if len(apps)==1:
                        old=app.config.data_dir;old.mkdir(parents=True,exist_ok=True)
                        marker=old/'owned-old-data-marker.txt';marker.write_text('preserve',encoding='utf-8')
                        sentinels.append(marker)
                        app.form['data_dir'].set(str(target))
                        if app.save_settings() is not False or not app.closing:
                            raise RuntimeError('Root Save did not request orderly exit')
                        if app.config.data_dir!=old:raise RuntimeError('Live root switched before restart')
                    else:
                        if app.config.data_dir!=target:raise RuntimeError('Restart lost saved root')
                        if app._active() or app.audio_panel.is_running:raise RuntimeError('Restart auto-started source')
                        app.close()
                except Exception as error:
                    errors.append(type(error).__name__+': '+str(error))
                    root.destroy()
            root.after(30,action)
            return app
        for _ in range(2):
            with patch('sys.argv',['SmartTapo','--config',str(config_path)]), \
                 patch.object(ui,'DesktopApp',side_effect=construct), \
                 patch.object(ui.messagebox,'askyesno',return_value=True), \
                 patch.object(ui.messagebox,'showerror',side_effect=_unexpected_main_error), \
                 patch('smart_tapo.updates.ReleaseClient.check',return_value=None), \
                 patch('smart_tapo.startup.manage_entry',side_effect=AssertionError('No startup mutation')):
                if ui.main()!=0:raise RuntimeError('Storage main exit failed')
            if errors:raise RuntimeError('; '.join(errors))
            app=apps[-1]
            if not app.controller._transport_closed:raise RuntimeError('Restart IPC not closed')
            if app.controller._frame_manager._process.is_alive():raise RuntimeError('Restart Manager survived')
        saved=json.loads(config_path.read_text(encoding='utf-8'))
        if Path(saved['data_dir'])!=target or loaded[0]==target or loaded[1]!=target:
            raise RuntimeError('Saved/reopened root mismatch')
        if sentinels[0].read_text(encoding='utf-8')!='preserve':raise RuntimeError('Old data changed')
        return dict(storage_save_main_exit_gui=True,storage_saved_root_reopened_gui=True,
                    storage_old_data_preserved_gui=True,storage_restart_managers_reaped_gui=True)
