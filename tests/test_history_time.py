"""History rendering is Bangkok time regardless of the machine timezone."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from smart_tapo import desktop_history, history_web


class HistoryTimeTests(unittest.TestCase):
    def test_gui_and_web_ignore_host_timezone_and_roll_the_date(self):
        timestamp = datetime(2026, 10, 27, 17, 30, tzinfo=timezone.utc).timestamp()
        for hours in (0, -8, 9, 7):
            host = timezone(timedelta(hours=hours))

            class HostDatetime(datetime):
                @classmethod
                def fromtimestamp(cls, value, tz=None):
                    current = datetime.fromtimestamp(value, tz=host if tz is None else tz)
                    return cls(current.year, current.month, current.day, current.hour, current.minute,
                               current.second, current.microsecond, tzinfo=current.tzinfo)

                def astimezone(self, tz=None):
                    return super().astimezone(host if tz is None else tz)

            with self.subTest(host_offset=hours), \
                 patch.object(desktop_history, 'datetime', HostDatetime), \
                 patch.object(history_web, 'datetime', HostDatetime):
                with self.subTest(surface='desktop'):
                    self.assertEqual(desktop_history.local_time(timestamp), '28/10/2026 00:30:00')
                with self.subTest(surface='web'):
                    self.assertEqual(history_web._stamp(timestamp), '2026-10-28T00:30:00+07:00')

    def test_missing_and_invalid_timestamps_remain_explicit(self):
        for value in (None, 'invalid', float('nan'), float('inf'), 10**100):
            with self.subTest(value=value):
                self.assertEqual(desktop_history.local_time(value), '—')
                self.assertEqual(history_web._stamp(value), '—')

    def test_web_document_labels_fixed_thai_timezone(self):
        body = history_web._document('', 24).decode('utf-8')
        self.assertIn('เวลาไทย (UTC+7)', body)


if __name__ == '__main__':
    unittest.main()
