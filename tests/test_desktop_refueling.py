"""Synthetic history tests; run Tk tests only through run_local_checks.py."""
import hashlib
import io
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from smart_tapo.desktop_refueling import RefuelingHistoryPanel, _image_bytes, _query, _run, session_values


def row(**changes):
    result = dict(id='session1', camera_id='camera1', started_at=100, last_ready_at=200,
                  ready_count=2, ended_at=None, repeated_ready_at=200, missing_end_at=501,
                  evidence=[])
    result.update(changes)
    return result


def result(rows=()):
    return dict(rows=list(rows), counts={}, cleanup={}, image=None)


class ReaderTests(unittest.TestCase):
    def test_absent_store_does_not_create_directory_or_connect(self):
        with tempfile.TemporaryDirectory() as tmp, patch('smart_tapo.desktop_refueling.RefuelingStore') as store:
            directory = Path(tmp) / 'absent'
            self.assertEqual(_query(directory, 'camera1', True, None)['rows'], [])
            self.assertFalse(directory.exists())
            store.assert_not_called()

    def test_completed_keeps_flags_and_null_end_is_explicit(self):
        self.assertEqual(session_values(row())[3], 'ยังไม่พบเวลาจบ')
        values = session_values(row(ended_at=600))
        self.assertIn('จบแล้ว', values[4])
        self.assertIn('ประกาศพร้อมเติมซ้ำ', values[4])
        self.assertIn('ไม่พบเสียงจบภายใน 5 นาที', values[4])

    def test_worker_opens_uses_closes_on_same_thread_and_cleans_only_owned_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'refueling'
            directory.mkdir()
            sqlite3.connect(directory / 'refueling.sqlite3').close()
            source = Path(tmp) / 'visual.png'
            source.write_bytes(b'preserve')
            calls = []
            class Store:
                def __init__(self, path, camera):
                    self.owner = threading.get_ident()
                    self.db = sqlite3.connect(path / 'refueling.sqlite3')
                    calls.append(('open', self.owner, path, camera))
                def check(self):
                    assert threading.get_ident() == self.owner
                    self.db.execute('SELECT 1')
                def cleanup(self):
                    self.check(); calls.append(('cleanup', self.owner)); return {'media_delete_failed': 1}
                def list_history(self, limit):
                    self.check(); assert limit == 200; return [row()]
                def delivery_counts(self):
                    self.check(); return {'expired_before_delivery': 3}
                def close(self):
                    self.check(); self.db.close(); calls.append(('close', self.owner))
            output = queue.Queue(1)
            with patch('smart_tapo.desktop_refueling.RefuelingStore', Store):
                worker = threading.Thread(target=_run, args=(output, 4, directory, 'camera1', True, None))
                worker.start(); worker.join(3)
            generation, data, error = output.get_nowait()
            self.assertFalse(error)
            self.assertEqual(generation, 4)
            self.assertEqual(data['counts']['expired_before_delivery'], 3)
            self.assertEqual([call[0] for call in calls], ['open', 'cleanup', 'close'])
            self.assertNotEqual(calls[0][1], threading.get_ident())
            self.assertEqual(source.read_bytes(), b'preserve')

    def test_image_manifest_hash_size_mime_missing_and_supplied_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp)
            stream = io.BytesIO()
            Image.new('RGB', (8, 8), 'red').save(stream, 'PNG')
            data = stream.getvalue()
            identifier = 'a' * 32
            owned = media / (identifier + '.png')
            owned.write_bytes(data)
            manifest = dict(id=identifier, content_type='image/png', byte_size=len(data),
                            sha256=hashlib.sha256(data).hexdigest(), path='C:/untrusted.png')
            self.assertEqual(_image_bytes(media, manifest), data)
            for changes in ({'sha256': '0'*64}, {'byte_size': 1}, {'content_type': 'image/jpeg'}, {'id': '../bad'}):
                with self.assertRaises((ValueError, OSError)):
                    _image_bytes(media, dict(manifest, **changes))
            owned.unlink()
            with self.assertRaises(OSError):
                _image_bytes(media, manifest)


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.config = SimpleNamespace(data_dir=Path(self.tmp.name), camera_id='camera1')
        self.panel = RefuelingHistoryPanel(self.root, lambda: self.config)
        self.addCleanup(self.panel.close)

    def test_constructor_has_no_store_or_network_side_effects(self):
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])
        self.assertFalse(self.panel.busy)

    def test_render_selection_flags_no_auto_popup_and_failure_summary(self):
        self.panel._render(result([row()]))
        self.panel.tree.selection_set('session1')
        data = result([row(ended_at=600)])
        data['cleanup'] = {'media_delete_failed': 1}
        data['counts'] = {'expired_before_delivery': 2}
        self.panel._render(data)
        self.assertEqual(self.panel.tree.selection(), ('session1',))
        self.assertIn('ประกาศพร้อมเติมซ้ำ', self.panel.tree.item('session1', 'values')[4])
        self.assertIn('ลบภาพหมดอายุไม่สำเร็จ', self.panel.summary.cget('text'))
        self.assertIn('2', self.panel.summary.cget('text'))
        self.assertIsNone(self.panel.viewer)
        self.assertIn('ไม่ได้ยืนยัน', self.panel.status.cget('text'))

    def test_scope_switch_ignores_stale_result_and_worker_target_has_no_panel(self):
        main = threading.get_ident()
        targets = []
        class Thread:
            def __init__(self, target, args, **kwargs):
                targets.append((target, args))
            def start(self):
                self_id = threading.get_ident()
                assert self_id == main
        with patch('smart_tapo.desktop_refueling.threading.Thread', Thread):
            self.panel.poll(visible=True)
            old = self.panel.generation
            self.config = SimpleNamespace(data_dir=Path(self.tmp.name)/'other', camera_id='camera2')
            self.panel.results.put_nowait((old, result([row()]), False))
            self.panel.poll(visible=True)
        self.assertEqual(self.panel.rows, {})
        self.assertEqual(len(targets), 2)
        self.assertIs(targets[0][0], _run)
        self.assertNotIn(self.panel, targets[0][1])
        self.assertEqual(targets[1][1][3], 'camera2')

    def test_close_does_not_schedule_callbacks_or_hold_tk_in_worker(self):
        self.panel.close()
        with patch('smart_tapo.desktop_refueling.threading.Thread') as thread:
            self.panel.poll(True)
            self.panel.show_image()
            thread.assert_not_called()

    def test_viewer_is_explicit_and_missing_image_error_is_safe(self):
        self.panel._render(result([row()]))
        self.panel.show_image()
        self.assertIsNone(self.panel.viewer)
        stream = io.BytesIO()
        Image.new('RGB', (8,8)).save(stream, 'PNG')
        self.panel._viewer(stream.getvalue())
        self.assertTrue(self.panel.viewer.winfo_exists())
        self.panel.scope = (Path(self.tmp.name)/'refueling', 'camera1')
        self.panel.next_cleanup = self.panel.next_refresh = float('inf')
        self.panel.results.put_nowait((self.panel.generation, None, True))
        self.panel.poll()
        self.assertIn('อ่านประวัติ/ภาพ', self.panel.status.cget('text'))


if __name__ == '__main__':
    unittest.main()
