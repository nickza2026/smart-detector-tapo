import json
import os
import tkinter as tk
import unittest

from smart_tapo import smoke_window
from smart_tapo.desktop_notify_history import NotifyHistoryButton


class FakeHistory:
    last_error = False

    def __init__(self, count=205):
        self.rows = [dict(id=i, created=1700000000.0, channel='notify', alert_id='alert',
                          importance='critical', url='https://example.invalid/full',
                          http_status=None if i % 2 else 200,
                          state=('success', 'failed', 'pending')[i % 3], code=None,
                          elapsed_ms=None) for i in range(count, 0, -1)]
        self.pages = []
        self.details = []

    def list_page(self, before_id=None, limit=100):
        self.pages.append((before_id, limit))
        return [row for row in self.rows if before_id is None or row['id'] < before_id][:limit]

    def detail(self, record_id):
        self.details.append(record_id)
        row = next(row for row in self.rows if row['id'] == record_id)
        return dict(row, request_body=' {"message": "ไทย"}\n',
                    request_headers={'Authorization': '[REDACTED]'},
                    response_body='raw response\n', response_headers={}, response_truncated=False)


@unittest.skipUnless(os.name == 'nt', 'Requires native offscreen/no-focus policy')
class NotifyHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        smoke_window.install()  # Must precede even the first root construction.

    def setUp(self):
        self.root = tk.Tk()
        self.history = FakeHistory()
        self.getter_calls = 0

        def getter():
            self.getter_calls += 1
            return self.history

        self.button = NotifyHistoryButton(self.root, getter)
        self.button.pack()
        self.root.deiconify()
        self.button.invoke()
        self.window = self.button.window
        self.root.update()

    def tearDown(self):
        self.button.close()
        self.root.destroy()

    def test_bounded_all_states_navigation_and_lazy_readonly_json(self):
        window = self.window
        self.assertEqual(self.history.pages, [(None, 100)])
        self.assertEqual(self.history.details, [])
        self.assertEqual(len(window.tree.get_children()), 100)
        states = {window.tree.set(item, 'state') for item in window.tree.get_children()}
        self.assertEqual(states, {'success', 'failed', 'pending'})
        window.tree.selection_set('205')
        window._select()
        text = window.details.get('1.0', 'end-1c')
        metadata, bodies = text.split('\n\nREQUEST BODY\n', 1)
        payload = json.loads(metadata)
        self.assertTrue(bodies.startswith(' {"message": "ไทย"}\n'))
        self.assertIn('\n\nRESPONSE BODY\nraw response\n', bodies)
        self.assertEqual(payload['url'], 'https://example.invalid/full')
        self.assertEqual(str(window.details['state']), 'disabled')
        self.assertEqual(str(window.details['wrap']), 'none')
        window.older()
        self.assertEqual(self.history.pages[-1], (106, 100))
        self.assertEqual(window.details.get('1.0', 'end-1c'), '')
        window.older()
        self.assertEqual(len(window.tree.get_children()), 5)
        self.assertEqual(str(window.older_button['state']), 'disabled')
        window.newer()
        self.assertEqual(self.history.pages[-1], (106, 100))
        window.newer()
        self.assertEqual(self.history.pages[-1], (None, 100))
        self.assertTrue(all(limit == 100 for _, limit in self.history.pages))

    def test_refresh_uses_current_data_directory_and_warning(self):
        previous = self.history
        self.history = FakeHistory(2)
        self.history.last_error = True
        self.window.refresh()
        self.assertEqual(len(self.window.tree.get_children()), 2)
        self.assertEqual(len(previous.pages), 1)
        self.assertIn('คำเตือน', self.window.status['text'])
        self.assertGreaterEqual(self.getter_calls, 2)

    def test_safe_unknown_errors_and_missing_detail(self):
        def broken(*args, **kwargs):
            raise RuntimeError('SECRET credential /private/path')

        self.history.detail = broken
        self.window.tree.selection_set('205')
        self.window._select()
        self.assertNotIn('SECRET', self.window.status['text'])
        self.assertEqual(self.window.details.get('1.0', 'end-1c'), '')
        self.window._history_getter = broken
        self.window.refresh()
        self.assertEqual(self.window.tree.get_children(), ())
        self.assertNotIn('SECRET', self.window.status['text'])
        self.assertIn('ลองใหม่', self.window.status['text'])

    def test_reuses_window_close_and_reopen(self):
        self.button.invoke()
        self.assertIs(self.button.window, self.window)
        self.button.close()
        self.assertFalse(self.window.winfo_exists())
        self.button.invoke()
        self.assertIsNot(self.button.window, self.window)
        self.button.window.destroy()
        self.button.invoke()
        self.assertTrue(self.button.window.winfo_exists())


if __name__ == '__main__':
    unittest.main()
