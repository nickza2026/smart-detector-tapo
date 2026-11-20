# ข้อเสนอแนะการแก้ไข SmartTapo (ตรวจ 30 ตุลาคม 2026)

เอกสารนี้สรุปผลตรวจโปรเจกต์ว่าทำไม "ใช้งานจริงไม่ได้" และควรแก้อะไร อย่างไร เรียงตามลำดับความสำคัญ ทุกข้อระบุแหล่งหลักฐาน (ไฟล์:บรรทัด หรือไฟล์ผลวิเคราะห์) และแยกชัดว่าเป็น **ยืนยันแล้ว** หรือ **สมมติฐาน**

> ขอบเขตการตรวจ: อ่านโค้ดใน working tree ปัจจุบัน (branch `fix/stuck-alert-and-history-timeout`, HEAD `6f325c4` + ไฟล์ที่ยังไม่ commit), รันชุดทดสอบทั้งหมด, simulation และอ่าน log/ผลวิเคราะห์ที่เก็บไว้ใน `.tools/` **ไม่ได้** เชื่อม ADB/MEmu/เสียง/Pushover หรือ VPS จริง และไม่ได้แก้โค้ด

---

## สรุปสั้น

ส่วนถ่ายภาพจาก MEmu ทำงานได้ในระดับโค้ด แต่ระบบไม่เคยแจ้งเตือนแบงค์ค้างได้จริงด้วยสองเหตุผลหลัก

1. **กรอบตรวจ (ROI) ที่ตั้งไว้ในเครื่องจริงมองแบงค์ค้างเป็น "ว่าง"** จึงไม่มีอะไรให้นับตั้งแต่ต้น
2. **กติกานับ 10 ภาพต่อเนื่องเข้มเกินไปสำหรับสภาพเครื่องจริง** เฟรมขาด 1 ภาพหรือแย่ง lock ไม่ทัน 5 วินาทีก็รีเซ็ตเป็นศูนย์ และงานตรวจหน้า Live ใช้ lock ตัวเดียวกับงานบันทึกภาพ

นอกจากนี้ระบบไม่กู้ตัวเองหลังรีบูต/Tapo ปิด, ตัวตรวจหน้า Live เปราะต่อการเปลี่ยนแปลงหน้าจอ, ฟีเจอร์เสียง/webhook ยังเป็น library ที่ไม่ได้ต่อเข้าแอป และถ้า commit ตอนนี้แบบไม่ครบไฟล์ build จะพัง

ชุดทดสอบผ่านทั้งหมด (383 รายการ: ผ่าน 382 / ข้าม 1) **แต่ทุกการทดสอบใช้ ADB/เสียง/กล้องจำลอง** จึงไม่ใช่หลักฐานว่าใช้ได้บนเครื่องจริง ตาม `VERIFICATION.md` ทุกรุ่น v2–v13 ไม่เคยผ่านการทดสอบบน VPS จริง

---

## ตารางลำดับความสำคัญ

| # | ปัญหา | ระดับ | สถานะหลักฐาน | ต้องแก้โค้ด? |
|---|---|---|---|---|
| 1 | ROI 131×104 มองแบงค์ค้างเป็น EMPTY | Blocker | ยืนยันแล้ว (offline) | ไม่ — ตั้งค่าใหม่ใน GUI |
| 2 | เฟรมขาด/แย่ง UI lock → รีเซ็ตการนับ | Blocker | กลไกยืนยันแล้ว, ผลบน VPS เป็นสมมติฐาน | ใช่ |
| 3 | ไม่กู้ตัวเองหลังรีบูต/Tapo ปิด | สูง | ยืนยันแล้ว | ใช่ + ตั้งค่า Windows |
| 4 | ตัวตรวจหน้า Live เปราะ / Tap to refresh อาจไม่ทำงาน | สูง | โค้ดยืนยันแล้ว, ผลจริงเป็นสมมติฐาน | ใช่ |
| 5 | commit ไม่ครบไฟล์ → build พัง | สูง (ตอนส่งมอบ) | ยืนยันแล้ว | ไม่ — แค่ git add ให้ครบ |
| 6 | ฟีเจอร์ webhook/ผูกรูป/เสียง→แจ้งเตือน ยังไม่ต่อเข้าแอป | กลาง | ยืนยันแล้ว | ใช่ |
| 7 | quota ภาพ 512 MB อาจเต็ม | กลาง | สมมติฐาน | อาจต้อง |
| 8 | เสียงบน VPS (RDP/VNC, stream_fault) | กลาง | สาเหตุยังไม่พิสูจน์ | ต้องเก็บข้อมูลก่อน |
| 9 | เอกสาร/คำสั่งใน README ไม่ตรงไฟล์จริง | ต่ำ | ยืนยันแล้ว | ไม่ — แก้เอกสาร |

---

## 1. ROI ที่ตั้งไว้มองแบงค์ค้างเป็น "ว่าง" — Blocker

