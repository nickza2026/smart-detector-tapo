"""Explicit RTSP audio controls. Construction never probes or opens a source."""
from copy import deepcopy
from pathlib import Path
import sys
import math


_STORAGE_RECOVERY_CODES = frozenset({'storage_busy', 'storage_full', 'storage_permission', 'storage_io_error'})
_STAGE_LABELS = {'receiving': 'รับเสียง', 'matching': 'เทียบประกาศ', 'persisted': 'ยืนยันบันทึกแล้ว',
                 'storage_open': 'เปิดประวัติ', 'storage_write': 'บันทึกประวัติ',
                 'storage_commit': 'ยืนยันประวัติ', 'storage_flush': 'ยืนยันประวัติ',
                 'storage_gap': 'บันทึกช่วงข้อมูลขาด', 'storage_cleanup': 'จัดการข้อมูลหมดอายุ',
                 'storage_close': 'ปิดประวัติ'}
import time
from tkinter import BooleanVar, StringVar, ttk

from .desktop_listener import ListenerPanel
from .prepared_profile import default_prepared_manifest
from .health_alerts import SAMPLE_MAX_AGE
from .storage_diagnostics import format_guard_evidence
from .audio_recovery import AudioRecovery


def audio_source_identity(config):
    """Exact source/store inputs consumed by the RTSP audio child, never logged."""
    return config.camera_id, config.data_dir, config.rtsp_url


