"""Explicit operator-reviewed local profiles; no discovery, playback, or promotion."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import stat
import threading
import uuid

from .announcement import PhraseTemplate, read_wav, _feature_version
from .announcement_profile import _identifier, _pinned_file, _regular, _hash, load_announcement_profile
from .audio import lock_directory, unlock_directory

_LIMIT = 64 * 1024 * 1024
_RESERVE = 64 * 1024 * 1024
_ERROR = 'invalid_private_profile'


def _path(value):
    path = Path(value)
    if ('..' in path.parts or any(
            part.endswith((' ', '.')) or ':' in part
            for part in path.parts if part != path.anchor)):
        raise ValueError
    return Path(os.path.abspath(path))


def _identity(info):
    return info.st_dev, info.st_ino


def _pin(stack, path):
    handles = lock_directory(path, check_contents=False)
    stack.callback(unlock_directory, handles)


def _directory(stack, path):
    _pin(stack, path.parent)
    try:
        path.mkdir()
    except FileExistsError:
        pass
    _pin(stack, path)


def _cleanup(folder, folder_id, owned):
    # Never recurse, follow a replacement, or remove a file we did not create.
    try:
        with ExitStack() as stack:
            _pin(stack, folder)
            if _identity(folder.lstat()) != folder_id:
                return
            for path, identity in reversed(owned):
                try:
                    info = path.lstat()
                    if (path.parent == folder and _identity(info) == identity
                            and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                            and not getattr(info, 'st_file_attributes', 0) & 0x400):
                        path.unlink()
                except OSError:
                    pass
        # Windows ancestry handles must be closed before removing the directory.
        with ExitStack() as stack:
            _pin(stack, folder.parent)
            info = folder.lstat()
            if (_identity(info) == folder_id and stat.S_ISDIR(info.st_mode)
                    and not getattr(info, 'st_file_attributes', 0) & 0x400):
                folder.rmdir()  # fails safely if anything unexpected remains
    except Exception:
        pass


def create_private_profile(data_dir, camera_id, templates, *, feature_version=1) -> Path:
    """Copy exactly two explicitly selected WAVs into a new persistent profile.

    Files are bounded to 64 MiB each / 128 MiB total, with 64 MiB free reserve.
    No existing file is overwritten. All failures use a non-private error code.
    """
    folder = None
    folder_id = None
    owned = []
    try:
        _feature_version(feature_version)
        if not _identifier(camera_id) or type(templates) not in (list, tuple) or len(templates) != 2:
            raise ValueError
        records = []
        for entry in templates:
            fields={'kind', 'file', 'start', 'end', 'threshold'}
            if (type(entry) is not dict or not fields<=set(entry)
                    or set(entry)-fields-{'temporal_threshold','alternates'}):
                raise ValueError
            record = dict(entry)
            temporal=record.get('temporal_threshold')
            if 'temporal_threshold' in record and (type(temporal) not in (float,int)
                    or not math.isfinite(temporal) or not 0<temporal<=1):
                raise ValueError
            if type(record['kind']) is not str or record['kind'] not in ('paid_ready', 'end'):
                raise ValueError
            for key in ('start', 'end', 'threshold'):
                if type(record[key]) not in (int, float) or not math.isfinite(record[key]):
                    raise ValueError
            if not (0 <= record['start'] < record['end'] <= 20
                    and .8 <= record['end'] - record['start'] <= 9.5
                    and .5 <= record['threshold'] <= 1):
                raise ValueError
            if 'alternates' in record:
                alternates=record['alternates']
                if (record['kind']!='paid_ready' or type(alternates) is not list or len(alternates)!=1
                        or any(type(a) is not dict or set(a)!={'start','end'} for a in alternates)):
                    raise ValueError
                record['alternates']=[dict(a) for a in alternates]
            record['file'] = _path(record['file'])
            if record['file'].suffix.lower() != '.wav':
                raise ValueError
            records.append(record)
        if {record['kind'] for record in records} != {'paid_ready', 'end'}:
            raise ValueError
        root = _path(data_dir)
        with ExitStack() as stack:
            _directory(stack, root)
            profiles = root / 'announcement-profiles'
            _directory(stack, profiles)
            total = 0
            source_hashes = []
            for record in records:
                source = record['file']
                _pin(stack, source.parent)
                stack.enter_context(_pinned_file(source, _LIMIT))
                total += _regular(source, _LIMIT).st_size
                source_hashes.append(_hash(source))
                samples, rate = read_wav(source)
                PhraseTemplate.from_recording(record['kind'], samples, rate,
                                              record['start'], record['end'], record['threshold'],record.get('temporal_threshold'),
                                              feature_version=feature_version,
                                              alternate_spans=[(a['start'],a['end']) for a in record.get('alternates',[])])
            if total > 2 * _LIMIT or shutil.disk_usage(profiles).free < total + _RESERVE:
                raise ValueError
            profile_id = 'private_' + uuid.uuid4().hex
            candidate = profiles / profile_id
            candidate.mkdir()  # exclusive; collision never becomes owned
            folder = candidate
            folder_id = _identity(folder.lstat())
            _pin(stack, folder)
            manifest = {'schema_version': 1, 'profile_id': profile_id,
                        'camera_id': camera_id, 'templates': []}
            if feature_version != 1:
                manifest['feature_version'] = feature_version
            for record, expected in zip(records, source_hashes):
                name = 'ready.wav' if record['kind'] == 'paid_ready' else 'end.wav'
                target = folder / name
                digest = hashlib.sha256()
                with target.open('xb') as output:
                    owned.append((target, _identity(os.fstat(output.fileno()))))
                    with record['file'].open('rb') as source:
                        count = 0
                        for chunk in iter(lambda: source.read(65536), b''):
                            count += len(chunk)
                            if count > _LIMIT:
                                raise ValueError
                            digest.update(chunk)
                            output.write(chunk)
                if digest.hexdigest() != expected or _hash(target) != expected or _hash(record['file']) != expected:
                    raise ValueError
                manifest['templates'].append({**record, 'file': name, 'sha256': expected})
            result = folder / 'profile.json'
            with result.open('x', encoding='utf-8') as output:
                owned.append((result, _identity(os.fstat(output.fileno()))))
                json.dump(manifest, output, ensure_ascii=True, allow_nan=False)
            load_announcement_profile(result, camera_id)
        return result
    except Exception:
        if folder is not None and folder_id is not None:
            _cleanup(folder, folder_id, owned)
        raise ValueError(_ERROR) from None


def open_profile_setup(parent, config, on_saved):
    """Open an opt-in Thai editor. The caller must keep capture/listening inactive."""
    import tkinter as tk
    from tkinter import filedialog, ttk

    # Freeze configuration now; no live config or Tk reads in the worker.
    data_dir, camera_id = _path(config.data_dir), config.camera_id
    dialog = tk.Toplevel(parent)
    dialog.title('สร้างโปรไฟล์เสียงส่วนตัว')
    dialog.geometry('760x480')
    dialog.minsize(700, 460)
    frame = ttk.Frame(dialog, padding=12)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='เลือก WAV PCM16 ของคุณเท่านั้น — ไม่ค้นหาไฟล์หรือเปิดฟังอัตโนมัติ',
              wraplength=720).pack(anchor='w')
    ttk.Label(frame, text='ต้องใช้ Live เพื่อฟังจริง การสร้างนี้ไม่เปิดเสียง กล้อง SDK หรือเครือข่าย',
              wraplength=720).pack(anchor='w', pady=(4, 8))
    rows = []
    controls = []
    confirmed = tk.BooleanVar(master=dialog,value=False)
    status = tk.StringVar(master=dialog,value='ระบุจุดเริ่มและจุดจบเอง ห้ามตัดหรือเดาคำพูด (ช่วง 0.8–9.5 วินาที)')
    busy = False

    def changed(*_):
        confirmed.set(False)

    for kind, caption in (('paid_ready', 'พร้อมใช้งาน / รับชำระแล้ว'), ('end', 'สิ้นสุดการใช้งาน')):
        box = ttk.LabelFrame(frame, text=caption, padding=6)
        box.pack(fill='x', pady=4)
        path = tk.StringVar(master=dialog)
        start, end, threshold = (tk.StringVar(master=dialog,value=value) for value in ('0','','0.9'))
        entry = ttk.Entry(box, textvariable=path)
        entry.grid(row=0, column=0, columnspan=6, sticky='ew')
        def pick(variable=path):
            value = filedialog.askopenfilename(parent=dialog, filetypes=[('PCM16 WAV', '*.wav')])
            if value:
                variable.set(value)
        button = ttk.Button(box, text='เลือก WAV', command=pick)
        button.grid(row=0, column=6, padx=4)
        controls.extend((entry, button))
        for column, (label, variable) in enumerate((('เริ่ม (วิ)', start), ('จบ (วิ)', end), ('เกณฑ์', threshold))):
            ttk.Label(box, text=label).grid(row=1, column=column * 2, pady=4)
            field = ttk.Entry(box, textvariable=variable, width=9)
            field.grid(row=1, column=column * 2 + 1)
            controls.append(field)
        box.columnconfigure(0, weight=1)
        for variable in (path, start, end, threshold):
            variable.trace_add('write', changed)
        rows.append((kind, path, start, end, threshold))
    ttk.Label(frame, text='0.9 เป็นเพียงค่า correlation ทดลอง ไม่ใช่โอกาสถูก 90% และไม่มีผลวัดความแม่นยำ',
              wraplength=720).pack(anchor='w', pady=4)
    ttk.Label(frame, text='สำเนา WAV อยู่ในเครื่อง ไม่อัปโหลด และเก็บจนกว่าคุณจะลบโปรไฟล์เอง\n'
              'เป็นการตั้งค่าถาวร ไม่ใช่ประวัติที่ลบใน 3 วัน', wraplength=720).pack(anchor='w')
    consent = ttk.Checkbutton(frame, variable=confirmed, text='ฉันทบทวนแล้ว: แต่ละช่วงมีวลีที่ถูกต้องครบถ้วน (พร้อม / สิ้นสุด)\n'
                              'ไม่มีการแต่งจำนวนเหรียญหรือยอดเงิน และยินยอมเก็บสำเนาในเครื่อง')
    consent.pack(anchor='w', pady=6)
    controls.append(consent)
    ttk.Label(frame, textvariable=status, wraplength=720).pack(anchor='w')
    results = queue.Queue()

    def poll():
        nonlocal busy
        try:
            ok, value = results.get_nowait()
        except queue.Empty:
            dialog.after(75, poll)
            return
        busy = False
        for control in controls:
            control.configure(state='normal')
        confirmed.set(False)
        if ok:
            status.set('บันทึกแล้ว — ยังไม่ได้เริ่มฟัง')
            on_saved(value)
            destroy_editor()
        else:
            status.set('บันทึกไม่สำเร็จ โปรดตรวจไฟล์ PCM16 ช่วงเวลา เกณฑ์ และพื้นที่ว่าง')

    def save():
        nonlocal busy
        if busy or not confirmed.get():
            return
        try:
            snapshot = [{'kind': kind, 'file': _path(path.get()), 'start': float(start.get()),
                         'end': float(end.get()), 'threshold': float(threshold.get())}
                        for kind, path, start, end, threshold in rows]
        except (ValueError, OverflowError):
            confirmed.set(False)
            status.set('กรุณาระบุเวลาและเกณฑ์เป็นตัวเลขที่ถูกต้อง')
            return
        busy = True
        for control in controls:
            control.configure(state='disabled')
        save_button.configure(state='disabled')
        status.set('กำลังตรวจและคัดลอกไฟล์ โปรดรอ ห้ามปิดหน้าต่าง')
        def worker():
            try:
                results.put((True, create_private_profile(data_dir, camera_id, snapshot)))
            except Exception:
                results.put((False, None))
        threading.Thread(target=worker, daemon=False).start()
        dialog.after(75, poll)

    save_button = ttk.Button(frame, text='สร้างโปรไฟล์', command=save, state='disabled')
    save_button.pack(anchor='e', pady=4)
    confirmed.trace_add('write', lambda *_: save_button.configure(
        state='normal' if confirmed.get() and not busy else 'disabled'))
    variables=[confirmed,status]+[variable for row in rows for variable in row[1:]]
    def release_variables(event=None):
        if event is not None and event.widget is not dialog:return
        # Tcl trace callbacks retain the confirmed variable through a Python
        # cycle. Remove them on the Tk thread; otherwise the next file worker's
        # GC can finalize a closed dialog's variables off-thread.
        for variable in variables:
            if variable._tk is None:continue
            for modes,command in variable.trace_info():variable.trace_remove(modes,command)
        for variable in variables:
            if variable._tk is None:continue
            # Variable has no public close(): dispose its Tcl state explicitly
            # on the owner thread, then leave later Python GC a no-op.
            tk.Variable.__del__(variable)
            variable._tk=None
            variable._root=None
    def destroy_editor():
        release_variables()
        dialog.destroy()
    dialog.bind('<Destroy>',release_variables,add='+')
    dialog.request_close=lambda: None if busy else destroy_editor()
    dialog.protocol('WM_DELETE_WINDOW', dialog.request_close)
    return dialog