### หลักฐาน (ยืนยันแล้ว)

- `config.local.json`: `roi = {x:249, y:272, width:131, height:104}`, `difference_threshold = 15`, `changed_pixel_ratio = 0.12`
- `smart_tapo/vision.py:85-86` ต้องผ่าน **ทั้งสอง** เกณฑ์จึงเป็น PRESENT:

  ```python
  label = "PRESENT" if (score >= self.config.difference_threshold and
                        ratio >= self.config.changed_pixel_ratio) else "EMPTY"
  ```

- `.tools/video-analysis-20261028/RESULT-TH.md` วิเคราะห์คลิปแบงค์ค้างที่ผู้ใช้ส่ง (21.7 วินาที, 44 ตัวอย่าง):

  | ROI | ผล | mean difference | changed ratio |
  |---|---|---|---|
  | 249,272,**131×104** (ใช้อยู่) | EMPTY 44/44 | 12.32–12.69 (ต่ำกว่า 15) | 20.35–20.85% (ผ่าน) |
  | 249,272,**100×80** (ทดลอง) | PRESENT 44/44 | 20.18–20.50 | 34.45–35.30% |

- `.tools/supplied-image-analysis/report.json` จากภาพนิ่งให้ผลแบบเดียวกัน (score ≈ 12.5, ratio ≈ 0.208 → EMPTY)
- Replay 22 ภาพที่ 1 Hz ผ่าน Worker ชั่วคราว: ROI เดิมนับได้สูงสุด 0, ROI 100×80 นับได้ 21 และได้ stuck/shadow อย่างละ 1

### สาเหตุ

กรอบกว้างเกินจนรวมพื้นหลังที่ไม่เปลี่ยนเข้ามามาก ค่าความต่างเฉลี่ย (mean) จึงถูกเจือจางให้ต่ำกว่า 15 แม้สัดส่วนพิกเซลเปลี่ยนจะผ่านเกณฑ์

### ข้อจำกัดของหลักฐาน

- เป็นภาพจาก Playback ที่ resize ให้เข้ากรอบ 720×405 ไม่ใช่ PNG จาก ADB จริง
- เป็นเหตุการณ์เดียว ไม่ใช่ความแม่นยำทั่วไป
- ยังไม่มีคลิปหยอดปกติ/มือจริงพอที่จะวัด false positive

### วิธีแก้ (ไม่ต้องแก้โค้ด)

1. บน VPS หยุด Capture/Worker
2. แท็บ **ภาพช่องว่าง** → ใส่ `X=249, Y=272, กว้าง=100, สูง=80` → กด **ใช้พิกัดกรอบ**
3. ตรวจว่าภาพอ้างอิงไม่มีแบงค์จริง → ติ๊กยืนยัน → บันทึก
4. เริ่ม Worker/Capture แล้วดู log ในแท็บเริ่ม/หยุด ต้องเห็นค่า `score/threshold` สูงกว่า 15 เมื่อมีแบงค์จริง
5. **ห้าม** ลด `difference_threshold` เพื่อบังคับให้ผ่าน และห้ามใช้ภาพที่มีแบงค์เป็นภาพช่องว่าง

### ทางเลือกระยะยาว (ต้องแก้โค้ด, ต้องตัดสินใจ)

- เปลี่ยนเกณฑ์เป็นแบบใช้ `changed_pixel_ratio` เป็นหลักและ `score` เป็นเกณฑ์รอง (เช่น ratio ≥ 0.15 **หรือ** score ≥ 15 และ ratio ≥ 0.12) — ทนต่อขนาดกรอบมากกว่า แต่ต้องทดสอบกับคลิปหยอดปกติก่อนว่าไม่เตือนทุกครั้ง
- เพิ่มเครื่องมือใน GUI ที่แสดง score/ratio สดของ ROI ปัจจุบันระหว่างเลือกกรอบ เพื่อให้ผู้ใช้เห็นทันทีว่ากรอบนี้ "เห็น" แบงค์หรือไม่

---

## 2. เฟรมขาด / แย่ง UI lock ทำให้นับไม่ถึง 10 ภาพ — Blocker

### หลักฐานกลไก (ยืนยันจากโค้ด)

**ก. Worker รีเซ็ตการนับทุกครั้งที่ลำดับไม่ต่อกันพอดี**

`smart_tapo/worker/__init__.py:247-250, 282-283`

```python
continuity = previous is None or (
    frame.session_id == previous.session_id and frame.sequence == previous.sequence + 1
    and 0 < frame.captured_monotonic - previous.captured_monotonic <= self.config.max_frame_gap_seconds
    and 0 < frame.captured_at - previous.captured_at <= self.config.max_frame_gap_seconds)
...
if not continuity:
    self.count = self.empty = self.live_count = 0
```

`max_frame_gap_seconds = 1.8` ใน config ดังนั้นรอบถ่ายที่ช้ากว่า 1.8 วินาทีหรือข้าม sequence เพียงครั้งเดียวก็เริ่มนับใหม่

