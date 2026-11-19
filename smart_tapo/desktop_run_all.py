"""One click-scoped, explicit start transaction; importing never starts anything."""
from copy import deepcopy
from importlib.util import find_spec
from tkinter import messagebox

from .desktop_calibration import is_ready
from .announcement_profile import load_announcement_profile
from .prepared_profile import prepare_profile, needs_prepared_upgrade
from .startup import profile_digest
from .desktop_webhook import PreparedStartError


class RunAllError(ValueError):
    """Only fixed, non-secret human messages cross this boundary."""


CONFIRM = ('เริ่ม Capture + Worker + เสียงจาก RTSP ที่บันทึกไว้ + Refueling API?\n'
           'ยินยอมฟังแหล่ง RTSP ที่เลือกจนกดหยุด รวมลองแหล่งเดิมใหม่เมื่อข้อมูลขาด\n'
           'ผูกคิวเดิมที่ยังไม่มีปลายทางทั้งหมดของกล้องนี้ รวมเหตุใหม่ ไป URL/คีย์ที่บันทึกไว้\n'
           'ไม่ย้ายคิวที่ผูกแล้ว ไม่ส่งซ้ำรายการ delivered และไม่เปลี่ยนอายุคิว\n'
           'ใช้โปรไฟล์ที่เลือก หรือโปรไฟล์เสียงที่เตรียมให้เมื่อยังไม่ได้เลือก')


def _check(app, config):
    if (app.closing or app.busy or app.config != config or app._candidate() != config
            or app._calibration_pending() or not is_ready(config, app.store.root)):
        raise RunAllError('ยังไม่เริ่ม: บันทึกการตั้งค่าและยืนยันภาพช่องว่างก่อน หรือรองานเดิมหยุด')
    if config.capture_backend != 'rtsp':
        raise RunAllError('ยังไม่เริ่ม: เลือกและบันทึกแหล่ง RTSP ก่อน')
    if app.offline_audio_panel.is_running or app.startup_panel.is_running:
        raise RunAllError('ยังไม่เริ่ม: หยุดงานเสียงทดสอบหรือเริ่มอัตโนมัติเดิมก่อน')
    if (app.notify_key.get().strip(), app.notify_base.get().strip()) != app._health_credentials:
        raise RunAllError('ยังไม่เริ่ม: บันทึกข้อมูลการแจ้งเตือนก่อน')
    if config.notify_enabled and not all(app._health_credentials):
        raise RunAllError('ยังไม่เริ่ม: บันทึกคีย์แจ้งเตือนก่อน')
    if any(find_spec(name) is None for name in ('av', 'numpy', 'cv2')):
        raise RunAllError('ยังไม่เริ่ม: ส่วนประกอบ RTSP ไม่ครบ กรุณาตรวจชุดโปรแกรม')
    config.validate()


