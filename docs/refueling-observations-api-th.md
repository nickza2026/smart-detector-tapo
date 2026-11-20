# Refueling API: หลักฐานเสียงข้อมูลไม่ครบ

เหตุการณ์ชนิดนี้บันทึกประกาศที่ตรวจยืนยันแล้ว แต่ยังนำไปเป็นรอบเติมปกติไม่ได้ เช่น พบเสียงจบโดยไม่ทราบจุดเริ่มรอบที่ยืนยันได้ หรือพบเสียงพร้อมเติมหลังช่วงเสียงขาด เป็นหลักฐานเสียง ไม่ใช่การยืนยันธุรกรรม เวลาเริ่มเติม จำนวนครั้งเติม หรือยอดรับเงินจริง

## POST `/api/v1/refueling/observations`

SmartTapo ต่อ `/v1/refueling/observations` กับ base URL ที่ตั้งไว้ เช่น `https://dashboard.example/api` การส่งใช้สิทธิ์และกล้องเดียวกับการส่งรอบปกติ ไม่เปิด HTTP รับข้อมูลบนเครื่องเฝ้าตู้

```http
Authorization: Bearer <application-key>
X-Camera-ID: cabinet-1
Idempotency-Key: 11111111111141118111111111111111:1
Content-Type: application/json; charset=utf-8
Accept: application/json
```

```json
{
  "schema_version": 1,
  "event_id": "11111111111141118111111111111111:1",
  "event_type": "refueling.observation",
  "occurred_at": "2026-11-19T18:48:00Z",
  "observation": {
    "id": "11111111111141118111111111111111",
    "camera_id": "cabinet-1",
    "kind": "end",
    "reason": "completion_without_start",
    "observed_started_at": "2026-11-19T18:48:00Z",
    "observed_ended_at": "2026-11-19T18:48:02Z",
    "audio_group_id": null,
    "amount_baht": null,
    "evidence": []
  }
}
```

### ฟิลด์และการตรวจข้อมูล

ทุกฟิลด์ในตัวอย่างเป็นฟิลด์บังคับ รับเฉพาะชื่อฟิลด์ตามตัวอย่าง JSON เป็น UTF-8 ไม่เกิน 65,536 bytes ปฏิเสธชื่อฟิลด์ซ้ำ ชื่อต่างตัวพิมพ์ ฟิลด์นอกสัญญา และข้อมูลต่อท้าย JSON รองรับ Content-Type `application/json` หรือ `application/json; charset=utf-8`

| ฟิลด์ | ชนิดและค่า |
|---|---|
| `schema_version` | integer ค่า `1` ไม่ใช่ Boolean |
| `event_type` | string ค่า `refueling.observation` |
| `observation.id` | UUID รูป hexadecimal ตัวเล็ก 32 ตัว และไม่เป็นศูนย์ทั้งหมด |
| `event_id` | string เท่ากับ `observation.id` ต่อด้วย `:1`; เป็น observation ที่แก้ revision ไม่ได้ |
| `observation.camera_id` | string ของกล้องที่ application key และ `X-Camera-ID` อนุญาต |
| `observation.kind` | `end` หรือ `paid_ready` |
| `observation.reason` | คู่ค่าตามตารางด้านล่าง |
| `observed_started_at` / `observed_ended_at` | string RFC3339 ASCII ไม่เกิน 40 ตัว พร้อม timezone ที่ไม่เป็น zero time; เวลาต้นและท้ายประกาศเสียงจริง โดยท้ายไม่ก่อนต้น |
| `occurred_at` | string RFC3339 ASCII ไม่เกิน 40 ตัว แทน instant เดียวกับ `observed_started_at` โดยเปรียบเทียบเศษวินาทีครบตามที่ส่ง |
| `audio_group_id` | `null` หรือ string 1–128 ตัว ASCII ตาม `[A-Za-z0-9_.:-]+`; ไม่รับ string ว่าง เป็นการเชื่อมหลักฐานเสียงเท่านั้น ไม่ใช่ ID รอบเติม |
| `amount_baht` | literal `null` เท่านั้น |
| `evidence` | array ว่าง `[]` เท่านั้นในสัญญานี้ ไม่รับ `null` หรือรูปที่สร้างแทนหลักฐานที่ไม่มี |

เวลาเสียงต้นและท้ายอยู่ได้ไม่เกินเวลา server บวกสองนาที ไม่มีการกำหนดช่วงเสียงสูงสุดเพิ่มเติมจากสัญญานี้ ไม่มีฟิลด์ physical session เช่น `started_at`, `last_ready_at`, `ready_count` หรือ `state` ให้ใส่ข้อมูลทดแทน

| `kind` | `reason` ที่รับ |
|---|---|
| `end` | `completion_without_start` |
| `paid_ready` | `awaiting_end_after_source_gap`, `expired_round_awaiting_completion` |

### ผลตอบกลับและการส่งซ้ำ

รับครั้งแรกตอบ `201` และรับการส่งซ้ำเดิมตอบ `200` พร้อม acknowledgement:

```json
{"accepted":true,"event_id":"11111111111141118111111111111111:1"}
```

client ยืนยันการส่งสำเร็จจาก `accepted: true` และ `event_id` ตรงกับงานที่ส่ง ไม่ถือเพียง HTTP 200 ว่าสำเร็จ ID และ bytes ของ body ถูกเก็บครั้งเดียวสำหรับ retry ภายใต้เจ้าของและกล้องเดิม ID เดิมแต่ bytes ต่างกัน รวมถึงการเปลี่ยนลำดับ key/ช่องว่าง ได้ `409 idempotency_conflict` และไม่สร้างข้อมูลอีกแถว

รูปแบบ error เป็น `{"code":"..."}`:

| HTTP | code / ความหมาย |
|---|---|
| `401` | `unauthorized`: key หาย ไม่ถูกต้อง หรือไม่มีสิทธิ์กล้อง |
| `403` | `origin_not_allowed`: ส่ง Origin ที่ไม่ได้รับอนุญาต |
| `409` | `idempotency_conflict` |
| `413` | `body_too_large` |
| `415` | `unsupported_media_type` |
| `422` | `invalid_argument` หรือ `history_outside_retention` |
| `503` | `not_configured`: ตัวรับยังไม่พร้อมใช้งาน |

## อ่านหลักฐานที่รับแล้ว

การอ่านใช้ dashboard session cookie `refueling_session` ของเจ้าของ ไม่ใช่ application key ตอบ `Cache-Control: no-store` และจำกัดกล้องตามสิทธิ์ผู้ใช้

### GET `/api/v1/refueling/observations`

| Query | รูปแบบ |
|---|---|
| `camera_id` | ไม่บังคับ; จำกัดในกล้องที่อนุญาต |
| `kind` | ไม่บังคับ; `end` หรือ `paid_ready` |
| `reason` | ไม่บังคับ; หนึ่งในสาม reason ด้านบน |
| `from` / `to` | ไม่บังคับ; RFC3339 กรองเวลาเสียงต้นในช่วง `[from,to)` และ `from < to` |
| `page` | integer ตั้งแต่ 1; default 1 |
| `page_size` | integer 1–100; default 25 |
| `sort` | `observed_started_at_desc` เป็น default หรือ `observed_started_at_asc` |

เมื่อเวลาเท่ากันเรียงต่อด้วย `camera_id` แล้ว ID เพื่อให้ลำดับคงที่ ค่าที่ไม่รองรับถูกปฏิเสธ ผลตอบเป็น `{items:[DTO],page,page_size,total}` และรายการว่างเป็น `items: []`

การกรองและเรียงเวลามีความละเอียดระดับไมโครวินาที ส่วนค่าเวลาใน payload ของรายการตอบกลับคงข้อความ RFC3339 เดิมที่ส่งมา

DTO คือ event root เดิมที่ส่งเข้า เพิ่ม `first_received_at` เป็นเวลา server UTC RFC3339 และ `classification: "incomplete_observation"` ไม่รวมรายการเหล่านี้เป็นจำนวนรอบสำเร็จหรือยอดเงิน

### GET `/api/v1/refueling/observations/{id}?camera_id=...`

`id` เป็น lowercase UUIDhex32 และ `camera_id` บังคับ ผลสำเร็จเป็น DTO เดียวกัน รายการที่ไม่มีหรืออยู่นอกสิทธิ์ตอบ `404 not_found`

## อายุข้อมูลและการเปิดใช้

ตัวรับใช้ `retention_months` ของเจ้าของ ID ใหม่ที่เวลาเสียงต้นบวกเดือนตามปฏิทินหมดอายุแล้วตอบ `422 history_outside_retention` แถวที่รับสำเร็จหมดอายุจาก `first_received_at` บวกเดือน ส่วน replay เดิมตรวจจาก bytes เดิมก่อนตรวจอายุขณะที่แถวยังอยู่ ข้อมูลที่ล้างแล้วไม่ถูกสร้างกลับด้วยการส่งซ้ำแม้ภายหลังเพิ่มระยะเก็บ และไม่เปลี่ยนนโยบายเก็บรอบปกติ

ฝั่ง SmartTapo เก็บคิวตามระยะ 72 ชั่วโมงของข้อมูลที่บันทึกครั้งแรก การพบเสียงซ้ำไม่ต่ออายุข้อมูลเดิม การส่งใช้ consent และการผูกปลายทางเดิม ไม่เริ่มตัวส่งอัตโนมัติจากการอัปเดต และไม่ backfill ประวัติเก่าอัตโนมัติ

การนำขึ้นระบบจริงเป็นคนละขั้นกับการทดสอบในเครื่อง: receiver และ migration ที่รองรับ observations มาก่อน client ที่ส่งชนิดใหม่นี้ การรับ HTTP acknowledgement ไม่ใช่หลักฐานว่ามีการเติมจริงหรือยืนยันยอดเงิน