**ข. Capture ข้าม sequence เมื่อรอบช้า**

`smart_tapo/capture/__init__.py:285-286`

```python
missed = max(0, int((now - target) // config.capture_interval_seconds))
sequence += missed + 1
```

**ค. งานบันทึกภาพต้องรอ UI lock ตัวเดียวกับงาน UI dump**

`smart_tapo/capture/__init__.py:322` บันทึกภาพทุกเฟรมภายใต้ `ui_lock(...)` timeout 5 วินาที

ผู้ใช้ lock ตัวเดียวกัน (mutex `Local\SmartTapoUI-<hash serial>` ใน `smart_tapo/capture/ui_lock.py`):

| ผู้ใช้ | ตำแหน่ง | ถือนานแค่ไหน |
|---|---|---|
| Capture ตรวจหน้า Live เบื้องหลัง | `capture/__init__.py:160` (`read_page_mode`) | `dumpsys` + `uiautomator dump` + `cat` + `rm` + `dumpsys` (ราว 2.7 วินาทีบนเครื่องจริงตาม log .5) |
| Capture บันทึกภาพ | `capture/__init__.py:322` | สั้น แต่ต้องรอคิว |
| Worker ตัดสินผลแต่ละเฟรม | `worker/__init__.py:217` | timeout 5 วินาที |
| ตัวฟังเสียงต่อเนื่อง | `continuous_listener.py:77` | ตรวจหน้า Live เช่นกัน |

**ง. เมื่อรอ lock ไม่ทัน ระบบรีเซ็ต**

- Capture: `ui_lock` โยน `ValueError('ui_dump_busy')` (`ui_lock.py:37`) ซึ่งถูกจับที่ `capture/__init__.py:338-341` → **เปลี่ยน session ใหม่ sequence = 0** และ `verifier.reset()`
- Worker: `worker/__init__.py:217-219` รอ lock ไม่ได้ → `navigation_unavailable` → เป็น safety reason → `continuity = False` → **นับใหม่**

**จ. การตรวจหน้า Live เบื้องหลังเริ่มทุก 5 วินาที**

`smart_tapo/capture/live_verifier.py:30` เริ่มตรวจใหม่เมื่อผ่าน `interval / 2` (= 10/2 = 5 วินาที) ถ้าผลเป็นลบหรือ exception จะโยน `ValueError` (`live_verifier.py:24-27`) → Capture รีเซ็ต session ทั้งหมด ดังนั้น popup ชั่วคราวหรือ dump ล้มเหลวเพียงครั้งเดียวก็ตัดชุดหลักฐาน

### หลักฐานจากเครื่องจริง (มีแต่รุ่นเก่า)

`.tools/supplied-runtime-log-analysis.json` (log จริงช่วง 22:26–23:07 วันที่ 27 ต.ค. ก่อนรุ่น .5):

- 605 detections: EMPTY 589 / UNKNOWN 16 / **PRESENT 0**
- เหตุผล `gap` 207 ครั้ง, alignment ล้มเหลว 7 ครั้ง (`current_has_no_static_anchors` ×5)
- พลาดรอบถ่ายภาพ 257 slot, Capture ช้าซ้ำราวทุก 12–13 วินาที

**สมมติฐาน (ยังไม่วัดบน VPS):** หลัง .5 ย้ายการตรวจ UI ไปเบื้องหลังแล้ว แต่การบันทึกภาพยังต้องรอ lock เดียวกัน และ .10–.13 เพิ่ม Worker/ตัวฟังเสียงเข้ามาแย่ง lock อีก จึงน่าจะยังเกิด gap/รีเซ็ตบ่อย ยังไม่มี log จาก VPS หลังรุ่น .5 มายืนยัน

### วิธีแก้ที่แนะนำ

**2.1 ไม่ถือ UI lock ระหว่างบันทึกภาพ (แก้ใน `capture/__init__.py:322-328`)**

lock นี้ใช้กันการ navigation (กด refresh) ระหว่างเฟรม แต่สิ่งที่ต้องตรวจจริงคือ "generation ของ navigation ไม่เปลี่ยนระหว่างถ่าย" ซึ่งอ่านได้จากไฟล์ fence โดยไม่ต้องถือ mutex ของ UI dump แนวทาง:

- แยก mutex เป็นสองตัว: `ui_dump` (สำหรับ uiautomator) และ `navigation` (สำหรับการกด/เปลี่ยนหน้า) ใน `coordination.py` มี `ui_lock('navigation:' + serial, ...)` อยู่แล้วที่บรรทัด 73
- การบันทึกภาพและ Worker ให้ตรวจเฉพาะ `navigation_snapshot()` ก่อน/หลัง (เทียบ generation + `recovering`) ถ้าเปลี่ยนให้ทิ้งเฟรม ไม่ต้องรอ UI dump

**2.2 Worker ไม่ควรรีเซ็ตเพราะรอ lock ไม่ทัน (`worker/__init__.py:217-219`)**

