import json
import os
import sqlite3
import time
import unittest
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch

from smart_tapo.config import Config,load_config
from smart_tapo.desktop_history import local_time
from smart_tapo.history import get_event,list_events
from smart_tapo.history_web import _stamp
from smart_tapo.time_display import thai_time
from tests import test_history_gui as gui_fixture
from tests import test_history_reader as history_fixture
from tests import test_history_web as web_fixture


class ThaiTimeTests(unittest.TestCase):
    def test_fixed_thai_time_even_when_host_zone_differs(self):
        old=os.environ.get('TZ')
        self.addCleanup(lambda:os.environ.pop('TZ',None) if old is None else os.environ.__setitem__('TZ',old))
        if hasattr(time,'tzset'):self.addCleanup(time.tzset)
        for zone in ('UTC','Pacific/Honolulu','Asia/Tokyo'):
            with patch.dict(os.environ,{'TZ':zone}):
                if hasattr(time,'tzset'):time.tzset()
                self.assertEqual(thai_time(0),'01/01/1970 07:00:00')
                self.assertEqual(local_time(0),'01/01/1970 07:00:00')
                self.assertEqual(_stamp(0),'1970-01-01T07:00:00+07:00')
        if hasattr(time,'tzset'):time.tzset()

    def test_invalid_time_never_raises(self):
        for value in (None,float('inf'),float('nan'),'private-value',10**100):
            self.assertEqual(thai_time(value),'—')


class EmergencyConfigTests(unittest.TestCase):
    def test_validation_and_old_config_keeps_priority_one(self):
        self.assertEqual(Config().notification_priority,1)
        for field,values in {'notification_priority':(0,3,True,'2'),
                            'emergency_retry_seconds':(29,True,60.5),
                            'emergency_expire_seconds':(0,10801,float('inf')),
                            'emergency_poll_seconds':(4,False,float('nan'))}.items():
            for value in values:
                with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                    replace(Config(),**{field:value}).validate()
        replace(Config(),notification_priority=2,emergency_retry_seconds=30,
                emergency_expire_seconds=10800,emergency_poll_seconds=5).validate()


class EmergencyGuiTests(unittest.TestCase):
    setUp=gui_fixture.HistoryGuiTests.setUp

    def test_explicit_priority_two_save_and_capture_interval_preserve_count(self):
        self.assertEqual(self.app.priority.get(),'1 — High')
        self.app.priority.set('2 — Emergency')
        self.app.form['capture_interval_seconds'].set('0.2')
        self.assertTrue(self.app.save_settings())
        config=self.store.load_or_create()
        self.assertEqual(config.notification_priority,2)
        self.assertEqual(config.capture_interval_seconds,.2)
        self.assertEqual(config.stuck_consecutive_frames,10)
        self.assertEqual(config.emergency_retry_seconds,60)
        self.assertFalse(config.notify_enabled)
        self.assertFalse(any(key in json.loads(self.store.config_path.read_text(encoding='utf-8')) for key in ('token','user')))

    def test_invalid_emergency_settings_do_not_overwrite_config(self):
        before=self.store.config_path.read_bytes()
        self.app.priority.set('2 — Emergency')
        for key,value in (('emergency_retry_seconds','29'),('emergency_expire_seconds','10801'),('emergency_poll_seconds','4')):
            old=self.app.emergency_form[key].get()
            self.app.emergency_form[key].set(value)
            self.assertFalse(self.app.save_settings())
            self.assertEqual(self.store.config_path.read_bytes(),before)
            self.app.emergency_form[key].set(old)

    def test_priority_locked_with_running_worker_and_readonly_when_stopped(self):
        self.controller.active=True
        self.app._poll()
        self.assertEqual(str(self.app.priority_widget.cget('state')),'disabled')
        self.controller.active=False
        self.app._poll()
        self.assertEqual(str(self.app.priority_widget.cget('state')),'readonly')

    def test_notification_save_actions_fit_minimum_window(self):
        self.root.geometry('850x650')
        self.app.notebook.select(self.app.notifications)
        self.root.deiconify()
        end=time.monotonic()+3
        while time.monotonic()<end:
            self.root.update()
            if self.app.notification_actions.winfo_ismapped():break
            time.sleep(.01)
        actions=self.app.notification_actions
        self.assertTrue(actions.winfo_ismapped())
        self.assertGreater(actions.winfo_height(),10)
        self.assertLessEqual(actions.winfo_y()+actions.winfo_height(),self.app.notifications.winfo_height())


class EmergencyHistoryTests(unittest.TestCase):
    setUp=history_fixture.HistoryReaderTests.setUp
    event=history_fixture.HistoryReaderTests.event

    def test_legacy_then_state_read_without_receipt_exposure_or_schema_mutation(self):
        identity=self.event(1,kind='stuck')
        self.assertEqual(get_event(self.config,identity,now=self.now).emergency_state,'')
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('CREATE TABLE emergency_notifications(id TEXT,event TEXT,state TEXT,receipt TEXT,tag TEXT,created REAL)')
            db.execute('INSERT INTO emergency_notifications VALUES(?,?,?,?,?,?)',('n',identity,'acknowledged','private-receipt','private-tag',self.now))
        result=list_events(self.config,now=self.now).events[0]
        self.assertEqual(result.emergency_state,'acknowledged')
        self.assertEqual(result.kind,'stuck')
        self.assertNotIn('private-',repr(result))
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("UPDATE emergency_notifications SET state='private-untrusted-value'")
        self.assertEqual(get_event(self.config,identity,now=self.now).emergency_state,'')


class EmergencyWebTests(unittest.TestCase):
    setUp=web_fixture.HistoryWebTests.setUp
    event=web_fixture.HistoryWebTests.event
    request=web_fixture.HistoryWebTests.request
    login=web_fixture.HistoryWebTests.login

    def test_html_shows_thai_time_and_ack_without_receipt_or_tag(self):
        self.event(started=0)
        with closing(sqlite3.connect(self.db_path)) as db,db:
            db.execute('CREATE TABLE emergency_notifications(id TEXT,event TEXT,state TEXT,receipt TEXT,tag TEXT,created REAL)')
            db.execute('INSERT INTO emergency_notifications VALUES(?,?,?,?,?,?)',('n','evt','acknowledged','private-receipt','private-tag',1))
        self.login()
        for path in ('/','/event?id=evt'):
            status,headers,body=self.request(path)
            text=body.decode('utf-8')
            self.assertEqual(status,200)
            self.assertIn('1970-01-01T07:00:00+07:00',text)
            self.assertIn('รับทราบแล้ว',text)
            self.assertNotIn('private-receipt',text)
            self.assertNotIn('private-tag',text)