class RtspListenerPanel(ListenerPanel):
    def __init__(self, parent, config_getter, *, runtime=None, other_running=lambda: False,
                 prepared_manifest=None, settings_current=lambda: True, clock=time.monotonic):
        ttk.Frame.__init__(self, parent, padding=12)
        if runtime is None:
            from .rtsp_audio import RtspContinuousListener
            runtime = RtspContinuousListener()
        self.get_config, self.runtime = config_getter, runtime
        self.get_device = lambda: None
        self.other_running, self.settings_current = other_running, settings_current
        self.closing = self._stopping = self._active = False
        self.clock = clock
        self._watching = False
        self._recovery = AudioRecovery()
        self._health_expected = False
        self._health_generation = 0
        self._health_since = self.clock()
        self._health_good = self._health_observed = None
        self._health_reason = 'starting'
        self._health_pcm = None
        self._storage_guard_detail = ''
        self._retry_at = None
        self._retry_count = 0
        self._watch_config = self._watch_path = self._watch_digest = self._watch_fingerprint = None
        self.profile_dialog = None
        base = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
        self.prepared_manifest = Path(prepared_manifest) if prepared_manifest else default_prepared_manifest(base)
        self._prepare_future = self._prepare_executor = None
        self._announcements = 0
        self._epoch_total = self._count_offset = 0
        self._epoch_kinds=self._kind_offset=(0,0)
        self._typed_counts_known=True
        self.profile_path = StringVar(master=self)
        self.consent = BooleanVar(master=self, value=False)
        self.status = StringVar(master=self, value='ยังไม่รับเสียง — เลือกโปรไฟล์และยินยอมก่อนเริ่ม')
        self.start_hint = StringVar(master=self)
        self.device_summary = StringVar(master=self)
        self.count = StringVar(master=self, value='พร้อมเติมหลังรับเงิน: 0 | เสียงจบ: 0\nรวม 0 ประกาศ — ไม่ใช่จำนวนรอบเติม')
        self.health_status = StringVar(master=self, value='ยังไม่มีข้อมูลสุขภาพ — ไม่เก็บไฟล์เสียง')
        ttk.Label(self, text='ฟังประกาศจาก RTSP', style='Subheading.TLabel').pack(anchor='w')
        ttk.Label(self, text='ใช้ RTSP URL เดียวกับภาพ แต่รับเสียงแยก process ไม่ใช้ MEmu / ADB / เสียง Windows\nความต่อเนื่องของ transport ไม่ใช่หลักฐานว่าเป็นภาพ/เสียงสดทางกายภาพ; ข้อมูลขาดไม่ใช่เสียงเงียบ', wraplength=750).pack(anchor='w', pady=6)
        row = ttk.Frame(self); row.pack(fill='x', pady=8)
        ttk.Label(row, text='โปรไฟล์ JSON:').pack(side='left')
        self.path_entry = ttk.Entry(row, textvariable=self.profile_path)
        self.path_entry.pack(side='left', fill='x', expand=True, padx=6)
        self.browse_button = ttk.Button(row, text='เลือกไฟล์…', command=self.choose_profile)
        self.browse_button.pack(side='left')
        self.prepared_button = ttk.Button(self, text='ใช้โปรไฟล์จากเสียงที่เตรียมให้', command=self.use_prepared_profile)
        self.prepared_button.pack(anchor='w')
        self.create_button = ttk.Button(self, text='ตั้งโปรไฟล์เอง (ขั้นสูง)…', command=self.create_profile)
        self.create_button.pack(anchor='w')
        ttk.Label(self, textvariable=self.device_summary, wraplength=750).pack(anchor='w', pady=6)
        self.consent_box = ttk.Checkbutton(self, text='ยินยอมฟัง RTSP ที่บันทึกไว้จนกดหยุด รวมลองแหล่งเดิมใหม่เมื่อข้อมูลขาด', variable=self.consent)
        self.consent_box.pack(anchor='w')
        row = ttk.Frame(self); row.pack(anchor='w', pady=8)
        self.start_button = ttk.Button(row, text='เริ่มฟัง RTSP', command=self.start)
        self.start_button.pack(side='left', padx=(0,8))
        self.stop_button = ttk.Button(row, text='หยุดฟัง', command=self.stop)
        self.stop_button.pack(side='left')
        ttk.Label(self, textvariable=self.start_hint, wraplength=750).pack(anchor='w')
        for variable in (self.status, self.count, self.health_status):
            ttk.Label(self, textvariable=variable, wraplength=750).pack(anchor='w', pady=4)
        self.profile_path.trace_add('write', lambda *_: self._buttons())
        self.consent.trace_add('write', lambda *_: self._buttons())
        self._buttons()

    @property
    def is_running(self):
        return self._watching or super().is_running

    def start_bound(self, config, path, *, expected_fingerprint=None):
        """Start one consented watch; retries retain these exact source/profile bytes."""
        from .startup import profile_digest
        frozen = deepcopy(config)
        digest = profile_digest(path)
        self._health_expected = True
        self._health_generation += 1
        self._health_since = self.clock()
        self._health_good = self._health_observed = self._health_pcm = None
        self._health_reason = 'starting'
        self.runtime.start(frozen, None, path, consent=True, expected_fingerprint=expected_fingerprint)
        self._recovery.reset()
        self._watch_config, self._watch_path, self._watch_digest = frozen, path, digest
        self._watch_fingerprint = expected_fingerprint or getattr(self.runtime, 'profile_fingerprint', None)
        self._watching, self._active, self._stopping = True, True, False
        self._retry_at, self._retry_count = None, 0
        self._epoch_total = self._count_offset = self._announcements = 0
        self._epoch_kinds=self._kind_offset=(0,0)
        self._typed_counts_known=True
        self._render_counts()

    def _retry(self):
        if not self._watching or self._retry_at is None or self.clock() < self._retry_at:
            return
        from .startup import profile_digest
        try:
            current = self.get_config()
            if audio_source_identity(current) != audio_source_identity(self._watch_config):
                raise ValueError
            if not self._settings_ready() or self.profile_path.get().strip() != self._watch_path or profile_digest(self._watch_path) != self._watch_digest:
                raise ValueError
        except Exception:
            self._watching, self._retry_at = False, None
            self._recovery.reset()
            self._health_expected = False
            self.consent.set(False)
            self.status.set('แหล่งหรือโปรไฟล์เปลี่ยน — ยกเลิกลองใหม่ ต้องยินยอมเริ่มใหม่')
            return
        if self.runtime.is_running:
            # A stuck previous child must not mute health forever or overlap a
            # new producer. This scheduled start fails its reap precondition.
            if self._recovery.take_due(self.clock()):
                self._schedule_recovery()
            return
        if not self._recovery.take_due(self.clock()):
            return
        self._retry_at = None
        self._retry_count = self._recovery.attempts
        self._health_since = self.clock()
        self._health_good = self._health_observed = self._health_pcm = None
        self._health_reason = 'starting'
        try:
            self.runtime.start(deepcopy(self._watch_config), None, self._watch_path, consent=True,
                               expected_fingerprint=self._watch_fingerprint)
            self._count_offset += self._epoch_total
            self._kind_offset=tuple(a+b for a,b in zip(self._kind_offset,self._epoch_kinds))
            self._epoch_kinds=(0,0)
            self._epoch_total = 0
            self.status.set(f'กำลังลองเสียง RTSP ใหม่ครั้งที่ {self._recovery.attempts}/3 — รอ PCM ที่บันทึกได้ ช่วงขาดไม่ใช่ไม่มีการเติม')
        except Exception:
            self._health_reason = 'audio_listener_failed'
            self._health_observed = self.clock()
            self._schedule_recovery()

    def _schedule_recovery(self):
        self._recovery.fail(self.clock())
        self._retry_at = self._recovery.due
        if self._recovery.exhausted:
            self._watching = False
            self.status.set('ลองเริ่มเสียง RTSP ใหม่ครบ 3 ครั้งแล้วยังไม่พร้อม — หยุดลองและเปิดการแจ้งสุขภาพตามการตั้งค่า')
        else:
            self.status.set(f'เสียง RTSP ขัดข้อง — รอลองใหม่ครั้งที่ {self._recovery.attempts + 1}/3 ใน 15 วินาที ยังไม่แจ้งเตือน ช่วงขาดไม่ใช่ไม่มีการเติม')

    def _service_recovery(self):
        if not self._watching or not self._health_expected or not self.runtime.is_running:
            return
        now = self.clock()
        if self._health_good is not None and 0 <= now - self._health_good <= SAMPLE_MAX_AGE:
            self._recovery.observe_good(self._health_good)
        elif (now - self._health_since > SAMPLE_MAX_AGE
              or self._health_reason not in ('starting', 'unknown')):
            self._schedule_recovery()
            # Stop only this owned audio child; wait for poll() to reap it
            # before the scheduled attempt. Do not disarm the consented watch.
            self.runtime.stop()

    def stop(self):
        self._recovery.reset()
        self._health_expected = False
        self._health_good = None
        self._watching, self._retry_at = False, None
        super().stop()
        callback=getattr(self,'on_health_stop',None)
        if callable(callback):
            try:callback()
            except Exception:pass  # Optional telemetry cannot prevent stopping.

    def close(self):
        self._recovery.reset()
        self._health_expected = False
        self._health_good = None
        self._watching, self._retry_at = False, None
        super().close()

    def _settings_ready(self):
        try: return bool(self.settings_current())
        except Exception: return False

    def _buttons(self):
        # Includes consent/profile variable traces invoked by close() after
        # mainloop destroyed the widget tree. Do not skip resource cleanup.
        if not self.tk.call('info','commands',self._w):return
        busy = self.is_running or self.closing
        self.device_summary.set('แหล่งเสียง: RTSP URL ที่บันทึกในแท็บตั้งค่า (ไม่แสดง URL)')
        other_busy, settings_ready = self._other_busy(), self._settings_ready()
        selected = bool(self.profile_path.get().strip())
        allowed = not busy and not other_busy and settings_ready and selected and self.consent.get()
        self.start_button.configure(state='normal' if allowed else 'disabled')
        if busy:
            hint = 'กำลังรับเสียงหรือรอลองแหล่งเดิม — กดหยุดได้ก่อนเริ่มรอบใหม่'
        elif other_busy:
            hint = 'รอการทำงานเบื้องหลังให้เสร็จก่อนเริ่มรับเสียง'
        elif not settings_ready:
            hint = 'บันทึก RTSP URL รหัสกล้อง และโฟลเดอร์ข้อมูลในแท็บตั้งค่าก่อนเริ่มเสียง'
        elif not selected:
            hint = 'เลือกโปรไฟล์ก่อนเริ่มรับเสียง'
        elif not self.consent.get():
            hint = 'ติ๊กยินยอมแล้วกดเริ่มฟัง RTSP เพื่อเริ่มรอบใหม่'
        else:
            hint = 'พร้อมเริ่มรับเสียง RTSP'
        self.start_hint.set(hint)
        self.stop_button.configure(state='normal' if (self.is_running or getattr(self,'_health_expected',False)) and not self.closing else 'disabled')
        for widget in (self.path_entry, self.browse_button, self.consent_box, self.create_button, self.prepared_button):
            widget.configure(state='disabled' if busy else 'normal')

    def start(self):
        if self.closing or self.is_running or self._other_busy(): return
        if not self._settings_ready():
            self.status.set('บันทึกการตั้งค่าที่เปลี่ยนก่อนเริ่มเสียง'); return
        if not self.profile_path.get().strip() or not self.consent.get():
            self.status.set('เลือกโปรไฟล์และยินยอมรับเสียง RTSP ก่อน'); return
        try:
            self.start_bound(self.get_config(), self.profile_path.get().strip())
            self._active, self._stopping, self._announcements = True, False, 0
            self.consent.set(False)
            self.status.set('กำลังเตรียมโปรไฟล์และเปิดเสียง RTSP — ยังไม่ยืนยันว่าได้รับข้อมูล')
        except Exception:
            self.status.set('เริ่มเสียง RTSP ไม่สำเร็จ ตรวจโปรไฟล์และส่วนรับเสียงที่รวมในโปรแกรม')
        self._buttons()

    def _render_counts(self):
        if self._typed_counts_known:
            ready,ended=(a+b for a,b in zip(self._kind_offset,self._epoch_kinds))
            self.count.set(f'พร้อมเติมหลังรับเงิน: {ready} | เสียงจบ: {ended}\nรวม {self._announcements} ประกาศ — ไม่ใช่จำนวนรอบเติม')
        else:
            self.count.set(f'ตรวจพบเสียงเริ่ม/จบรวม: {self._announcements} ประกาศ\nยังไม่มีข้อมูลแยกชนิด — ไม่ใช่จำนวนรอบเติม')

    def _update_counts(self,event):
        total=event.get('observations_total')
        if type(total) is int and total>=0:
            if total<self._epoch_total:return
            self._epoch_total=total
        elif type(event.get('observations')) is int and event['observations']>=0:
            self._epoch_total+=event['observations']
        else:return
        self._announcements=self._count_offset+self._epoch_total
        ready,ended=event.get('ready_total'),event.get('end_total')
        if (type(ready) is int and type(ended) is int and ready>=self._epoch_kinds[0]
                and ended>=self._epoch_kinds[1] and ready+ended==self._epoch_total):
            self._epoch_kinds=(ready,ended)
        else:self._typed_counts_known=False
        self._render_counts()

    def _accept_health(self, event, now):
        kind = event.get('kind')
        if kind == 'finished' and self._watching and self._recovery.pending and self._retry_at is not None:
            return  # This is the owned stop before recovery, not a new fault.
        if kind in ('error', 'listener_status', 'listener_retry', 'finished'):
            stamp = event.get('heartbeat', now)
            if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                    or not self._health_since <= stamp <= now
                    or (self._health_observed is not None and stamp < self._health_observed)):
                return
            self._health_observed = stamp
            code = event.get('code')
            self._health_reason = ('audio_storage' if type(code) is str and code.startswith('storage_') else
                'audio_no_track' if code in ('no_audio_track', 'no_audio_stream') else
                'audio_disconnected' if code == 'rtsp_disconnected' else
                'audio_no_pcm' if code in ('audio_timeout', 'audio_timestamp_invalid', 'audio_timestamp_gap', 'audio_backlog') else
                'audio_listener_failed')
            self._health_good = None
            return
        if kind != 'listener_progress' or event.get('waiting_audio') is not False or event.get('stage') != 'persisted':
            return
        observed, begin = event.get('heartbeat'), event.get('pcm_started_at')
        frames, session = event.get('audio_frames'), event.get('pcm_session')
        if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (observed, begin))
                or not self._health_since <= begin <= observed <= now
                or type(frames) is not int or frames <= 0
                or type(session) is not str or len(session) != 32
                or any(c not in '0123456789abcdef' for c in session)):
            return
        if self._health_observed is not None and observed <= self._health_observed:
            return
        if self._health_pcm is not None and session == self._health_pcm[0] and frames <= self._health_pcm[1]:
            return
        self._health_pcm = (session, frames)
        self._health_observed = self._health_good = observed
        self._health_reason = 'unknown'

    def health_snapshot(self, now=None):
        now = self.clock() if now is None else now
        good = self._health_good
        healthy = bool(self._health_expected and good is not None and 0 <= now - good <= SAMPLE_MAX_AGE)
        reason = self._health_reason if good is None or healthy else 'audio_no_pcm'
        if healthy:
            reason = 'ok'
        if reason == 'starting' and now - self._health_since > SAMPLE_MAX_AGE:
            reason = 'audio_no_pcm'
        return dict(expected=self._health_expected, healthy=healthy, reason=reason,
                    observed_at=self._health_observed, generation=self._health_generation,
                    last_good_at=good, recovery_pending=(self._recovery.pending or
                        (self._health_expected and self._watching and good is None
                         and self._health_reason == 'starting')))

    def _show_health_freshness(self,now):
        sample=self.health_snapshot(now)
        if not sample['expected']:
            label='หยุดเฝ้าสุขภาพเสียง'
        elif sample['healthy']:
            label='มี PCM ใหม่ที่ผ่านการบันทึก (เสียงเงียบก็ใช้งานได้)'
        else:
            reason=sample['reason']
            label={
                'audio_no_pcm':'ไม่มี PCM ใหม่ที่ใช้งานได้ (audio_no_pcm)',
                'audio_disconnected':'การเชื่อมต่อเสียงขาด (audio_disconnected)',
                'audio_storage':'บันทึกประวัติเสียงไม่ได้ (audio_storage)',
                'audio_no_track':'ไม่มี audio track (audio_no_track)',
                'audio_listener_failed':'ตัวรับเสียงหยุดผิดปกติ (audio_listener_failed)',
                'starting':'กำลังรอ PCM แรก',
            }.get(reason,'ยังยืนยันความสดของ PCM ไม่ได้')
        last=sample['last_good_at']
        age=f' | PCM ล่าสุด {max(0.,now-last):.1f} วินาทีก่อน' if last is not None else ''
        details = [getattr(self, '_health_detail', ''), getattr(self, '_storage_guard_detail', '')]
        self.health_status.set('ความสดเสียง: '+label+age+'\n'+'\n'.join(item for item in details if item))

    def _progress_diagnostics(self, event):
        def count(name):
            value = event.get(name)
            return str(value) if type(value) is int and value >= 0 else 'ไม่ทราบ'
        rms = event.get('audio_rms')
        rms_text = f'{rms:.4g}' if type(rms) in (int, float) and math.isfinite(rms) and rms >= 0 else 'ไม่ทราบ'
        stage = _STAGE_LABELS.get(event.get('stage'), 'ไม่ทราบ')
        states = {'warming_up': 'กำลังสะสมเสียงสำหรับเทียบ', 'no_candidate': 'ยังไม่พบผู้สมัครที่ผ่านเกณฑ์',
                  'filtered': 'มีผู้สมัครตกเกณฑ์ temporal/anchor', 'pending': 'มีผู้สมัคร รอช่วงยืนยัน',
                  'confirmed': 'พบประกาศยืนยันแล้ว', 'failed': 'ตัวเทียบเสียงหยุดเพราะข้อมูลไม่ผ่านเงื่อนไข'}
        def score(name):
            value = event.get(name)
            return f'{value:.3f}' if type(value) in (int, float) and math.isfinite(value) else 'ไม่ทราบ'
        fingerprint = getattr(self.runtime, 'profile_fingerprint', None)
        fingerprint = fingerprint[:12] if isinstance(fingerprint, str) and len(fingerprint) == 64 and all(
            c in '0123456789abcdef' for c in fingerprint) else 'ไม่ทราบ'
        self._health_detail=(
            f'ขั้นตอน: {stage} | เฟรมเสียง: {count("audio_frames")} | RMS: {rms_text}\n'
            f'{states.get(event.get("match_state"), "ยังไม่มีผลวิเคราะห์")} | โปรไฟล์: {fingerprint}\n'
            f'กำกวมสะสม: {count("ambiguous_total")} | ผู้สมัครหน้าต่างล่าสุด: {count("candidate_count")} | '
            f'ปฏิเสธ: {count("rejected_count")} | รอยืนยัน: {count("pending_count")}\n'
            f'พร้อมเติม raw/เกณฑ์: {score("ready_raw_score")}/{score("ready_threshold")} | '
            f'temporal ณ raw สูงสุด/เกณฑ์: {score("ready_temporal_score")}/{score("ready_temporal_threshold")}\n'
            'เป็นข้อมูลความคืบหน้า ไม่ใช่จำนวนบันทึกยืนยัน — ไม่เก็บไฟล์เสียง')
        self.health_status.set(self._health_detail)

    def poll(self):
        if self._prepare_future is not None and self._prepare_future.done():
            future, self._prepare_future = self._prepare_future, None
            executor, self._prepare_executor = self._prepare_executor, None
            try:
                path = future.result()
                current = self.get_config()
                same = (current.camera_id, current.data_dir, current.rtsp_url) == (self._prepare_config.camera_id, self._prepare_config.data_dir, self._prepare_config.rtsp_url)
                if not self.closing and not self._stopping and same:
                    self.profile_path.set(str(path))
                    self.status.set('โปรไฟล์พร้อมแล้ว — ยินยอมและกดเริ่ม RTSP เอง')
            except Exception:
                self.status.set('เตรียมโปรไฟล์ไม่สำเร็จ ตรวจชุด prepared-profile และสิทธิ์ข้อมูล')
            finally:
                if executor is not None: executor.shutdown(wait=False, cancel_futures=True)
        if (self.closing or self._stopping) and self.profile_dialog is not None and self.profile_dialog.winfo_exists():
            self.profile_dialog.request_close()
        for event in self.runtime.poll():
            kind = event.get('kind')
            self._accept_health(event, self.clock())
            self._update_counts(event)
            if kind == 'listener_progress':
                self._progress_diagnostics(event)
                if not self.closing and not self._stopping:
                    if event.get('waiting_audio'):
                        self.status.set('รอข้อมูลเสียง RTSP — ยังไม่มีข้อมูล ไม่ใช่เสียงเงียบหรือไม่มีการเติม')
                    elif event.get('awaiting_boundary'):
                        self.status.set('กำลังรับข้อมูลเสียง RTSP — รอประกาศสิ้นสุดหลังช่วงขาดก่อนหน้า ไม่ผูกลูกค้าข้ามช่วงที่หาย')
                    elif type(event.get('ambiguous_total')) is int and event['ambiguous_total'] > 0:
                        self.status.set('ได้รับเสียง RTSP — พบเสียงกำกวม ไม่ถือเป็นประกาศยืนยัน')
                    elif event.get('observations_total') == 0:
                        self.status.set('ได้รับเสียง RTSP — ยังไม่มีประกาศที่ยืนยันบันทึก ไม่ใช่หลักฐานว่าไม่มีการเติม')
                    else:
                        self.status.set('กำลังรับข้อมูลเสียง RTSP — transport ต่อเนื่อง ไม่ใช่หลักฐาน physical Live')
            elif kind in ('error', 'listener_status', 'listener_retry'):
                code = event.get('code')
                messages = {
                    'decoder_unavailable': 'ส่วนถอดรหัสเสียงไม่พร้อม ต้องใช้โปรแกรมที่รวม PyAV แล้ว',
                    'no_audio_track': 'RTSP ไม่มี audio track — ไม่สลับไปเสียง Windows',
                    'storage_error': 'บันทึกประวัติเสียงไม่สำเร็จ — หยุดเฝ้า ตรวจข้อมูลโดยไม่ลบประวัติ',
                    'storage_busy': 'ประวัติกำลังถูกใช้งาน — รอให้การบันทึกอื่นเสร็จ',
                    'storage_full': 'พื้นที่บันทึกเต็ม — เพิ่มพื้นที่โดยไม่ลบประวัติในโปรแกรม',
                    'storage_permission': 'ไม่มีสิทธิ์บันทึกประวัติ — ตรวจสิทธิ์โฟลเดอร์ข้อมูล',
                    'storage_io_error': 'อุปกรณ์บันทึกข้อมูลไม่พร้อม — ตรวจอุปกรณ์จัดเก็บ',
                    'storage_path_unsafe': 'ตำแหน่งประวัติไม่ปลอดภัย — หยุดเฝ้า ตรวจตำแหน่งข้อมูล',
                    'storage_corrupt': 'ประวัติเสียหาย — หยุดเฝ้า ให้ผู้ดูแลตรวจโดยไม่ลบข้อมูล',
                    'storage_schema': 'รูปแบบประวัติไม่รองรับ — หยุดเฝ้า ตรวจรุ่นโปรแกรม',
                    'storage_camera_mismatch': 'ประวัติไม่ตรงกล้อง — หยุดเฝ้า ตรวจกล้องและข้อมูล',
                    'profile_invalid': 'โปรไฟล์ไม่ตรงกล้องหรือไม่พร้อม ตรวจโปรไฟล์เดิมก่อนเริ่มใหม่',
                    'dependency_unavailable': 'ส่วนถอดรหัสเสียงไม่พร้อม ต้องใช้โปรแกรมที่รวม PyAV แล้ว',
                    'dependency_missing': 'ส่วนถอดรหัสเสียงไม่พร้อม ต้องใช้โปรแกรมที่รวม PyAV แล้ว',
                    'no_audio_stream': 'RTSP ไม่มี audio track — ไม่สลับไปเสียง Windows',
                    'rtsp_disconnected': 'RTSP ขาดการเชื่อมต่อ — ช่วงขาดไม่ใช่เสียงเงียบ',
                    'audio_timestamp_gap': 'เวลาเสียงขาดหรือซ้อนเกินเกณฑ์ — เริ่มรับใหม่ ไม่ผูกประกาศข้ามช่วงขาด',
                    'audio_timestamp_invalid': 'ข้อมูลเวลาเสียงไม่ถูกต้อง — ไม่ใช้ข้อมูลที่ยืนยันเวลาไม่ได้',
                    'audio_backlog': 'เสียงล่าช้าหรือ clock คลาดเกินเกณฑ์ — เริ่มรับใหม่ ไม่ใช้เสียงค้างย้อนหลัง',
                    'profile_changed': 'โปรไฟล์เปลี่ยน — หยุดและเลือกโปรไฟล์ใหม่ก่อน',
                }
                self.status.set(messages.get(code, 'เสียง RTSP ไม่พร้อมหรือข้อมูลไม่ต่อเนื่อง — ตรวจแหล่งเดิม/โปรไฟล์ ไม่มี fallback'))
                if code in messages and code.startswith('storage_'):
                    stage = _STAGE_LABELS.get(event.get('stage'), 'ไม่ทราบ')
                    guard = format_guard_evidence(event.get('guard')) if code in ('storage_path_unsafe', 'storage_busy') else ''
                    if guard:
                        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
                        self._storage_guard_detail = f'เหตุ guard ล่าสุด (เวลาเครื่อง {stamp}) | ขั้นตอน: {stage} | {guard}'
                    self.health_status.set(f'บันทึกประวัติไม่สำเร็จ | ขั้นตอน: {stage} — ไม่ลบประวัติหรือคิว')
                if kind == 'error':
                    if self._watching and not self.closing and not self._stopping and code != 'profile_changed':
                        self._schedule_recovery()
                    else:
                        self._watching, self._retry_at = False, None
                        self._recovery.reset()
            elif kind == 'finished':
                if self._watching and not self.closing and not self._stopping:
                    if not (self._recovery.pending and self._retry_at is not None):
                        self._schedule_recovery()
                else:
                    self._watching, self._retry_at = False, None
                    self.status.set('หยุดรับเสียง RTSP — ประวัติอาจไม่ครบถ้าหยุดกลางรอบ')
        self._service_recovery()
        self._retry()
        if self._active and not self.is_running:
            self._active = False
        if hasattr(self,'_health_expected') and hasattr(self,'health_status'):
            self._show_health_freshness(self.clock())
        health = getattr(self.runtime, 'health_snapshot', None)
        if type(health) is dict and health.get('io_error'):
            detail = getattr(self, '_storage_guard_detail', '')
            self.health_status.set('บันทึกสุขภาพไม่ได้ — ไม่เก็บไฟล์เสียง' + ('\n' + detail if detail else ''))
        self._buttons()


class RtspAudioPanel(ttk.Frame):
    def __init__(self, parent, config_getter, *, runtime=None, settings_current=lambda: True,
                 other_running=lambda: False):
        super().__init__(parent, padding=14)
        self.modes = ttk.Notebook(self); self.modes.pack(fill='both', expand=True)
        self.listener_panel = RtspListenerPanel(self.modes, config_getter, runtime=runtime,
            settings_current=settings_current, other_running=other_running)
        self.modes.add(self.listener_panel, text='ฟัง RTSP ต่อเนื่อง')
        self.status = self.listener_panel.status

    @property
    def is_running(self): return self.listener_panel.is_running
    def poll(self): self.listener_panel.poll()
    def stop(self): self.listener_panel.stop()
    def close(self): self.listener_panel.close()