กรณีรอ lock ไม่ได้ ควร "ข้ามเฟรมนี้โดยไม่ตัดชุดนับ" หรือ "รอแล้วลองใหม่" แทนการตีเป็น safety reason ที่ทำให้ `continuity = False`

**2.3 ยอมให้เฟรมขาดเล็กน้อย หรือเปลี่ยนเป็นนับตามเวลา (ต้องตัดสินใจกติกา)**

ตัวเลือก:

- **ก.** ยอมให้ sequence กระโดดได้ไม่เกิน 1–2 และ gap ไม่เกิน ~3 วินาที โดยไม่ตัดชุด แต่ไม่นับเฟรมที่หายเป็นหลักฐาน
- **ข.** เปลี่ยนเกณฑ์เป็น "เห็น PRESENT ต่อเนื่องอย่างน้อย N วินาที (เช่น 10) และมีเฟรมสดอย่างน้อย M ภาพ (เช่น 7)" — ทนต่อ jitter ของ ADB มากกว่าการนับภาพติดกันเป๊ะ

ทั้งสองแบบยังคงเงื่อนไขภาพสด (clock เปลี่ยน), alignment และ UNKNOWN ตัดชุดเหมือนเดิม

**2.4 ตรวจหน้า Live ไม่ถี่เกินจำเป็น**

พิจารณาเพิ่ม `ui_check_interval_seconds` เป็น 30 วินาที (ตรวจล่วงหน้าทุก 15 วินาที) และยอมให้ผลลบ 1 ครั้ง (transient) ก่อน pause แทนการรีเซ็ต session ทันที

### การทดสอบที่ต้องเพิ่มก่อนแก้ (red → green)

1. Capture: จำลอง UI dump ถือ lock 3 วินาทีระหว่างที่ถึงรอบบันทึกภาพ → ปัจจุบันต้องเห็นเฟรมล่าช้า/session รีเซ็ต; หลังแก้ต้องบันทึกได้ตามรอบ
2. Worker: จำลอง lock busy 1 เฟรมระหว่างชุด PRESENT 10 ภาพ → ปัจจุบันนับใหม่; หลังแก้ต้องถึงเกณฑ์ (หรือถึงเกณฑ์ตามกติกาเวลาใหม่)
3. จำลอง sequence กระโดด 1 ครั้งในชุด 12 ภาพ PRESENT → ระบุพฤติกรรมที่ตกลงไว้
4. ไม่ลด assertion ของกรณีที่ต้องไม่เตือน: ภาพไม่สด, UNKNOWN, navigation generation เปลี่ยน, ข้าม boot epoch

---

## 3. ไม่กู้ตัวเองหลังรีบูต / Tapo ปิด — สูง

### หลักฐาน (ยืนยันแล้ว)

- `capture/__init__.py:136-137` `ensure_live()` ถ้าไม่ใช่หน้า Live และไม่ใช่ Tap to refresh → `CaptureError('Live view not verified; open configured camera manually')` ไม่เปิดแอปหรือเลือกกล้องให้
- `capture/__init__.py:229` `camera_center()` มีโค้ดหาตำแหน่งชื่อกล้องในหน้า Home แต่ **ไม่มีที่ใดเรียกใช้** (grep ทั้ง `smart_tapo/`)
- `capture/__init__.py:73-76` เปิด MEmu ได้ครั้งเดียวต่อ process (`self.started = True`)
- README บรรทัด 28: ไม่ได้ติดตั้ง Windows service/task, ตัวฟังเสียงไม่เปิดเอง (ต้องกดยินยอมทุกครั้ง)

### ผลในการใช้งานจริง

VPS รีบูต, Windows update, Tapo crash หรือ MEmu ปิด → Capture วนแจ้ง "open configured camera manually" จนกว่าคนจะเข้าไปเปิด ระหว่างนั้นไม่มีการตรวจแบงค์เลย

### วิธีแก้

1. **เปิด SmartTapo อัตโนมัติเมื่อ logon** ด้วย Task Scheduler (trigger: At log on ของบัญชีที่ใช้ MEmu) และตั้ง Windows auto-logon หรือใช้ session ที่ค้างไว้ — ไม่ต้องแก้โค้ด
2. **เพิ่มตัวเลือก "เริ่ม Capture/Worker อัตโนมัติเมื่อเปิดโปรแกรม"** ใน config (ค่าเริ่มต้นปิด) — ต้องแก้ `desktop.py`/`desktop_controller.py`
3. **นำทางไปหน้ากล้องเองแบบจำกัดขอบเขต**:
   - `adb shell monkey -p com.tplink.iot 1` หรือ `am start` เพื่อเปิด Tapo เมื่อ foreground ไม่ใช่ Tapo
   - ใช้ `camera_center()` ที่มีอยู่แล้ว กดชื่อกล้องเฉพาะเมื่ออยู่หน้า Home และพบชื่อกล้องเพียงหนึ่งจุด ไม่มีช่องรหัสผ่าน
   - จำกัดจำนวนครั้ง + backoff และทำภายใต้ `navigation_scope` เพื่อให้ Worker/ตัวฟังเสียงตัดหลักฐานช่วงนั้นตามกลไกเดิม
