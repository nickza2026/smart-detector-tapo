# Refueling API v1 — สัญญาฝั่งรับข้อมูลและรูป

เอกสารนี้อธิบายรูปและ JSON ของรอบที่มีจุดเริ่ม สำหรับหลักฐานเสียงที่ยังจับคู่เป็นรอบไม่ได้ ใช้สัญญาแยก [หลักฐานเสียงข้อมูลไม่ครบ](refueling-observations-api-th.md)

## ทิศทางและสิทธิ์

โปรแกรมWindowsส่ง **รูปก่อน แล้วส่งJSONของรอบ** ออกไปยังHTTPSของdashboard ไม่เปิดpublic HTTPบนเครื่องเฝ้าตู้ และไม่ส่งWAV/เสียงสนทนา

- ตั้งค่าbase URL เช่น `https://dashboard.example/api`; clientต่อpathด้านล่างให้ ไม่มีquery/fragment/userinfo/tokenในURL
- ทุกการเขียนใช้ `Authorization: Bearer <application-key>` และ `X-Camera-ID: <camera_id>` ฝั่งรับต้องผูกkeyกับกล้องที่อนุญาต ไม่เชื่อcamera_idจากbodyอย่างเดียว
- ไม่ส่งapplication-keyไปFE/browser ไม่ใส่key/localpathลงbody/log/error การอ่านรูปในdashboardใช้สิทธิ์ผู้ใช้dashboard/BFF ไม่ใช้keyของเครื่องเฝ้า
- ทั้งsession_id/event_id/image_idต้องscopeตามเจ้าของและกล้อง idเหมือนกันต่างเจ้าของห้ามชนหรืออ่าน/เขียนข้ามกัน
- HTTPSตรวจcertificateตามปกติ ไม่ตามredirectและไม่ส่งauthต่อให้Location ใหม่ HTTPอนุญาตเฉพาะliteral loopbackในtest modeที่เลือกชัดเจน

## 1. PUT `/v1/refueling/images/{image_id}`

อัปโหลดทีละไฟล์ เพื่อลดขนาดrequestและไม่อัปโหลดทั้งชุดใหม่เมื่อJSONต้องretry

Headers:

```text
Authorization: Bearer <private application-key>
X-Camera-ID: cabinet-1
Idempotency-Key: <image_id>
X-Content-SHA256: <lowercase SHA-256 hex 64 ตัว>
Content-Type: image/png หรือ image/jpeg
```

Bodyเป็นbytesภาพจริง ไม่ใช่base64/multipart/path รูปไม่เกิน2MiB/4ล้านpixels และต้องdecodeเป็นPNG/JPEGได้ จำกัดpixel countฝั่งรับด้วย ไม่รับSVG/GIF/ชื่อไฟล์ที่กลายเป็นpath

- image_idเป็นUUIDhexเล็ก32ตัว กำหนดครั้งเดียวในเครื่อง ไม่สร้างใหม่เมื่อretry
- ฝั่งรับตรวจhash/mime/ขนาด/เจ้าของกล้อง เก็บรูปให้durableก่อนตอบ
- unique `(เจ้าของกล้อง,image_id)` ส่งซ้ำid+hashเดิมตอบ200/201 ไม่สร้างรูปเพิ่ม; idเดิมแต่bytes/hashต่างกันตอบ409 `idempotency_conflict`
- serverไม่ให้keyของกล้องหนึ่งเขียนทับ/อ้างรูปของอีกกล้อง

ตอบ200หรือ201เท่านั้น:

```json
{"stored":true,"image_id":"0123456789abcdef0123456789abcdef","sha256":"<SHA-256จริง>"}
```

clientตรวจid/hashตรงของที่ส่ง จึงจะบันทึกreceiptและใช้ภาพในevent ไม่ถือว่าHTTP200อย่างเดียวส่งสำเร็จ

## 2. POST `/v1/refueling/events`

Headersเหมือนเดิมแต่ `Content-Type: application/json` และ `Idempotency-Key: <event_id>` bodyUTF-8ไม่เกิน64KiB ภาพทั้งหมดต้องอัปโหลดสำเร็จก่อน

