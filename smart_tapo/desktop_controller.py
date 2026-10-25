"""Spawn-only desktop process supervision; no GUI or hardware work in the parent."""
from __future__ import annotations

import logging
import multiprocessing
import os
import queue
from collections import deque

from smart_tapo.runtime import ProcessLock


def _emit(messages, role, kind):
    try:
        messages.put_nowait((role, kind))
    except queue.Full:
        pass


class _SafeLog(logging.Handler):
    def __init__(self, messages, role):
        super().__init__(logging.INFO)
        self.messages, self.role = messages, role

    def emit(self, record):
        # Translate known app templates to safe operator hints, never arbitrary
        # exception strings, camera UI XML, paths or credential-bearing args.
        template = str(record.msg)
        if template.startswith('Alignment unavailable:'):
            kind = 'alignment'
        elif template.startswith('Camera view changed:'):
            kind = 'view_changed'
        elif template.startswith('Image quota full'):
            kind = 'quota'
        elif template.startswith('Capture unavailable:'):
            kind = 'connection'
            if record.args and str(record.args[0]).startswith('Screenshot'):
                kind = 'screenshot'
        elif record.levelno >= logging.WARNING:
            kind = 'warning'
        else:
            return
        _emit(self.messages, self.role, kind)


def _child(role, config, stop, messages, token='', user=''):
    root = logging.getLogger()
    root.handlers[:] = [_SafeLog(messages, role)]
    root.setLevel(logging.INFO)
    try:
        config.validate()
        # Do not inherit ambient credentials, even when notifications are disabled.
        os.environ.pop('PUSHOVER_TOKEN', None)
        os.environ.pop('PUSHOVER_USER', None)
        if role == 'worker' and config.notify_enabled:
            if not token or not user:
                raise ValueError('Missing credentials')
            os.environ['PUSHOVER_TOKEN'] = token
            os.environ['PUSHOVER_USER'] = user
        lock = 'worker.lock' if role == 'worker' else 'capture-' + config.camera_id + '.lock'
        with ProcessLock(config.data_dir / lock):
            if role == 'capture':
                from smart_tapo.capture import capture
                _emit(messages, role, 'ready')
                result = capture(config, stop)
                if result:
                    raise RuntimeError('Capture failed')
            else:
                from smart_tapo.worker import Worker
                worker = Worker(config)
                try:
                    _emit(messages, role, 'ready')
                    while not stop.is_set():
                        worker.scan()
                        stop.wait(config.poll_interval_seconds)
                finally:
                    worker.close()
        _emit(messages, role, 'stopped')
    except BaseException:
        _emit(messages, role, 'failed')
        # Exit status signals failure even if the bounded message queue is full.
        raise SystemExit(1) from None


class DesktopController:
    def __init__(self):
        self._context = multiprocessing.get_context('spawn')
        self._roles = {role: dict(state='stopped', process=None, stop=None, messages=None)
                       for role in ('capture', 'worker')}
        self._messages = deque(maxlen=128)

    def _role(self, role):
        if role not in self._roles:
            raise ValueError('Unknown process role')
        return self._roles[role]

    def start_capture(self, config):
        return self._start('capture', config)

    def start_worker(self, config, token='', user=''):
        return self._start('worker', config, token, user)

    def _start(self, role, config, token='', user=''):
        self.snapshot()
        record = self._role(role)
        if self.is_running(role):
            return False
        try:
            config.validate()
        except Exception:
            raise ValueError('Invalid process configuration') from None
        if role == 'worker' and config.notify_enabled and not (token and user):
            raise ValueError('Notifications require both Pushover credentials')
        stop = self._context.Event()
        messages = self._context.Queue(maxsize=64)
        try:
            process = self._context.Process(target=_child,
                        args=(role, config, stop, messages, token, user), name='SmartTapo-' + role)
            record.update(state='starting', process=process, stop=stop, messages=messages)
            process.start()
        except Exception:
            record.update(state='failed', process=None, stop=None, messages=None)
            messages.close()
            self._messages.append(role + ': failed to start')
            return False
        self._messages.append(role + ': starting')
        return True

    def stop(self, role):
        record = self._role(role)
        if record['state'] in ('starting', 'running', 'stopping'):
            record['stop'].set()
            record['state'] = 'stopping'

    def stop_all(self):
        for role in self._roles:
            self.stop(role)

    def snapshot(self):
        for role, record in self._roles.items():
            messages = record['messages']
            if messages is not None:
                # Bounded per poll as well as bounded at source.
                for _ in range(64):
                    try:
                        sender, kind = messages.get_nowait()
                    except queue.Empty:
                        break
                    hints = {
                        'ready': 'กำลังทำงาน (สถานะ process; ดูข้อความกล้องด้านล่าง)',
                        'warning': 'พบปัญหา ตรวจการตั้งค่าและข้อมูลภาพก่อนใช้งานจริง',
                        'alignment': 'จัดแนวภาพไม่มั่นใจ จึงหยุดนับต่อเนื่องและไม่เดาแจ้งเตือน',
                        'view_changed': 'มุมกล้องเปลี่ยน เริ่มนับภาพต่อเนื่องใหม่',
                        'quota': 'พื้นที่เก็บภาพถึงขีดจำกัด หยุดรับภาพใหม่และรอ Worker cleanup',
                        'connection': 'รับภาพไม่ได้ ตรวจ adb.exe / Serial, เปิด MEmu และล็อกอิน Tapo หน้า Live ตามชื่อกล้อง',
                        'screenshot': 'ภาพไม่ตรงขนาดที่ตั้งไว้หรืออ่าน PNG ไม่ได้ ตรวจความละเอียด MEmu',
                        'failed': 'เริ่มงานไม่สำเร็จ ตรวจการตั้งค่า ภาพช่องว่าง และโปรแกรมอื่นที่ใช้ข้อมูลชุดนี้',
                        'stopped': 'หยุดแล้ว',
                    }
                    if sender != role or kind not in hints:
                        continue
                    if kind == 'ready' and record['state'] == 'starting':
                        record['state'] = 'running'
                    text = hints[kind]
                    self._messages.append(role + ': ' + text)
            process = record['process']
            if process is not None and process.exitcode is not None:
                failed = process.exitcode != 0 or record['state'] == 'failed'
                record['state'] = 'failed' if failed else 'stopped'
                process.join(timeout=0)
                process.close()
                messages.close()
                record.update(process=None, stop=None, messages=None)
        return {role: dict(state=record['state'], pid=record['process'].pid
                          if record['process'] is not None else None)
                for role, record in self._roles.items()}

    def drain_messages(self):
        self.snapshot()
        result = list(self._messages)
        self._messages.clear()
        return result

    def is_running(self, role):
        return self._role(role)['state'] in ('starting', 'running', 'stopping')