4. **Capture ลองเปิด MEmu ใหม่ได้** หาก ADB หายเกิน N นาที (มี backoff) แทนการเปิดได้ครั้งเดียว

---

## 4. ตัวตรวจหน้า Live เปราะ / Tap to refresh อาจไม่ทำงาน — สูง

### หลักฐาน (โค้ดยืนยันแล้ว)

`smart_tapo/capture/page_mode.py`

- บรรทัด 8: ต้องเป็น activity `com.tplink.iot/.view.ipcamerav3.play.VideoPlayV3Activity` ตรงตัว
- บรรทัด 10-11: ต้องเห็น resource-id ครบ 6 ตัว `live_surface_list_view_container, live_surface_list_view, control_tv, talk_tv, tool_sound, playback_and_download`
- บรรทัด 77-81: ต้องมี node `title` **หนึ่งตัวเท่านั้น** และข้อความต้องเท่ากับ `camera_name` ทุกตัวอักษร
- บรรทัด 82-85: ตรวจ `LIVE_REQUIRED` **ก่อน** ตรวจข้อความ `tap to refresh`
- `capture/__init__.py:213-216` `refresh_center()` จับเฉพาะข้อความภาษาอังกฤษ `tap to refresh`

ข้อดี: ผู้ scout พบว่า dump จริงจาก VPS (`.tools/diagnostic-v11/tapo-live-*.json`) ผ่านเกณฑ์นี้ได้ในภาวะปกติ

### ความเสี่ยง (สมมติฐาน)

- Tapo อัปเดต, popup แจ้งเตือน, เข้าเต็มจอ/แนวนอน, เลื่อนแผงควบคุม → element บางตัวหาย → `incomplete_live_layout` → ตัวตรวจเบื้องหลังให้ผลลบ → Capture รีเซ็ต session (ข้อ 2.จ)
- ถ้าหน้า "Tap to refresh" บังปุ่มควบคุมจน `control_tv/talk_tv/...` ไม่ visible → ได้ `incomplete_live_layout` ก่อนจะถึงบรรทัดตรวจ refresh → **ไม่กด refresh เลย** และ `ensure_live` แจ้งให้เปิดกล้องเอง
- ถ้า Tapo ตั้งภาษาไทย ข้อความ refresh อาจไม่ใช่ภาษาอังกฤษ → ไม่กด
- `docs/refueling-parallel-v13-th.md` บรรทัด 60 ยอมรับว่ายังไม่ยืนยัน Tap to refresh บนเครื่องจริง

### วิธีแก้

1. **ย้ายการตรวจ `tap to refresh` ขึ้นไปก่อน `LIVE_REQUIRED`** (หลังยืนยัน activity + ชื่อกล้องแล้ว) เพื่อให้กู้ได้แม้ overlay บังปุ่ม
2. **ลด `LIVE_REQUIRED` ให้เหลือชุดที่จำเป็น** เช่น `live_surface_list_view` + `title` + activity ถูกต้อง + ไม่มี Playback veto ส่วน element อื่นให้เป็นหลักฐานเสริม ไม่ใช่เงื่อนไขบังคับ
3. **รองรับข้อความ refresh หลายภาษา** — ขอ dump จริงตอนขึ้น refresh จากเครื่อง VPS (ทั้งภาษาไทย/อังกฤษ) ก่อนเพิ่มคำ ไม่เดาข้อความ
4. **บันทึก reason ของผลลบลง log** (`incomplete_live_layout`, `camera_not_confirmed`, …) ทุกครั้งที่ตัวตรวจเบื้องหลังให้ผลลบ เพื่อเห็นจาก log จริงว่าติดเพราะอะไร (ปัจจุบัน `live_verifier.py:27` โยนข้อความรวมว่า "Live view lost")
5. เก็บ fixture XML จริงที่ sanitize แล้ว 3 กรณี: Live ปกติ, Live + Tap to refresh, Live + popup แล้วเขียน regression test

---

## 5. commit ไม่ครบไฟล์ → build พัง — สูง (ตอนส่งมอบ)

### หลักฐาน (ยืนยันแล้ว โดย verifier)

- HEAD `6f325c4` build/test ได้ตามเดิม (90 tests ผ่านใน checkout ชั่วคราว)
- แต่ไฟล์ที่ track และถูกแก้ใน working tree ตอนนี้ import โมดูลที่ **ยังไม่ได้ track** เช่น
  - `smart_tapo/frames.py:10` → `.clock_epoch`
  - `smart_tapo/capture/__init__.py:15-18` → `live_verifier`, `page_mode`, `ui_lock`, `coordination`
  - `smart_tapo/desktop_controller.py:14,17` → `audit_log`, `diagnostics`
  - `smart_tapo/worker/__init__.py:23-28` → `clock_epoch`, `capture.coordination`, `diagnostics`
  - `smart_tapo/desktop.py:26-27` → `desktop_audio`, `desktop_refueling`