```json
{
  "schema_version":1,
  "event_id":"example-round:3",
  "event_type":"refueling.attention_required",
  "occurred_at":"2026-10-28T22:31:11+07:00",
  "session":{
    "id":"example-round",
    "camera_id":"cabinet-1",
    "revision":3,
    "state":"open",
    "started_at":"2026-10-28T22:26:00+07:00",
    "last_ready_at":"2026-10-28T22:26:10+07:00",
    "ended_at":null,
    "ready_count":2,
    "has_issue":true,
    "issues":[
      {"code":"ready_prompt_repeated","detected_at":"2026-10-28T22:26:10+07:00"},
      {"code":"completion_not_detected","detected_at":"2026-10-28T22:31:11+07:00"}
    ],
    "amount_baht":null,
    "evidence":[
      {
        "id":"0123456789abcdef0123456789abcdef",
        "sha256":"0000000000000000000000000000000000000000000000000000000000000000",
        "content_type":"image/png",
        "byte_size":1024,
        "captured_at":"2026-10-28T22:25:58+07:00"
      }
    ]
  }
}
```

hash/ขนาดภาพในตัวอย่างเป็นplaceholder ต้องใช้ค่าจริงจากupload ข้อมูลรูปก่อนเวลาstarted_atเป็นไปได้เพราะรับเงินก่อนมีประกาศ ห้ามตัดรูปเหล่านี้ทิ้งเพียงเพราะเวลาเก่ากว่าstarted_at

### ความหมายและvalidation

- event_type: `refueling.started`, `refueling.updated` (เช่นเพิ่มรูป), `refueling.attention_required` (ประกาศซ้ำ/ไม่พบจบ), `refueling.completed`
- session.idคงเดิมทั้งรอบ event_id=`session.id:revision` revisionเป็นจำนวนเต็มบวกและเพิ่มเมื่อfactsเปลี่ยน ไม่เพิ่มเมื่อretry
- started_atมาจากเสียงพร้อมเติมครั้งแรก last_ready_atมาจากครั้งล่าสุด ready_countนับคำประกาศจริง ไม่ใช่จำนวนwindowที่ตรวจพบหรือจำนวนลูกค้า
- state=`open/completed/interrupted`; open/interruptedต้องended_at=null, completedต้องมีเวลาเสียงขอบคุณ ไม่สมมติจบที่นาที5
- ready_count>=1; ready_count>1ต้องมีissue `ready_prompt_repeated`; has_issueต้องตรงกับissues ไม่ล้างปัญหาทิ้งเพียงเพราะจบภายหลัง
- `completion_not_detected`: แสดง “ไม่พบเสียงจบภายใน 5 นาที” detected_atคือเวลาพบปัญหา ไม่ใช่เวลาจบ เสียงรถ/คุย/กระแทกไม่รีเซ็ตlatest-ready
- `audio_source_interrupted`: เพิ่มในdraft.12 เมื่อหลักฐานเสียงขาดขณะมีรอบเปิด stateต้องเป็นinterrupted แสดง “หลักฐานเสียงขาดช่วง / ไม่ทราบเวลาจบ” เวลาพบgapไม่ใช่เวลาลูกค้าเติมจบ รายการissuesมีได้สูงสุด3ชนิด ไม่เอาreadyหลังgapมาเพิ่มรอบเดิม และไม่เปลี่ยนinterruptedเป็นcompletedจากเสียงขอบคุณที่ไม่ทราบว่าเป็นลูกค้ารายใด actualendหลังgapใช้ปลดการรอขอบเขต จากนั้นreadyถัดไปเป็นidใหม่ ปัญหาที่พบก่อนgapยังคงอยู่ ไม่สร้างmissing-endใหม่จากช่วงไม่ได้รับข้อมูล
- amount_bahtต้องnullในv1นี้ เพราะเสียงไม่บอกยอดจริง ห้ามแทนด้วย0/10หรือคำนวณจำนวนลิตร
- ทุกเวลาRFC3339พร้อมtimezone clientปัจจุบันส่งUTC Zได้ หน้าจอแปลงเป็นAsia/Bangkok ไม่ใช่naive/local-timeที่ไม่มีoffset
- evidenceไม่เกิน64ภาพ แต่ละภาพidไม่ซ้ำ/hash64hex/mimePNGหรือJPEG/size1..2MiB เจ้าของกล้องและhashต้องตรงกับรูปที่เก็บแล้ว
- ไม่รับunknown field/NaN/Infinity ไม่รับlocalfile path/application-key/rawaudioในmetadata ภาพเป็นหลักฐานจากบริเวณรับธนบัตร ไม่รับรองชนิด/ยอดธนบัตร ไม่มีรูปไม่แปลว่าเป็นเหรียญล้วน