def run_all(app, *, confirm=None):
    """Return true when ordered; synchronous failure rolls back only owned starts.

    The GUI should increment ``_run_all_generation`` on Stop All and Close, so a
    modal dialog cannot revive a stopped session. No network probe is performed.
    """
    if getattr(app, '_run_all_pending', False):
        return False
    app._run_all_pending = True
    owned = []
    try:
        config = deepcopy(app.config)
        _check(app, config)
        listener = app.audio_panel.listener_panel
        panel = app.webhook_panel
        roles = tuple(app.controller.is_running(role) for role in ('capture', 'worker'))
        bound_roles = getattr(app, '_run_all_role_configs', {})
        if any(running and bound_roles.get(role) != config
               for role, running in zip(('capture', 'worker'), roles)):
            raise RunAllError('ยังไม่เริ่ม: หยุดงานเดิมก่อน เพราะยืนยันการตั้งค่าที่กำลังทำงานไม่ได้')
        audio = listener.is_running
        if audio and (not listener._watching or listener._watch_config != config
                      or listener.profile_path.get().strip() != listener._watch_path
                      or profile_digest(listener._watch_path) != listener._watch_digest
                      or load_announcement_profile(listener._watch_path, config.camera_id).fingerprint
                         != listener._watch_fingerprint):
            raise RunAllError('ยังไม่เริ่ม: หยุดเสียงเดิมก่อนเปลี่ยนแหล่งหรือโปรไฟล์')
        plan = panel.prepare_start(consent=True)
        if all(roles) and audio and plan.active:
            app.status.set('ทุกส่วนสั่งเริ่มอยู่แล้ว — ตรวจสถานะรายส่วน')
            return True
        selected = listener.profile_path.get().strip()
        profile = load_announcement_profile(selected, config.camera_id) if selected else None
        digest = profile_digest(selected) if selected else None
        upgrade = bool(selected and not audio and needs_prepared_upgrade(
            config.data_dir, config.camera_id, selected, listener.prepared_manifest))
        generation = getattr(app, '_run_all_generation', 0)
        ask = confirm or (lambda text: messagebox.askyesno('Run All', text, parent=app.root))
        consent=CONFIRM
        if upgrade:
            consent+='\nโปรไฟล์เสียงที่เตรียมให้รุ่นเดิมจะถูกคัดลอกเป็นรุ่นปัจจุบัน (วิธีเสียงรุ่น 2 / ช่วงเสียงพูดพร้อมเติมใหม่) โดยไม่แก้ไฟล์เดิมหรือโปรไฟล์ที่ปรับเอง'
        if not ask(consent):
            app.status.set('ยกเลิก Run All — ยังไม่เริ่มส่วนใหม่')
            return False
        _check(app, config)
        if (generation != getattr(app, '_run_all_generation', 0)
                or roles != tuple(app.controller.is_running(r) for r in ('capture', 'worker'))
                or listener.is_running != audio or listener.profile_path.get().strip() != selected):
            raise RunAllError('สถานะเปลี่ยน กรุณาตรวจและกด Run All ใหม่')
        panel.validate_prepared(plan)
        if upgrade and (profile_digest(selected) != digest or not needs_prepared_upgrade(
                config.data_dir,config.camera_id,selected,listener.prepared_manifest)):
            raise RunAllError('โปรไฟล์เดิมเปลี่ยน กรุณาตรวจและกด Run All ใหม่')
        if not selected or upgrade:
            selected = str(prepare_profile(config.data_dir, config.camera_id, listener.prepared_manifest))
            profile = load_announcement_profile(selected, config.camera_id)
            digest = profile_digest(selected)
        _check(app,config)
        if generation != getattr(app,'_run_all_generation',0):
            raise RunAllError('สถานะเปลี่ยน กรุณาตรวจและกด Run All ใหม่')
        if (profile_digest(selected) != digest or
                load_announcement_profile(selected, config.camera_id).fingerprint != profile.fingerprint):
            raise RunAllError('โปรไฟล์เปลี่ยน กรุณาตรวจและกด Run All ใหม่')
        app._run_all_role_configs = dict(bound_roles)
        if not roles[0]:
            owned.append(lambda: app.controller.stop('capture'))
            if app.controller.start_capture(config) is False:
                raise RunAllError('เริ่ม Capture ไม่ได้ — หยุดส่วนที่เริ่มใหม่แล้ว')
            app._run_all_role_configs['capture'] = deepcopy(config)
        if not roles[1]:
            owned.append(lambda: app.controller.stop('worker'))
            if app.controller.start_worker(config, app._health_credentials[0]) is False:
                raise RunAllError('เริ่ม Worker ไม่ได้ — หยุดส่วนที่เริ่มใหม่แล้ว')
            app._run_all_role_configs['worker'] = deepcopy(config)
        if not audio:
            listener.profile_path.set(selected)
            owned.append(listener.stop)
            listener.start_bound(config, selected, expected_fingerprint=profile.fingerprint)
        if not plan.active:
            owned.append(panel.stop)
            panel.start_prepared(plan)
        app.status.set('สั่งเริ่มทุกส่วนแล้ว / รอสถานะรายส่วน — ยังไม่ยืนยันภาพสดหรือ API รับข้อมูล'
                       +f' / วิธีเสียงรุ่น {getattr(profile,"feature_version",1)}')
        return True
    except Exception as exc:
        for stop in reversed(owned):
            try:
                stop()
            except Exception:
                pass
        message = str(exc) if isinstance(exc, (RunAllError, PreparedStartError)) else 'Run All ยังไม่สำเร็จ: ตรวจการตั้งค่า โปรไฟล์ สิทธิ์ และคีย์ที่บันทึกไว้'
        app.status.set(message)
        return False
    finally:
        app._run_all_pending = False