- คัดลอกเฉพาะไฟล์ที่ track (เนื้อหาปัจจุบัน) ไปทดลอง → ทุกโมดูลหลัก import ไม่ได้
- `.github/workflows/build-windows-exe.yml` และ `scripts/build-windows.ps1` จะพังที่ขั้น unittest/PyInstaller

### วิธีแก้

- commit ไฟล์ใหม่ทั้งหมดใน `smart_tapo/**/*.py`, `tests/*.py`, `docs/*` ที่เกี่ยวข้องพร้อมไฟล์ที่แก้ ใน batch เดียว
- หลัง commit ให้ clone/checkout ใหม่ในโฟลเดอร์ชั่วคราวแล้วรัน `python -m unittest discover -s tests` เพื่อยืนยันว่าไม่พึ่งไฟล์ที่ไม่ได้ track
- **ห้าม** commit `config.local.json`, `empty.png` (ถ้าเป็นข้อมูลส่วนตัว), `.tools/`, ไฟล์เสียง หรือ `pushover.dat`
- พิจารณาเพิ่มขั้น CI ที่ตรวจว่า import ทุกโมดูลได้จาก checkout สะอาด

---

## 6. ฟีเจอร์ที่ยังไม่ได้ต่อเข้าแอป — กลาง

### หลักฐาน (ยืนยันด้วย grep)

| สิ่งที่ผู้ใช้อาจคาดหวัง | สถานะจริง |
|---|---|
| ส่งรอบเติม/รูปเข้า webhook/dashboard | `RefuelingWebhookClient` (`refueling_webhook.py:76`) ถูกใช้เฉพาะใน `refueling_smoke.py:12,31` |
| ผูกรูปกับรอบเติมจากเสียง | `add_evidence` (`refueling_store.py:324`) ถูกเรียกเฉพาะ `refueling_smoke.py:27` |
| แจ้งเตือนจากเสียงประกาศ | ไม่มีการเชื่อม refueling store → Worker/Pushover |
| Pushover แนบรูป / Emergency acknowledgement | ไม่รองรับ (README บรรทัด 73) ส่งข้อความ priority 1 เท่านั้น |
| เปิดเองอัตโนมัติ / service | ไม่มี (ดูข้อ 3) |

### วิธีแก้

แนะนำให้ **เลื่อนไปทำหลังข้อ 1–4 ผ่านบนเครื่องจริงแล้ว** เพราะฟีเจอร์เสียงพึ่งการยืนยันหน้า Live และ lock ตัวเดียวกัน ถ้าฐานยังไม่นิ่งจะเพิ่มปัญหา เมื่อพร้อม:

1. ยืนยันสเปก API ปลายทาง (มีร่างใน `docs/refueling-webhook-api-th.md`) และ URL/token จริงกับเจ้าของ dashboard
2. เพิ่มหน้าตั้งค่าปลายทาง (เก็บ token ด้วย DPAPI แบบเดียวกับ Pushover)
3. เพิ่ม sender loop ใน process แยก (หรือใน Worker) อ่าน outbox → ส่ง → บันทึก receipt
4. ผูกรูป: เลือกภาพจาก spool ของ Capture ในช่วงเวลารอบเติม (ต้องตกลงกติกาว่าเอาภาพช่วงไหน)

---

## 7. Quota ภาพ 512 MB อาจเต็ม — กลาง (สมมติฐาน)

### หลักฐาน

- `smart_tapo/frames.py:107` `image_storage_bytes()` สแกนไฟล์ทั้งหมดใน `frames/` และ `events/` ทุกรอบ Capture (`capture/__init__.py:319`)
- ถ้าเกิน quota: Capture log `Image quota full; publishing paused` และหยุดส่งภาพ (`capture/__init__.py:320`)
- `config.local.json` ตั้ง `deposit_history_hours = 48` (ค่าเริ่มต้น 24) เหตุการณ์ปกติแต่ละครั้งเก็บภาพเต็ม `first_full`

### ความเสี่ยง

จุดที่มีการหยอดบ่อย + เก็บ 48 ชั่วโมง อาจทำให้ภาพเหตุการณ์สะสมจนชน 512 MB แล้ว Capture หยุดส่งภาพ (ไม่ได้วัดจริง) และการสแกนโฟลเดอร์ทุกวินาทีจะช้าลงเมื่อไฟล์เยอะ

### วิธีแก้

- ดูขนาดจริงของ `<data_dir>/frames` และ `<data_dir>/events` บน VPS และค้นใน log หา `Image quota full`
- ถ้าใกล้เต็ม: ลด `deposit_history_hours` หรือเพิ่ม `max_image_storage_mb` ตามพื้นที่ดิสก์
- ระยะยาว: เก็บยอดรวมไว้ในหน่วยความจำ/SQLite แทนการสแกนทุกรอบ และบีบอัดภาพ `first_full` เป็น JPEG

