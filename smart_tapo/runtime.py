from __future__ import annotations

import os
import signal
import threading
from contextlib import contextmanager
from pathlib import Path


class ProcessLock:
    """OS-backed lock, released on crash; never delete a locked file to steal ownership."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            self.file.close()
            self.file = None
            raise RuntimeError("Another process already owns " + str(self.path)) from None
        return self

    def __exit__(self, *args):
        if self.file is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()
                self.file = None


@contextmanager
def stop_event():
    """Coordinate Ctrl+C/termination without killing MEmu or abandoning writes."""
    stop = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for kind in (signal.SIGINT, signal.SIGTERM):
            previous[kind] = signal.getsignal(kind)
            signal.signal(kind, lambda *_args: stop.set())
    try:
        yield stop
    finally:
        stop.set()
        for kind, handler in previous.items():
            signal.signal(kind, handler)
