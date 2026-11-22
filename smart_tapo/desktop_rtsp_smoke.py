"""Cold desktop RTSP/vision/audio separation against owned synthetic sources only."""
from dataclasses import replace
import json
import os
import sqlite3
import time
from contextlib import closing

from PIL import Image, ImageDraw


def pump(root,predicate,timeout=20,label='unknown',diagnostic=None):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        root.update()
        if predicate():return
        time.sleep(.03)
    suffix='' if diagnostic is None else ' / '+str(diagnostic())[:350]
    raise RuntimeError('Synthetic RTSP GUI deadline: '+label+suffix)


def smoke_rtsp_gui(root,app):
    from .rtsp_av_smoke import smoke_av_gui
    return smoke_av_gui(root,app,_history)


def _history(root,app):
    from .history import list_events,get_images
    from .history_web import HistoryWebServer
    from http.cookiejar import CookieJar
    from urllib.request import HTTPCookieProcessor,build_opener
    from urllib.parse import urlencode
    page=list_events(app.config)
    if not page.events:raise RuntimeError('RTSP event missing from history')
    app.notebook.select(app.history_panel);app.history_panel._render(page);root.update()
    event_id=page.events[0].id
    result=dict(history_menu_created=app.notebook.tab(app.history_panel,'text')=='ประวัติการหยอดแบงค์',
        history_event_visible=event_id in app.history_panel.tree.get_children(),
        history_full_image=any(image.role=='first_full' for image in get_images(app.config,event_id)))
    app.history_panel.server=HistoryWebServer(lambda:app.config);web=app.history_panel.server;web.start(0)
    try:
        client=build_opener(HTTPCookieProcessor(CookieJar()))
        with client.open(web.url,timeout=5) as response:body=response.read(1024*1024).decode('utf-8')
        uri='http://127.0.0.1:'+str(web.port)+'/image?'+urlencode(dict(id=event_id,role='first_full'))
        with client.open(uri,timeout=5) as response:content=response.read(16*1024*1024+1)
        result['history_web_served']='ประวัติการหยอดแบงค์' in body and content.startswith(b'\x89PNG\r\n\x1a\n')
    finally:web.stop()
    if not all(result.values()):raise RuntimeError('RTSP history GUI/web incomplete')
    return result