---

## 8. เสียงบน VPS — กลาง (สาเหตุยังไม่พิสูจน์)

### หลักฐาน

- `VERIFICATION.md` บรรทัด 15: ใต้ RDP เห็นเฉพาะ Remote Audio, ใต้ VNC เห็น CABLE และมิเตอร์ขยับ
- `VERIFICATION.md` บรรทัด 19: รอบจริงจบด้วย `stream_fault` สาเหตุย่อยยังไม่พิสูจน์
- `docs/refueling-parallel-v13-th.md` บรรทัด 54: ยังไม่ตรวจว่าหลังปิด VNC viewer เสียงยังมาอยู่หรือไม่

### วิธีแก้

1. ใช้ VNC/Console เท่านั้น ห้ามเข้า RDP ด้วยบัญชีเดียวกันระหว่างเฝ้า
2. ทดสอบปิด VNC viewer 30 นาทีแล้วดู log ว่ายังได้แพ็กเก็ตเสียง/ไม่มี `stream_fault`
3. เก็บ log `stream_fault` พร้อมรหัสรายขั้นที่ .12 เพิ่มไว้ ส่งกลับมาวิเคราะห์ก่อนแก้โค้ดเพิ่ม
4. ถ้าต้องการให้ภาพทำงานได้โดยไม่ขึ้นกับเสียง: แยกการเปิดตัวฟังเสียงออกจากการตรวจแบงค์ค้างให้ชัด (ปัจจุบันใช้ UI lock ร่วมกัน ดูข้อ 2)

---

## 9. เอกสาร/คำสั่งไม่ตรงไฟล์จริง — ต่ำ

### หลักฐาน (ยืนยันแล้ว)

- README ส่วนที่ 2 และ 7 อ้าง `tapo-screen.png` แต่ไฟล์ไม่มีในโปรเจกต์และไม่เคยถูก track คำสั่ง simulation จึงล้มด้วย `FileNotFoundError`
- ใช้ `empty.png` แทนได้ผลตรงตามที่ README ระบุ: normal 1, stuck 1, shadow notifications 2 ที่ frame 17 และ 77, `real_notifications: 0`
- README บนสุดซ้อนบันทึกรุ่น .8–.13 เป็นย่อหน้ายาวที่ไม่มีเว้นวรรค อ่านยากสำหรับผู้ใช้หน้างาน

### วิธีแก้

- เปลี่ยนตัวอย่างคำสั่งเป็น `empty.png` หรือเพิ่มภาพตัวอย่างสังเคราะห์ที่ไม่มีข้อมูลส่วนตัว
- แยกประวัติรุ่นออกจาก README ไปไว้ `CHANGELOG`/`VERIFICATION.md` และให้ README เหลือขั้นตอนใช้งานรุ่นปัจจุบัน

---

## ความซับซ้อนที่ทำให้แก้ยาก (ข้อสังเกต)

- ไม่มีไฟล์เกิน 1,000 บรรทัด (ใหญ่สุด `desktop.py` 823 บรรทัด) แต่ไฟล์ `desktop_*.py` มีบรรทัดยาวเกิน 300 ตัวอักษรหลายจุด อ่าน/รีวิวยาก
- การตรวจ "อยู่หน้า Live หรือไม่" กระจายหลายที่: Capture (`ensure_live`, `read_page_mode`, `recover_live`, ตัวตรวจเบื้องหลัง), Worker (navigation snapshot), `continuous_listener.py` และ `coordination.py`
- มีกลไกประสานสามชั้น: mutex UI dump, mutex navigation, และไฟล์ fence JSON — ทุกงานผูกกันด้วย lock เดียว ทำให้ปัญหาหนึ่งส่งผลไปทุกงาน
- ระบบออกแบบ "fail closed" ทุกจุด ซึ่งปลอดภัยต่อการแจ้งเตือนผิด แต่เมื่อซ้อนกันหลายชั้น ผลรวมคือ **แทบไม่มีเส้นทางที่แจ้งเตือนได้** บนเครื่องจริงที่มี jitter

ข้อเสนอ: เมื่อแก้ข้อ 2 ให้ถือโอกาสรวมการตัดสิน "หลักฐานยังใช้ได้หรือไม่" ไว้ที่โมดูลเดียว และบันทึกเหตุผลที่ตัดชุดทุกครั้งเป็น code เดียวกันใน log

---

## ลำดับการทำงานที่แนะนำ

### ขั้นที่ 0 — ทำได้ทันทีบน VPS (ไม่ต้องแก้โค้ด)

