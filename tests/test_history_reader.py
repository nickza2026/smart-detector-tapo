import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image

from smart_tapo.config import Config
from smart_tapo.history import HistoryUnavailable, get_event, get_images, list_events


class HistoryReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config=replace(Config(),data_dir=self.root/'data')
        self.config.data_dir.mkdir()
        self.path=self.config.data_dir/'worker.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.executescript('CREATE TABLE settings(name TEXT PRIMARY KEY,value TEXT);'
                'CREATE TABLE events(id TEXT PRIMARY KEY,kind TEXT,started REAL,last_present REAL,first_clear REAL,closed REAL);'
                'CREATE TABLE evidence(event TEXT,role TEXT,path TEXT,PRIMARY KEY(event,role));')
            db.execute('INSERT INTO settings VALUES(?,?)',('camera_id',self.config.camera_id))
        self.now=2000000.0

    def event(self,number,started=None,closed=None,kind='normal_visual'):
        event_id=f'{number:032x}'
        started=self.now-100+number if started is None else started
        closed=started+2 if closed is None else closed
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',
                       (event_id,kind,started,started+1,closed,closed))
        return event_id

    def image(self,event_id,role='first_full',outside=False):
        folder=self.root/'outside' if outside else self.config.data_dir/'events'/self.config.camera_id
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/(event_id+'-'+role+'.png')
        Image.new('RGB',(4,4),'white').save(path)
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO evidence VALUES(?,?,?)',(event_id,role,str(path)))
        return path

    def test_empty_history_never_creates_database_or_directory(self):
        config=replace(self.config,data_dir=self.root/'not-created')
        self.assertEqual(list_events(config).events,())
        self.assertIsNone(get_event(config,'absent'))
        self.assertEqual(get_images(config,'absent'),())
        self.assertFalse(config.data_dir.exists())

    def test_latest_first_and_keyset_pagination_remain_stable_after_new_event(self):
        ids=[self.event(n) for n in range(103)]
        first=list_events(self.config,limit=100,now=self.now)
        self.assertEqual([e.id for e in first.events],list(reversed(ids[3:])))
        self.event(999,started=self.now-1)
        second=list_events(self.config,before=first.next_before,limit=100,now=self.now)
        self.assertEqual([e.id for e in second.events],list(reversed(ids[:3])))
        self.assertIsNone(second.next_before)

    def test_normal_full_day_starts_at_closure_and_expired_images_are_not_served(self):
        retained=self.event(1,started=self.now-90000,closed=self.now-86399)
        expired=self.event(2,started=self.now-90000,closed=self.now-86401)
        self.image(retained);self.image(expired)
        self.assertEqual([e.id for e in list_events(self.config,now=self.now).events],[retained])
        self.assertEqual(len(get_images(self.config,retained,now=self.now)),1)
        self.assertIsNone(get_event(self.config,expired,now=self.now))
        self.assertEqual(get_images(self.config,expired,now=self.now),())

    def test_only_event_owned_existing_images_are_opened_and_reads_do_not_mutate(self):
        event=self.event(1)
        self.image(event,'first_full')
        self.image(event,'first',outside=True)
        snapshot=self.path.read_bytes()
        self.assertEqual([i.role for i in get_images(self.config,event,now=self.now)],['first_full'])
        self.assertEqual(self.path.read_bytes(),snapshot)
        image=get_images(self.config,event,now=self.now)[0]
        image.path.unlink()
        self.assertEqual(get_images(self.config,event,now=self.now),())

    def test_camera_mismatch_cannot_display_another_cameras_history(self):
        self.event(1)
        with self.assertRaises(HistoryUnavailable):
            list_events(replace(self.config,camera_id='other'),now=self.now)

    def test_invalid_query_limits_cursors_and_port_values_are_rejected(self):
        for limit in (0,101,True):
            with self.assertRaises(ValueError):list_events(self.config,limit=limit)
        for cursor in ((float('nan'),'x'),('text','x'),(1,'x'*129)):
            with self.assertRaises(ValueError):list_events(self.config,before=cursor)
        for port in (0,65536,True,1.5):
            with self.assertRaises(ValueError):replace(self.config,history_web_port=port).validate()


if __name__=='__main__':unittest.main()
