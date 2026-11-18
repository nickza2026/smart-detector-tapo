# Notify API client (ทดสอบออฟไลน์เท่านั้น)

โมดูล `smart_tapo.notify_api` ใช้ Python standard library ไม่มี dependency ใหม่ และไม่ได้เชื่อมต่อ API จริงระหว่างการพัฒนา/ทดสอบนี้ ไม่ได้เผยแพร่หรือ deploy

## Interface สำหรับ integration

- `DEFAULT_BASE_URL` คือ public HTTPS URL ตามสัญญา API ที่ผู้ใช้แนบ
- `NotifyTarget(base_url, key)` เป็น frozen dataclass; URL ต้องเป็น HTTPS ไม่มี userinfo/query/fragment; key เป็น opaque secret ไม่ตีความเป็น Pushover key และไม่ปรากฏใน repr (URL ก็ไม่แสดงใน repr)
- `AlertMessage(alert_id, idempotency_key, title, message, importance, ttl_seconds=300)` เป็น frozen dataclass ผู้เรียกสร้าง ID ที่คงเดิม **ครั้งเดียวต่อเหตุการณ์** และต้องระบุ `importance` เป็น `normal` หรือ `critical` เอง ไม่มี default critical
- `NotifyResult(state, code, retry_after=None, retryable=False)` เป็น frozen dataclass; state คือ `success`, `failed`, `ambiguous`; code เป็น allowlist เท่านั้น ไม่มี exception/provider body/URL/key ดิบ
- `send_alert(target, alert, cancel, *, opener=None)` ส่ง POST หนึ่งครั้งเท่านั้น; opener สำหรับทดสอบต้องมี `open(request, timeout=10)`
- `deliver_alert(target, alert, cancel, *, sender=send_alert, clock=time.monotonic, attempts=3, max_elapsed=90)` retry เหตุการณ์เดิม ไม่สร้าง ID ใหม่

`cancel` ต้องมี `is_set()` และ `wait(seconds)` เช่น `threading.Event` หากยกเลิกก่อนเปิด connection จะไม่ POST แต่หลังเริ่มส่งแล้วไม่สามารถย้อนคืนการส่งได้ ผลระหว่าง retry จึงอาจเป็น ambiguous

## ข้อจำกัดและความหมาย

ID ตรง regex ตามสัญญา: alert ID 1–100 `[A-Za-z0-9_-]`, idempotency key 1–128 `[A-Za-z0-9_.:-]`; title ไม่ว่างไม่เกิน 120 code points, message ไม่ว่างไม่เกิน 2,000 code points; TTL เป็น integer (ไม่รับ bool) 30–10800 วินาที ไม่มีการตัดข้อความ/เปลี่ยน whitespace

เข้ารหัส JSON UTF-8 ครั้งเดียว สูงสุด 16,384 bytes และ reuse bytes เดิมตลอด retry ส่วนข้อจำกัด payload มือถือ 4,000 bytes เป็นการตรวจของ server (422 ไม่ retry) ไม่ใช่เหตุให้ client ตัดข้อความ

คีย์ต้องไม่ว่าง ไม่เกิน 4,096 ASCII printable characters ไม่มี header controls เป็นเพดานทรัพยากรของ client ไม่ใช่การเดารูปแบบคีย์ provider

แต่ละ POST timeout 10 วินาที อ่าน response สูงสุด 65,537 bytes เพื่อตรวจเพดาน 65,536 bytes ไม่ตาม redirect จึงไม่ส่ง Authorization ต่อไป URL ใหม่

202 สำเร็จเฉพาะ receipt JSON ที่ตรง ID/importance, status `provider_accepted`, acknowledged และ delivery_verified เป็น boolean false จริง และ expires_at เป็น UTC RFC3339 ลงท้าย Z รองรับเศษวินาทีหลายหลัก รับ replay ที่หมดอายุแล้วด้วย อนุญาต extra response fields ที่ไม่รู้จัก

**success หมายถึง provider รับคำขอเท่านั้น ไม่ยืนยันว่ามือถือได้รับ ส่งเสียง หรือผู้ใช้รับทราบ** ไม่มี GET polling, Ack, mute, cancel หรือ stop alarm endpoint ใน client นี้ และ recovery จากเครื่องต้นทางไม่ใช่การหยุดเสียง