### Idempotency และrevision

ในtransactionฝั่งรับ:

1. ตรวจapp-key/cameraและภาพอ้างอิงทั้งหมด
2. บันทึกevent_idกับhashของbodyเดิม ส่งid+bodyเดิมซ้ำให้ตอบรับเดิม; idเดิมแต่bodyต่าง409 `idempotency_conflict`
3. Upsertprojectionตามsession.id อัปเดตเฉพาะrevisionที่ใหม่กว่า ไม่ให้requestเก่าที่retryมาทีหลังล้างflags/ended_atใหม่
4. Commitmetadata+image referencesให้durableก่อนตอบรับ ห้ามนับลูกค้าเพิ่มตามจำนวนPOST/ready_count

สำหรับrevisionใหม่ของรอบเดิม ต้องไม่ลดready_count/last_ready_at, เปลี่ยนstarted_at, ล้างปัญหาที่เคยพบ หรือเปลี่ยนcompletedกลับopen เวลาended_atที่ยืนยันแล้วต้องไม่ถูกแทนด้วยnull/เวลาสมมติ ให้ปฏิเสธstate transitionที่ขัดกติกาแทนเขียนทับ

ตอบ200/201:

```json
{"accepted":true,"event_id":"example-round:3"}
```

ถ้ารูปยังไม่พร้อมตอบ409 `{"code":"images_missing"}` ไม่ตอบaccepted:trueก่อนรูปใช้งานได้ clientอาจล้างreceiptและอัปโหลดซ้ำด้วยidเดิม ถ้ายังผิดซ้ำหลังยืนยันuploadให้หยุดเป็นcontract mismatch ไม่วนส่งรูปไม่จบ

## ผลลัพธ์อื่นและretry

| ผล | ฝั่งclient |
|---|---|
| 200/201 +ackตรง | ยืนยันphaseนั้นสำเร็จ |
| timeout/connectionขาด/5xx | ยังไม่ยืนยันการส่ง retryด้วยid/bodyเดิม |
| 429 | Retry-Afterวินาที clamp1–3600; ถ้าอ่านไม่ได้ใช้delayปกติ |
| 3xx | ไม่ตามredirect ไม่ส่งauthต่อ |
| 401/403 | errorสิทธิ์ พัก/ให้แก้configuration ไม่ค้นหาkeyใหม่เอง |
| 400/404/409อื่น/413/415/422 | failedตามcontract ไม่อ้างว่าเก็บแล้ว |
| 2xxแต่bodyผิด/id/hashไม่ตรง | ไม่ถือว่าสำเร็จ |

clientทำหนึ่งattemptต่อการสั่งdispatch ไม่มีretryทันทีวนในrequestเดียว ใช้delay5วินาทีเพิ่มแบบexponentialสูงสุด1ชั่วโมงจนหมดอายุ ขนาดresponseอ่านไม่เกิน64KiB timeoutเครือข่ายจำกัด errorเก็บเฉพาะsafe code ไม่เก็บresponsebody/credential

## การอ่านรูปและเก็บยาวฝั่งdashboard

- dashboardต้องจัดเก็บbinaryและmetadataเองตามนโยบายระยะยาว ไม่เก็บลิงก์กลับมาWindowsที่หมดอายุใน3วัน
- รูปที่รับแล้วให้UIอ่านผ่านrouteที่ตรวจlogin/สิทธิ์กล้อง เช่น GET `/v1/refueling/images/{image_id}` หรือBFFภายใน ไม่มีpublic URLที่เดาจากWindowspath ไม่มีapplication-keyฝังในbrowser
- ตอบContent-Typeที่ตรวจแล้วและ`X-Content-Type-Options: nosniff`; ใช้image_idเป็นidentifier ไม่ใช้ชื่อไฟล์จากclientสร้างpath
- ถ้าเก็บunreferenced uploadsชั่วคราว ต้องมีระยะเผื่อมากกว่าคิวclient72ชั่วโมงและรองรับidempotent re-upload ไม่ลบรูปที่มีeventอ้างอิงเพราะreceiptหมดอายุ