1. ตรวจว่า VPS รันรุ่นอะไร (ข้อความเริ่ม Capture/Worker ต้องมี `diagnostics 2026-10-30.13` หรือรุ่นที่ติดตั้ง)
2. เปลี่ยน ROI เป็น `249, 272, 100, 80` และยืนยันภาพช่องว่างใหม่ (ข้อ 1)
3. เปิดเฉพาะ Capture + Worker (ยังไม่เปิดตัวฟังเสียง) อย่างน้อย 30 นาที ให้มีช่วงแบงค์ค้างจริงหรือจำลองด้วยกระดาษ
4. ส่งกลับ: `<data_dir>/logs/capture-*.jsonl`, `<data_dir>/logs/worker.jsonl` และเวลาที่แบงค์ค้างจริง
5. ตั้ง Task Scheduler เปิด SmartTapo เมื่อ logon (ข้อ 3.1)

### ขั้นที่ 1 — แก้โค้ดที่กระทบมากที่สุด

1. ข้อ 2.1–2.2: แยก lock การบันทึกภาพ/Worker ออกจาก UI dump และ Worker ไม่รีเซ็ตเพราะ lock busy
2. ข้อ 2.3: ตกลงกติกาการนับ (ยอม gap เล็กน้อย หรือ นับตามเวลา) แล้วแก้ Worker
3. ข้อ 4.1, 4.4: ย้ายการตรวจ Tap to refresh ขึ้นก่อน และ log reason ของผลลบ
4. เขียน regression test ที่ล้มก่อนแก้ทุกข้อ แล้วรันชุดเต็มผ่าน `.tools/run_local_checks.py`

### ขั้นที่ 2 — ทดสอบบนเครื่องจริง

1. build EXE ใหม่, ใช้ log จาก VPS ยืนยันว่า gap ลดลงและนับถึงเกณฑ์ได้เมื่อแบงค์ค้าง
2. ทดสอบหยอดปกติหลายครั้งเพื่อดูว่าไม่เตือนผิด (`notify_enabled=false` ก่อน)
3. เมื่อผ่าน shadow mode แล้วจึงเปิด Pushover จริง

### ขั้นที่ 3 — ความทนทานและฟีเจอร์ต่อ

1. ข้อ 3.2–3.4: เริ่มงานอัตโนมัติ, นำทางไปกล้องเอง, เปิด MEmu ใหม่
2. ข้อ 4.2–4.3, 4.5: ผ่อนเกณฑ์หน้า Live, รองรับข้อความหลายภาษา, fixture จริง
3. ข้อ 5: commit ครบไฟล์และตรวจจาก checkout สะอาด
4. ข้อ 6: ต่อ webhook/ผูกรูป หลังส่วนภาพนิ่งแล้ว

---

## ข้อมูลที่ยังขาดและต้องขอจาก VPS

| ข้อมูล | ใช้ตัดสินเรื่อง |
|---|---|
| รุ่น EXE ที่รันจริง | ผลวิเคราะห์ทุกข้อใช้กับรุ่นไหน |
| `capture-*.jsonl` / `worker.jsonl` หลังรุ่น .5 ช่วงที่มีแบงค์ค้าง | ยืนยันข้อ 2 ว่า gap/รีเซ็ตยังเกิดและเพราะอะไร |
| ROI/ภาพอ้างอิงที่ใช้จริงบน VPS | ยืนยันข้อ 1 ว่ายังเป็น 131×104 หรือไม่ |
| UI dump ตอนขึ้น Tap to refresh และตอนมี popup | แก้ข้อ 4 โดยไม่เดา |
| ขนาดโฟลเดอร์ `frames/` และ `events/` | ประเมินข้อ 7 |
| log `stream_fault` และสภาพ VNC/RDP ขณะเกิด | ประเมินข้อ 8 |

---

## ผลการตรวจที่รันจริงในรอบนี้

| คำสั่ง | ผล |
|---|---|
| `.venv\Scripts\python.exe .tools\run_local_checks.py` | exit 0 — 383 tests: ผ่าน 382, ข้าม 1 (`test_no_plaintext_fallback`, Non-Windows contract), ล้ม 0 |
| `.venv\Scripts\python.exe -m pip check` | exit 0 — No broken requirements |
| `.venv\Scripts\python.exe -m compileall -q smart_tapo tests entrypoints` | exit 0 |
| `git diff --check` | exit 0 (ตรวจเฉพาะไฟล์ที่ track) |
| `python -m smart_tapo.simulate --config config.example.json --image tapo-screen.png` | exit 1 — ไม่มีไฟล์ `tapo-screen.png` |
| `python -m smart_tapo.simulate --config config.example.json --image empty.png` | exit 0 — normal 1, stuck 1, shadow 2 ที่ frame 17/77, real_notifications 0 |
| import เฉพาะไฟล์ที่ track (เนื้อหาปัจจุบัน) ในโฟลเดอร์ชั่วคราว | ล้ม — ขาด `clock_epoch`, `capture.live_verifier`, `audit_log` |

ไม่มีการแก้ source, ไม่เชื่อมอุปกรณ์/เครือข่ายจริง, ไม่ commit/push
