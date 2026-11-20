"""Read-only install eligibility. Unknown state blocks; never stop business jobs."""
from pathlib import Path
import json
import sqlite3

from .refueling_store import _safe
from .refueling import RefuelingEngine, ConfirmedAnnouncement, _time
from .updates import BUSINESS_FLAGS


def business_state(app):
    try:
        if getattr(app,'_run_all_starting',False):
            return None
        if app._candidate() != app.config:
            return None
        detail = (app.busy, app._calibration_pending(), app.webhook_panel.is_running,
            app.notify_test_panel.is_running, app.refueling_panel.is_running,
            app.history_panel.pending, app.startup_panel.is_running)
        if any(type(v) is not bool for v in detail):
            return None
        flags = dict(workers=app._active(), capture=app.controller.is_running('capture'),
            listener=app.audio_panel.is_running, calibration=detail[0] or detail[1],
            offline=app.offline_audio_panel.is_running,
            webhook=detail[2] or detail[3],
            outstanding_writes=detail[4] or detail[5] or detail[6],
            open_round=False, interrupted_round=False)
        if set(flags) != set(BUSINESS_FLAGS) or any(type(v) is not bool for v in flags.values()):
            return None
        if any(flags.values()):
            return flags
        database = _safe(Path(app.config.data_dir) / 'refueling/refueling.sqlite3')
        if not database.exists():
            return flags
        # No DDL, migrations, cleanup, key reads, or history mutation.
        connection = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=.25)
        try:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            owner = connection.execute("SELECT value FROM meta WHERE key='camera_id'").fetchone()
            if owner is None or json.loads(owner[0]) != app.config.camera_id:
                return None
            checkpoint=connection.execute("SELECT value FROM meta WHERE key='checkpoint'").fetchone()
            if checkpoint is None:
                return None
            engine=RefuelingEngine(app.config.camera_id)
            engine.restore_checkpoint(json.loads(checkpoint[0]))
            flags['open_round']=engine.current is not None
            for key in ('awaiting_end_after_gap','awaiting_end_after_expiry'):
                row=connection.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
                value=json.loads(row[0]) if row is not None else False
                if type(value) is not bool:
                    return None
                flags['interrupted_round'] |= value
            interrupted_at=None
            for row in connection.execute('SELECT data FROM sessions WHERE ended_at IS NULL'):
                session = json.loads(row[0])
                if (type(session) is not dict or session.get('camera_id')!=app.config.camera_id
                        or session.get('ended_at') is not None):
                    return None
                if session.get('source_interrupted_at') is not None:
                    at=_time(session['source_interrupted_at'])
                    if at<_time(session['last_ready_at']):
                        return None
                    interrupted_at=at if interrupted_at is None else max(interrupted_at,at)
                else:
                    flags['open_round'] = True
            if interrupted_at is not None:
                # An actual accepted End establishes a later safe boundary; it
                # never fabricates completion of the interrupted history row.
                boundary=connection.execute("SELECT started_at,ended_at FROM observations "
                    "WHERE kind='end' AND (reason IS NULL OR reason='completion_without_start') "
                    'AND started_at>=? ORDER BY started_at DESC LIMIT 1',(interrupted_at,)).fetchone()
                if boundary is None:
                    flags['interrupted_round']=True
                else:
                    ConfirmedAnnouncement('end',boundary[0],boundary[1])
        finally:
            connection.close()
        return flags
    except Exception:
        return None