502 ผลที่บันทึกแล้วไม่ retry: provider_failed เป็น failed, delivery_unknown เป็น ambiguous; 409 ไม่เปลี่ยน key เพื่อบังคับส่งใหม่; 400/401/403/413/415/422 เป็น terminal

429, 503 และ network uncertainty เท่านั้นที่ client สร้างผล retryable; wrapper ส่งได้สูงสุด 3 ครั้ง backoff 2/4 วินาที รอค่าสูงกว่าระหว่าง backoff กับ Retry-After (decimal seconds) ผ่าน cancel.wait ไม่ใช้ sleep/spin หากเวลารอรวม timeout ของ attempt ถัดไปเกิน budget จะคืน retry_budget_exhausted ไม่ลดเวลารอเพื่อ POST ก่อน server อนุญาต ตัวเลข Retry-After ที่ยาวผิดปกติยังห้าม resend ภายใน budget; ค่าที่ไม่ใช่ decimal ใช้ bounded backoff

attempts รับ integer 1–3 และ max_elapsed รับเลข finite 1–90 วินาที (ไม่รับ bool) เป็น soft retry budget ไม่สามารถ interrupt opener ที่กำลังทำงานได้; attempt แรกยังใช้ timeout 10 วินาทีตามสัญญา ส่วน attempt ถัดไปต้องเหลืองบพอ timeout เต็ม การหมด budget หลังอาจส่งแล้วคืน ambiguous โดยไม่อ้างว่าส่งถึงหรือไม่ได้ส่ง

## การใช้งานในแอป (แทน Pushover)

- **ตั้งค่า**: `notify_api_url` และ `notify_importance` (`normal`/`critical`, ค่าเริ่มต้น `normal`) อยู่ใน config ส่วน API key เก็บด้วย DPAPI ใน `notify-api.dat` ผูกกับ Base URL และ `camera_id`; เปลี่ยน URL หรือกล้องแล้วต้องบันทึกคีย์ใหม่ คีย์ Pushover เดิมไม่ถูกอ่านหรือลบ
- **Worker (วัตถุค้าง)**: ส่งแบบ `critical` เสมอ (ปลุกต่อเนื่องจนกดรับทราบในมือถือ) ไม่ขึ้นกับ `notify_importance` ซึ่งใช้กับ health alerts เท่านั้น controller ส่งคีย์ให้ process ลูกผ่าน `NOTIFY_API_KEY` เท่านั้น (ล้างค่าที่สืบทอดมาก่อนเสมอ) `alert_id` คือ ID แถว `notifications` และ `Idempotency-Key` คือ `smarttapo:stuck:<id>`; `deliver_alert` ลองซ้ำเหตุการณ์เดิมได้ไม่เกิน 3 ครั้งใน 90 วินาที `Worker.close()` ยกเลิกการรอ backoff ถ้าส่งไม่สำเร็จ Worker สร้างแถวแจ้งเตือนใหม่ (ID ใหม่) หลัง 60 วินาทีตามกติกาเดิม
- **Health alerts**: `send_health(key, base_url, notice, cancel, importance)` ส่ง POST ครั้งเดียว ID คงที่ต่อ notice ส่วน recovery ใช้ `normal` เสมอ
- **ปุ่มทดสอบ**: `NotifyTestPanel` ส่ง `normal` หนึ่งรายการหลังผู้ใช้ยืนยัน
- CLI `smart-tapo-worker` อ่านคีย์จาก environment variable `NOTIFY_API_KEY` เมื่อ config เปิด `notify_enabled`

## ตรวจสอบ

รันเฉพาะ:

```powershell
.tools/v45-build-env/Scripts/python.exe -m unittest tests.test_notify_api tests.test_notify_credentials tests.test_desktop_notify_test tests.test_desktop_health_alerts tests.test_worker
```

ทุก test ใช้ fake opener/sender/event/clock และมี guard ห้าม fallback เปิด transport จริง ไม่มีการใช้ key จริงหรือส่งแจ้งเตือนไปมือถือ
