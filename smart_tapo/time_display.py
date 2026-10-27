"""Display captured Unix timestamps in modern Thailand time, independent of PC TZ.

This does not correct a wrong host clock, infer camera OSD time or stream delay.
"""
from datetime import datetime, timedelta, timezone

THAI_TIMEZONE=timezone(timedelta(hours=7),name='UTC+7')


def thai_time(value, *, iso=False):
    if value is None:
        return '—'
    try:
        stamp=datetime.fromtimestamp(value,tz=THAI_TIMEZONE)
        return stamp.isoformat(timespec='seconds') if iso else stamp.strftime('%d/%m/%Y %H:%M:%S')
    except (ValueError, TypeError, OverflowError, OSError):
        return '—'
