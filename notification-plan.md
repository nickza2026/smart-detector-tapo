# แผนแจ้งเตือน Pushover และการรับทราบ

สถานะชุดพัฒนา: เพิ่มตัวเลือก priority-2 Emergency พร้อม receipt/ack/cancel แบบ polling และ durable recovery แล้ว ทดสอบ API จำลอง ไม่เรียก Pushover จริง ไม่เผยแพร่ exe ชุดนี้เอง High/default config เก่ายังคง priority 1; เลือก Emergency ใน GUI อย่างชัดเจน ดู `README.md` และ `VERIFICATION.md`

กติกา Emergency ที่ใช้: ack แล้วเงียบจนเหตุจบ, clear ยืนยันแล้วขอ cancel, expired/cancelled ไม่ใช่ ack และไม่เปิดรอบใหม่เองในเหตุเดิม หยุดโปรแกรมไม่ยกเลิก cloud โดยอัตโนมัติ สถานะส่งที่ไม่รู้ receipt ระงับส่งใหม่ทั้งสองโหมดและเก็บหลักฐานไว้ ไม่มีการเดา expiry จากนาฬิกาเครื่องและไม่มี silent fallback/ปุ่ม force-resolve รายละเอียด retry/expire และเพดาน 50 retries อยู่ด้านล่าง

## 1. จำนวนภาพต้องต่อเนื่องจริง

- Capture เป้าหมายทุก 1 วินาที และ count เฉพาะภาพใหม่ที่ยืนยัน BANKNOTE ผ่าน freshness/quality
- ค่าเริ่มต้นที่ผู้ใช้ยืนยันคือ `stuck_consecutive_frames=10` ปรับได้
- ตัวอย่าง: ภาพ 1–5 เห็นแบงค์ → count=5; ภาพ 6 แบงค์หาย → count=0 และ **ไม่แจ้งเตือน**; ถ้าภาพ 7 กลับมาเห็นแบงค์เริ่ม count=1 ใหม่ และครบ 10 ภาพชุดใหม่ที่ภาพ 16 จึงเข้าเกณฑ์
- UNKNOWN/STALE/มือบังจนระบุไม่ได้/ภาพขาด/reconnect ก็ตัดช่วงยืนยัน ไม่เอา count สองช่วงมารวมกัน
- ภาพซ้ำจากการ poll ไม่เพิ่ม count ส่วนแบงค์ที่นิ่งแต่ปรากฏในเฟรมสดใหม่ทุกครั้งให้นับต่อได้

## 2. ข้อกำหนดจากผู้ใช้และข้อจำกัด API

ผู้ใช้ต้องการไม่ส่งซ้ำก่อนกดรับทราบใน Pushover ถ้าระบบเช็กการรับทราบไม่ได้ อนุญาตให้ตรวจใหม่อีก 1 นาทีและส่งซ้ำเฉพาะเมื่อแบงค์ยังค้างเป็นเหตุเดิม

ตรวจจากเอกสารทางการ:

- [Message API — Message Priority](https://pushover.net/api#priority): Emergency (`priority=2`) มีปุ่มรับทราบและคืน receipt แต่ **Pushover เตือนซ้ำเองจนรับทราบหรือหมดอายุ** ต้องระบุ retry และ expire; retry อย่างน้อย 30 วินาที, expire ไม่เกิน 10,800 วินาที และมีเพดาน 50 retries
- [Receipts API](https://pushover.net/api/receipts): อ่าน `acknowledged`, `acknowledged_at`, `expired` จาก receipt ได้ โดย polling ไม่ถี่กว่า 5 วินาที หรือใช้ callback URL ที่เข้าถึงได้จากอินเทอร์เน็ต
- ใช้ worker poll receipt ได้ ไม่จำเป็นต้องเปิด inbound port/public webhook บน Windows หากเลือกใช้โหมดนี้
- High priority (`priority=1`) ไม่มี Emergency receipt สำหรับเช็ก acknowledgement จึงไม่อาจอ้างว่าเปิดอ่าน/กดรับทราบแล้วจากผลส่งสำเร็จ

ดังนั้นห้ามอ้างว่า Emergency ให้ทั้ง acknowledgement และไม่เตือนซ้ำก่อน acknowledgement ได้โดยไม่มีข้อจำกัด ส่วน receipt cancel/expire ไม่ใช่การรับทราบโดยมนุษย์

## 3. แนวทางต้นแบบที่แนะนำ: โปรแกรมคุม reminder ตาม fallback

ใช้ High priority (`priority=1`) และให้ worker คุมการส่ง จึงไม่มี Pushover Emergency retry อีกชุดมาซ้อนกับการส่งของโปรแกรม นี่เป็นข้อเสนอสำหรับต้นแบบตาม fallback ที่ผู้ใช้อนุญาต ไม่ใช่การอ้างว่าตรวจรับทราบได้

1. ครบ 10 ภาพต่อเนื่อง → สร้าง initial alert ของ event นั้นครั้งเดียว
2. บันทึกผลส่งและ `last_successful_send_at` ไม่ถือ HTTP/API error หรือ timeout ว่าส่งสำเร็จ
3. รออย่างน้อย `reminder_interval_seconds=60` ซึ่งปรับได้
4. เมื่อถึงกำหนด ต้องมี **ภาพสดใหม่** ของกล้อง/ROI/session ที่ถูกต้อง และยังยืนยันธนบัตรต่อเนื่องครบจำนวนที่กำหนดในเหตุเดิม จึงส่ง reminder พร้อมหลักฐานล่าสุด
5. ไม่ส่งซ้ำทุกเฟรม และไม่เอาภาพล่าสุดเมื่อหนึ่งนาทีก่อนมาส่งโดยไม่ตรวจภาพใหม่
6. แบงค์หาย → หยุด reminder/reset count ทันที; ยืนยันช่องว่างตาม clear policy เพื่อปิดเหตุการณ์และเปิดเหตุใหม่โดยไม่เพิ่มประวัติซ้ำ
7. ภาพไม่สด/UNKNOWN/worker ไม่ทัน → งด reminder และตัด count จนกลับมามีหลักฐานใหม่ต่อเนื่อง ไม่แกล้งยืนยันว่าแบงค์ยังติด
8. หลัง restart เก็บสถานะการส่งเดิมและ cooldown ไม่ส่ง initial ซ้ำทันทีเพียงเพราะเริ่มโปรแกรมใหม่; ต้องยืนยันภาพสดใหม่และตรวจสถานะส่งก่อนดำเนินต่อ
9. Network retry เมื่อส่งล้มเหลวแยกจาก reminder ที่ส่งสำเร็จแล้ว ไม่ retry ถี่จนเกิด spam และต้องเปิดเผยความไม่แน่นอนหาก request timeout ทั้งที่ผู้ให้บริการอาจรับข้อความแล้ว
10. คำว่าแบงค์/ภาพเดิมหมายถึง **เหตุการณ์ค้างเดิม** ไม่ใช่ต้องให้ไฟล์หรือพิกเซลเท่ากัน เพราะ noise/แสงอาจเปลี่ยนและ frame ID ต้องใหม่

ตัวอย่าง config ที่เสนอ ยังไม่มีโปรแกรมจริงอ่านค่านี้:

```json
{
  "capture_interval_seconds": 1,
  "stuck_consecutive_frames": 10,
  "notifications": {
    "mode": "worker_reminder",
    "pushover_priority": 1,
    "reminder_interval_seconds": 60
  }
}
```

## 4. ถ้าต้องการรับทราบจาก Pushover จริง

- ต้องเลือก Emergency และยอมรับพฤติกรรม retry/expire ของ Pushover อย่างชัดเจน ไม่ตั้งค่าแทนผู้ใช้โดยเงียบ ๆ
- เก็บ receipt ผูกกับ event และ poll ตามอัตราที่เอกสารอนุญาต; ack เป็นอีกสถานะหนึ่ง แยกจาก BANKNOTE/CLEAR เพราะรับทราบไม่ได้แปลว่าเอาแบงค์ออกแล้ว
- ไม่เปิด worker reminder ซ้ำซ้อนกับ Emergency retry
- หากช่องว่างแล้ว ให้ยกเลิก retry ของ receipt ที่ยัง active และตรวจผล cancel; หาก API ยกเลิกไม่สำเร็จต้องแสดงสถานะ pending/error เพราะแจ้งเตือนฝั่ง cloud อาจยังดำเนินต่อ ไม่รับประกันหยุดทันที
- หากเช็ก receipt ไม่ได้เพราะ network error ห้ามเปลี่ยนเป็น fallback แล้วส่งใหม่ทันทีขณะที่ Emergency เดิมอาจ retry อยู่ ต้องยืนยัน/ยกเลิก notification เดิมก่อนเปลี่ยนโหมด
- API poll สำเร็จแต่ `acknowledged=0`, receipt หมดอายุ และ API poll ล้มเหลวเป็นคนละสถานะ ห้ามนับรวมเป็นผู้ใช้รับทราบ
- ใช้พฤติกรรมที่เสนอและผู้ใช้ให้ทำโหมดนี้ต่อ: หลังรับทราบ ถ้าวัตถุยังอยู่ให้เงียบจนเหตุจบ ไม่เริ่มรอบใหม่เอง ส่วน expired/cancelled แสดงแยก ไม่ปลอมเป็นรับทราบ

## 5. การเก็บข้อมูล

- เก็บ event ID, count/threshold, first sent/last successful sent, attempt/reminder ID, delivery result, mode และ receipt/ack/expire/cancel status ถ้ามี เพื่อไม่ส่งซ้ำผิดรอบหลัง restart
- ภาพและ record ของ pending send/retry/active receipt ต้องไม่ถูกลบตาม TTL ทั่วไปก่อนหมดภาระงาน ตาม `retention-plan.md`
- ไม่ใส่ token/user key ลงโค้ดหรือ log; เอกสารนี้ไม่เก็บ credentials และไม่มีการส่ง Pushover จริง

## 6. ผลจำลองเพิ่มเติม

รัน Python แบบ in-memory ด้วย labels สมมติ ผ่าน 4/4 กรณี:

1. ภาพ 1–5 เป็น BANKNOTE ภาพ 6 EMPTY: count=0 ไม่มี alert; กลับมาเป็น BANKNOTE ตั้งแต่ภาพ 7 จนครบชุดใหม่ จึงเกิด initial alert ที่ภาพ 16
2. BANKNOTE ต่อเนื่อง 131 ภาพที่ 1 Hz: fallback เกิดที่ภาพ 10, 70 และ 130 คือเว้น 60 วินาทีระหว่างรายการส่งสมมติ ไม่ส่งทุกเฟรม
3. ภาพ 70 ซึ่งตรงกำหนด reminder กลับว่าง: ไม่ส่ง reminder และหยุดการยืนยันค้าง
4. ภาพ 70 กลายเป็น UNKNOWN และไม่มีหลักฐานใหม่: ไม่ส่ง reminder จากภาพเก่า

ยังไม่ทดสอบ Pushover receipt/ack/cancel จริง ความล้มเหลวเครือข่าย การกู้ cooldown หลัง restart หรือ cleanup งานส่ง; ผลนี้ไม่ใช่การรับรอง native acknowledgement mode
