"""Frozen-compatible synthetic PNG/window/Worker smoke; no devices or providers."""
import io
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from PIL import Image
from .config import Config, Rect
from .capture.clock_window import PendingWindow
from .capture.coordination import navigation_snapshot
from .frames import publish_frame, read_frame
from .worker import Worker


def smoke_clock_window():
    with tempfile.TemporaryDirectory(prefix='smart-tapo-clock-window-') as directory:
        config = replace(Config(),data_dir=Path(directory),expected_size=(4,4),
            video=Rect(0,0,4,4),roi=Rect(0,0,2,2),clock_roi=Rect(0,0,1,1),
            capture_interval_seconds=.5,clock_window_enabled=True,
            stuck_minimum_seconds=9,clear_minimum_seconds=1)
        state=navigation_snapshot(config)
        at,mono=time.time()-2,time.monotonic()-2
        window=PendingWindow(config)
        published=[]
        try:
            for index in range(5):
                image=Image.new('RGB',(4,4),'red' if index==1 else 'black')
                stream=io.BytesIO();image.save(stream,format='PNG')
                rows,proof=window.add(stream.getvalue(),b'new' if index==4 else b'old',
                    at+index*.5,mono+index*.5,index+1,'smoke-window',state.generation)
                if index<4 and rows:raise RuntimeError('Tentative window published early')
                if rows:
                    proof.update(ui_at=mono,ui_until=mono+10)
                    for path,sequence,captured,captured_mono in rows:
                        published.append(publish_frame(config,path.read_bytes(),'smoke-window',sequence,
                            True,captured_at=captured,captured_monotonic=captured_mono,window_proof=proof))
            if len(published)!=4:raise RuntimeError('Confirmed window did not yield four samples')
        finally:window.reset()
        def observe(path):
            with Image.open(path) as image:
                label='PRESENT' if image.getpixel((0,0))[0] else 'EMPTY'
            return SimpleNamespace(label=label)
        with_worker=Worker(config,SimpleNamespace(observe=observe))
        try:
            with_worker.scan()
            rows=with_worker.db.execute('SELECT kind,started FROM events').fetchall()
            if len(rows)!=1 or rows[0]['kind']!='normal_visual' or rows[0]['started']!=at+.5:
                raise RuntimeError('Short Sourcebank visual pass was lost or misclassified')
            if with_worker.db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0]:
                raise RuntimeError('Short visual pass alerted')
        finally:with_worker.close()
        frozen=PendingWindow(config)
        try:
            for index in range(25):
                rows,proof=frozen.add(stream.getvalue(),b'frozen',at+index*.5,
                    mono+index*.5,index+1,'smoke-freeze',state.generation)
                if rows or proof:raise RuntimeError('Frozen clock released tentative images')
            for index in range(25,33):
                clock=b'frozen' if index<28 else b'recovered' if index<32 else b'healthy-next'
                rows,proof=frozen.add(stream.getvalue(),clock,at+index*.5,
                    mono+index*.5,index+1,'smoke-freeze',state.generation)
                if index<32 and (rows or proof):
                    raise RuntimeError('First progress after freeze replayed recovery images')
            if [row[2] for row in rows]!=[at+14.5,at+15,at+15.5,at+16]:
                raise RuntimeError('Recovery failed to re-prime the next healthy window')
        finally:frozen.reset()
        if list((config.data_dir/'pending').rglob('*.png')):
            raise RuntimeError('Owned tentative PNGs leaked')
    return {'clock_window_four_ordered_frames':True,'clock_window_short_pass_normal_visual':True,
            'clock_window_no_early_ready':True,'clock_window_frozen_fail_closed':True,
            'clock_window_recovery_reprimed':True,'clock_window_synthetic_only':True}