## Retentionและข้อมูลที่เครื่องต้นทางอาจหมดอายุ

ผู้ใช้ยืนยัน **ประวัติเติม/รูปที่เพิ่มใหม่ในเครื่องเก็บ72ชั่วโมงนับจากบันทึกรอบครั้งแรก** การประกาศซ้ำ/เพิ่มรูป/retryไม่ต่ออายุ เก็บยาวที่dashboard

- ลบแม้คิวยังส่งไม่สำเร็จเมื่อครบอายุ ไม่เก็บข้อมูลต่อไปไม่มีกำหนดเพียงเพราะปลายทางล่ม
- ต้องแสดง “หมดอายุโดยยังไม่ยืนยันการส่งสำเร็จ” ไม่สรุปว่าปลายทางไม่ได้รับแน่นอน เพราะresponseอาจหายหลังserverรับแล้ว และไม่เปลี่ยนเป็นส่งแล้วเอง
- ลบเฉพาะmetadata/mediaที่featureนี้เป็นเจ้าของ ไม่ลบsound/ตัวอย่างผู้ใช้/calibration/configหรือประวัติแจ้งแบงค์ค้างเดิมโดยพลการ
- หากรอบยังไม่จบตอนข้อมูลครบอายุ ลบเนื้อหา/รูป/รหัสรอบตามนโยบาย เหลือเพียงtechnical booleanว่า “รอขอบคุณก่อนเปิดขอบรอบใหม่” ไม่เปลี่ยนประกาศซ้ำเป็นลูกค้าใหม่ รอบเก่าที่ถูกลบแล้วไม่ถูกสร้าง/ปิดย้อนหลังด้วยเวลาสมมติ dashboardห้ามอนุมานว่าเติมเสร็จเพียงเพราะไม่ได้รับอัปเดต
- หากOSไม่ให้ลบรูปต้องแจ้งcleanup failureและretry ไม่อ้างว่าลบphysical fileได้หมดแล้ว ไม่มีคำรับประกันforensic secure eraseของWindows/SSD/backups
- โปรแกรมตรวจcleanupตอนทำงาน (หน้าประวัติตรวจแม้ไม่ได้เปิดแท็บ รอบประมาณ60วินาที) ถ้าปิดโปรแกรม/เครื่องอยู่จะลบเมื่อเปิดและถึงรอบตรวจแรก ไม่ได้ติดตั้งWindows service/scheduled taskหรืออ้างว่าลบได้ขณะไม่มีprocessรัน
- เปลี่ยนURL/credentialเป็นปลายทางคนละตัวไม่ย้ายqueueเก่าให้เอง ต้องมีการยืนยันrebindแยก

## Checklistฝั่งรับ

- ทดสอบkeyผิด/กล้องผิด/รูปidข้ามกล้อง/ขนาดเกิน/hashผิด/ภาพปลอม/unknown fields
- ทดสอบรูปซ้ำ/eventซ้ำ/bodyขัดแย้ง/revisionย้อน/ackหายแล้วretry ไม่ได้จำนวนลูกค้าเพิ่ม
- ทดสอบประกาศซ้ำและไม่พบจบพร้อมกันแล้วขอบคุณทีหลัง flagsไม่หาย
- ทดสอบรูปก่อนstarted_at รูปหลายใบ UIกดดูภาพเต็มได้หลังเครื่องต้นทางลบ3วัน
- ทดสอบAPIตอบรับหลังdurable commitจริง และไม่เปิดภาพให้ผู้ไม่มีสิทธิ์
- ผลทดสอบclientกับHTTPจำลอง127.0.0.1ไม่ใช่หลักฐานว่าbackendหรือdashboardจริงพร้อมใช้งาน
