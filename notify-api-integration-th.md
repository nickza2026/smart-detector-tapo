# Notify API สำหรับระบบอื่นส่งและติดตามแจ้งเตือน

เอกสารนี้ใช้สำหรับ backend-to-backend ผ่าน API ที่ใช้งานอยู่ ไม่ใช้ URL ของ Dashboard หรือ Firebase และไม่ต้องเข้าสู่ระบบ Google ก่อนเรียก

**API Base URL**

```text
https://notify-alarm-api-7496986423345747739.olufy-0.nortezh.com
```

ระบบผู้เรียกใช้ application API key ที่เจ้าของระบบส่งให้ผ่านช่องทางส่วนตัว คีย์เชื่อมต่อของระบบนี้คือค่า `ALERT_API_KEY` ที่เจ้าของตั้งไว้ฝั่ง API; ตัวแปร `NOTIFY_API_KEY` ในตัวอย่างฝั่ง caller เก็บคีย์เดียวกัน ตัวอย่างข้อมูลทั้งหมดในเอกสารเป็นข้อมูลสมมติ ไม่มีคีย์จริง API ปัจจุบันรับระดับ `normal` และ `critical`; ยังไม่รับ `urgent`

## เริ่มเชื่อมต่อ: ส่งข้อความเงียบหนึ่งเหตุการณ์

ตัวอย่างต่อไปนี้ส่ง HTTP จริงเมื่อรัน `NOTIFY_API_KEY` เป็น environment variable ส่วนตัวของระบบผู้เรียก ไม่ใส่คีย์จริงใน source code หรือส่งไปยัง browser

```bash
curl --request POST \
  'https://notify-alarm-api-7496986423345747739.olufy-0.nortezh.com/v1/alerts' \
  --header "Authorization: Bearer ${NOTIFY_API_KEY}" \
  --header 'Idempotency-Key: inventory:job:20261118:001' \
  --header 'Content-Type: application/json; charset=utf-8' \
  --data-raw '{"alert_id":"inventory-job-20261118-001","title":"ประมวลผลข้อมูลเสร็จแล้ว","message":"งานรอบ 09:00 เสร็จสมบูรณ์","ttl_seconds":300,"importance":"normal"}'
```

HTTP 202 หมายถึง provider รับคำขอ ไม่ยืนยันว่ามือถือได้รับแล้ว เก็บ `alert_id` เพื่ออ่านผลล่าสุดผ่าน `GET /v1/alerts/{alert_id}` ตามข้อ 4 ส่วนคีย์และ body สำหรับ retry ใช้ค่าของเหตุการณ์เดิมตามข้อ 3

## 1. Endpoint และสิทธิ์

| Method / path | หน้าที่ | Authentication |
|---|---|---|
| `POST /v1/alerts` | ส่งเหตุการณ์ใหม่ หรือ retry เหตุการณ์เดิม | application Bearer key + `Idempotency-Key` |
| `GET /v1/alerts/{alert_id}` | อ่านผลส่งและการรับทราบล่าสุด | application Bearer key |
| `GET /v1/history` | อ่านประวัติแบบ cursor pagination | application Bearer key |
| `GET /v1/stats` | อ่านจำนวนเหตุการณ์และการรับทราบ | application Bearer key |
| `GET /health` | ตรวจ configuration เบื้องต้น | ไม่ใช้ key |

สี่ endpoint แรกใช้ `Authorization: Bearer <APPLICATION_API_KEY>` คีย์เป็น secret ฝั่ง service ไม่ใช่ username/password ของ Dashboard, Android access token, Firebase API key, `DEVICE_TOKEN_ENCRYPTION_KEY` หรือ public `key_id` ชื่อ `primary` คีย์นี้เข้าถึงการส่งและประวัติของระบบเดียวกัน ไม่ใช่คีย์แยกสิทธิ์ตาม service

JSON response มี `Content-Type: application/json; charset=utf-8`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` เวลา response เป็น RFC 3339 ใน UTC ลงท้าย `Z` และอาจมีเศษวินาที จำนวนหลักจึงไม่คงที่

## 2. POST /v1/alerts

ตัวอย่างส่ง notification เงียบ:

```http
POST /v1/alerts
Authorization: Bearer <APPLICATION_API_KEY>
Idempotency-Key: inventory:job:20261118:001
Content-Type: application/json

{
  "alert_id": "inventory-job-20261118-001",
  "title": "ประมวลผลข้อมูลเสร็จแล้ว",
  "message": "งานรอบ 09:00 เสร็จสมบูรณ์",
  "importance": "normal"
}
```

ตัวอย่างเหตุการณ์ใหม่ที่ต้องปลุก ใช้ `Idempotency-Key` ของเหตุการณ์ใหม่นั้น:

```json
{
  "alert_id": "cabinet-001-off-20261118-001",
  "title": "ตู้ 001 ปิดนานเกินกำหนด",
  "message": "ตรวจพบสถานะ OFF ต่อเนื่อง กรุณาตรวจสอบตู้",
  "ttl_seconds": 300,
  "importance": "critical"
}
```

รับ JSON object เท่านั้น รองรับ `Content-Type: application/json` รวม `charset=utf-8` ไม่รับ field ที่ไม่รู้จัก, explicit `null`, ชนิดข้อมูลผิด หรือ JSON หลายก้อนต่อกัน ขนาด body สูงสุด 16,384 bytes

| Header / field | Type | Required / default | เงื่อนไข |
|---|---|---|---|
| `Idempotency-Key` | string | บังคับ | 1–128 ตัว ตรง `[A-Za-z0-9_.:-]+` |
| `alert_id` | string | ไม่ส่งได้; server สร้างและคืน ID | เมื่อส่งเอง 1–100 ตัว ตรง `[A-Za-z0-9_-]+` |
| `title` | string | บังคับ | ไม่ว่างหรือช่องว่างล้วน สูงสุด 120 Unicode code points |
| `message` | string | บังคับ | ไม่ว่างหรือช่องว่างล้วน สูงสุด 2,000 Unicode code points |
| `ttl_seconds` | integer | ไม่ส่ง = `300` | `30`–`10800` วินาที; `0` และค่านอกช่วงไม่ผ่าน |
| `importance` | enum string | ไม่ส่ง = `critical` | `normal` / `critical` แบบ case-sensitive; ค่าว่างไม่ผ่าน |

ช่องว่างต้น/ท้ายข้อความที่ไม่ใช่ช่องว่างล้วนยังเป็นส่วนหนึ่งของข้อความและการเทียบ retry จำนวน code points ไม่เท่ากับ bytes โดยเฉพาะภาษาไทยและ emoji นอกจาก body ยังมีเพดาน payload ส่งมือถือ 4,000 UTF-8 bytes รวมข้อความ, ID, เวลาและข้อมูลรับทราบที่ server เติม ข้อความที่ผ่านเพดานตัวอักษรจึงอาจได้ `422 payload_too_large`

### Importance, TTL และผู้รับ

| ค่า | พฤติกรรม Android รุ่นรองรับ |
|---|---|
| `normal` | notification เงียบ ไม่เล่นเสียง ไม่สั่น ไม่เปิด full-screen และไม่เริ่ม alarm foreground service; ไม่หยุด critical ที่กำลังดัง |
| `critical` | เริ่มเสียงปลุกต่อเนื่องจนผู้ใช้กดรับทราบในเครื่อง |

TTL กำหนดเวลาล่าสุดที่เหตุการณ์ยังเริ่มแสดง/ปลุกได้ ไม่ใช่ระยะเวลาหยุดเสียง critical ที่เริ่มแล้ว การกลับมาปกติของอุปกรณ์ต้นทางไม่ใช่การรับทราบและไม่สั่งหยุดเสียงอัตโนมัติ

normal ใช้ FCM priority `NORMAL` และอาจล่าช้าใน Doze/background ส่วน critical ใช้ `HIGH` ทั้งสองแบบขึ้นกับ permission, network และข้อจำกัด Android ไม่รับประกันเวลาถึงหรือเสียงในทุก OS state critical ใช้ระดับเสียงปลุกของ Android ไม่ใช่ระดับเสียงเรียกเข้า; ระดับเสียงปลุกเป็นศูนย์หรือ DND ที่ไม่อนุญาตเสียงปลุกยังมีผลต่อเสียง APK เก่าที่ไม่รู้จัก normal จะข้ามรายการ normal แทนการแปลงเป็นเสียงปลุก

service ไม่ส่ง FCM token, device ID, `key_id` หรือรายชื่อผู้รับใน body นี้ ผู้รับมาจากทะเบียนอุปกรณ์ของบัญชีที่เจ้าของระบบตั้งไว้ หรือปลายทางเดิมที่ยังใช้งานอยู่ เหตุการณ์กระจายไปยังผู้รับที่เลือกไว้สำหรับการส่งนั้น การ retry ไม่เพิ่มผู้รับที่เพิ่งลงทะเบียนภายหลัง และการรับทราบแต่ละเครื่องแยกกัน

### HTTP 202

```json
{
  "alert_id": "cabinet-001-off-20261118-001",
  "status": "provider_accepted",
  "expires_at": "2026-11-18T02:05:00Z",
  "acknowledged": false,
  "delivery_verified": false,
  "importance": "critical"
}
```

| Field | Type | ความหมาย |
|---|---|---|
| `alert_id` | string | ID สำหรับอ่านรายละเอียด |
| `status` | string | `provider_accepted` เมื่อ HTTP 202 |
| `expires_at` | timestamp | latest-start expiry ของเหตุการณ์ |
| `acknowledged` | boolean | ใน POST receipt เป็น `false` เสมอ ไม่ใช่ Ack ปัจจุบัน |
| `delivery_verified` | boolean | `false` เสมอ ไม่ยืนยันว่ามือถือได้รับหรือส่งเสียง |
| `importance` | enum | ระดับที่ใช้จริง รวม default ของ request |

`provider_accepted` หมายถึง provider รับคำขอ สำหรับหลายเครื่องหมายถึงทุกปลายทางในเหตุการณ์นั้นมีผล accepted ไม่ยืนยันว่าผู้ใช้เห็นข้อความหรือมีเสียงจริง

### HTTP 502: ผลการส่งที่บันทึกแล้ว

```json
{
  "alert_id": "cabinet-001-off-20261118-001",
  "status": "delivery_unknown",
  "error": { "code": "partial_provider_outcome" },
  "delivery_verified": false,
  "importance": "critical"
}
```

รูปแบบนี้มี `alert_id`, `status`, `error.code`, `delivery_verified`, `importance` แต่ไม่มี `expires_at`, `acknowledged`, `request_id`

| `status` | ความหมาย |
|---|---|
| `provider_failed` | ส่งไม่สำเร็จตามผลที่ทราบ; หลายเครื่องเป็น failed ทุกปลายทาง |
| `delivery_unknown` | ผลไม่แน่นอน หรือผลหลายเครื่องไม่เหมือนกัน บางเครื่องอาจรับแล้ว |

รหัสที่เกี่ยวข้องใน `error.code`: `provider_rejected`, `provider_unregistered`, `provider_authentication_failed`, `expired_before_send`, `provider_delivery_unknown`, `partial_provider_outcome` อ่านผลแยกเครื่องได้จาก GET รายละเอียด แต่ API ไม่คืน FCM token หรือ capability รับทราบ

## 3. Idempotency, retry และโควตา

หนึ่งเหตุการณ์ใช้ `Idempotency-Key` เดิมและ body ความหมายเดิมตลอด retry รวม `alert_id` ถ้าส่งเอง คีย์อยู่ในขอบเขตระบบร่วมกัน ใช้ prefix แยก service/เหตุการณ์ได้ตามอักขระที่รองรับ

- ลำดับ key/ช่องว่าง JSON ไม่เปลี่ยนความหมาย การไม่ส่ง TTL กับส่ง `300` เท่ากัน และการไม่ส่ง importance กับส่ง `critical` เท่ากัน
- หาก request แรกไม่ส่ง `alert_id` การ retry ใช้ body เดิมที่ยังไม่ส่ง ID ไม่เติม ID ที่ได้รับกลับเข้า body ของ retry
- ผลที่เสร็จแล้วคืน receipt เดิมพร้อม `Idempotency-Replayed: true` ไม่ส่ง provider ซ้ำ รวมผล failed/unknown และ replay หลัง TTL หมด
- POST replay หลังคน Ack ยังคืน `acknowledged:false` เดิม การติดตาม Ack ใช้ GET รายละเอียด
- เปลี่ยนข้อความ/TTL/importance/ID ด้วยคีย์เดิมเป็น `409 idempotency_conflict`; ID ที่มีอยู่กับคีย์ใหม่เป็น `409 alert_id_conflict`
- `409 delivery_unknown_or_in_progress` คือเหตุการณ์เดิมยังไม่มีผลสรุปที่คืนได้ ไม่ใช่สัญญาว่าจะส่งซ้ำให้เมื่อ retry

| ผลที่ caller พบ | การเรียกต่อ |
|---|---|
| HTTP 202 | เก็บ `alert_id`; ใช้ GET เมื่อต้องติดตาม Ack |
| HTTP 400/413/415/422 | แก้ข้อมูลก่อน ไม่วนส่ง input ผิดเดิม |
| HTTP 401 | ตรวจ application key ผ่านช่องทางส่วนตัว |
| HTTP 409 | ตรวจชนิด conflict/ผลเดิม ไม่เปลี่ยนคีย์เพื่อบังคับส่งซ้ำ |
| HTTP 429 | รอจำนวนวินาทีใน `Retry-After` แล้วใช้ request เดิม |
| HTTP 502 | retry คีย์เดิมอ่านผลเดิม ไม่เกิดการส่งรอบใหม่ |
| HTTP 503 หรือ timeout/connection error | ผลอาจเกิดแล้ว ใช้ backoff แบบมีขอบเขตและ request เดิมเท่านั้น พร้อมตรวจ GET ถ้าทราบ ID |

ไม่มี polling interval หรือเวลาถึงมือถือที่ API รับประกัน เหตุการณ์ใหม่หลัง TTL หมดหรือยกระดับ normal เป็น critical เป็นการตัดสินใจของ service/เจ้าของระบบ ไม่ใช่ network retry

ค่าเริ่มต้นคือ request ที่ผ่านสิทธิ์รวม 60 ครั้ง/นาทีต่อ API instance และไม่เกิน 4 คำขอพร้อมกัน ใช้งบร่วมกับการอ่านข้อมูลและ capability Ack ของมือถือ ไม่แยกโควตาตาม service คำขอไม่ผ่านสิทธิ์มีโควตาอีกชุดหนึ่ง 60 ครั้ง/นาทีต่อ instance ขณะเกินโควตานาทีมี `Retry-After: 60`; ขณะเต็ม concurrency มี `Retry-After: 1` ค่าจริงอาจต่างเมื่อเจ้าของระบบกำหนด instance ต่างจากค่าเริ่มต้น

## 4. GET /v1/alerts/{alert_id}

ไม่มี request body:

```http
GET /v1/alerts/cabinet-001-off-20261118-001
Authorization: Bearer <APPLICATION_API_KEY>
```

HTTP 200 ตัวอย่าง provider รับแล้ว ยังไม่มี Ack:

```json
{
  "alert_id": "cabinet-001-off-20261118-001",
  "title": "ตู้ 001 ปิดนานเกินกำหนด",
  "message": "ตรวจพบสถานะ OFF ต่อเนื่อง กรุณาตรวจสอบตู้",
  "importance": "critical",
  "status": "provider_accepted",
  "created_at": "2026-11-18T02:00:00Z",
  "provider_accepted_at": "2026-11-18T02:00:01Z",
  "expires_at": "2026-11-18T02:05:00Z",
  "acknowledged_at": null,
  "ack_count": 0,
  "last_ack_at": null,
  "acknowledged": false,
  "ack_actions": [],
  "deliveries": [
    {
      "device_id": "11111111-1111-4111-8111-111111111111",
      "device_name": "โทรศัพท์ทดสอบ",
      "status": "provider_accepted",
      "provider_accepted_at": "2026-11-18T02:00:01Z",
      "acknowledged_at": null,
      "ack_count": 0,
      "last_ack_at": null
    }
  ],
  "delivery_verified": false
}
```

### Alert fields ร่วมของรายละเอียดและประวัติ

| Field | Type / nullable | ความหมาย |
|---|---|---|
| `alert_id` | string | ID เหตุการณ์ |
| `title`, `message` | string | ข้อความที่รับไว้ |
| `importance` | enum | `normal` / `critical` |
| `status` | enum | `provider_accepted` / `provider_failed` / `delivery_unknown`; ไม่เปลี่ยนเป็น acknowledged หลัง Ack |
| `created_at` | timestamp | เวลาสร้างที่ server |
| `provider_accepted_at` | timestamp / null | เวลาสรุป provider accepted; null เมื่อยังไม่ใช่ accepted ทั้งเหตุการณ์ |
| `expires_at` | timestamp | latest-start expiry ไม่ใช่เวลาลบประวัติหรือหมดสิทธิ์ Ack เก่า |
| `acknowledged_at` | timestamp / null | เวลา server รับ Ack แรกจากเครื่องใดก็ได้ |
| `ack_count` | integer | จำนวนรายการ Ack ที่ไม่ซ้ำในเหตุการณ์ รวมแต่ละเครื่อง; retry ไม่เพิ่ม |
| `last_ack_at` | timestamp / null | เวลา server รับ Ack ล่าสุด |

### Fields เพิ่มเฉพาะรายละเอียด

| Field | Type | ความหมาย |
|---|---|---|
| `acknowledged` | boolean | มี Ack อย่างน้อยหนึ่งรายการ ไม่ได้แปลว่าครบทุกเครื่อง |
| `ack_actions` | array | เรียงเวลา server รับและ action ID; ไม่มีรายการเป็น `[]` |
| `deliveries` | array | ผลแยกเครื่องเรียง device ID; legacy อาจเป็น `[]` |
| `delivery_verified` | boolean | `false` เสมอ แม้มี Ack ก็ไม่ใช่การตรวจเสียงจาก API |

สมาชิก `ack_actions`:

```json
{
  "client_action_id": "22222222-2222-4222-8222-222222222222",
  "client_acked_at": "2026-11-18T02:01:00Z",
  "received_at": "2026-11-18T02:01:03Z",
  "device_id": "11111111-1111-4111-8111-111111111111",
  "device_name": "โทรศัพท์ทดสอบ"
}
```

`client_action_id` เป็น UUID การกด, `client_acked_at` เป็นเวลาจากเครื่องและอาจเป็น null, `received_at` เป็นเวลา server รับ สำหรับ legacy `device_id`/`device_name` เป็น null ไม่ใช่ field ที่หายไป

สมาชิก `deliveries` มี `device_id` UUID, `device_name` string, `status` enum เดียวกับ Alert, `provider_accepted_at`, `acknowledged_at`, `last_ack_at` เป็น timestamp/null และ `ack_count` integer ความหมายด้านเวลา/จำนวนใช้กับเครื่องนั้น ไม่ใช่ผลรวม

ID ไม่มี/รูปแบบ path ไม่ตรงได้ `404 not_found` อ่านไม่ได้ได้ `503 database_unavailable` รวม common auth/rate errors ในข้อ 8

## 5. GET /v1/history

ไม่มี request body:

```http
GET /v1/history?from=2026-11-18T00%3A00%3A00Z&to=2026-11-19T00%3A00%3A00Z&status=pending_ack&limit=1
Authorization: Bearer <APPLICATION_API_KEY>
```

| Query | Type / default | เงื่อนไข |
|---|---|---|
| `from` | RFC 3339 timestamp; ไม่ส่ง/ว่าง = ไม่จำกัดต้น | `created_at >= from` |
| `to` | RFC 3339 timestamp; ไม่ส่ง/ว่าง = ไม่จำกัดปลาย | `created_at < to`; เมื่อมีทั้งคู่ `from < to` |
| `status` | enum; ไม่ส่ง/ว่าง = `all` | `all`, `provider_accepted`, `provider_failed`, `delivery_unknown`, `acknowledged`, `pending_ack` |
| `limit` | integer; default `25` | `1`–`100`; ส่งว่างไม่ผ่าน |
| `cursor` | opaque string; ไม่ส่ง/ว่าง = หน้าแรก | ใช้ค่าจาก `next_cursor` |

`acknowledged` กรองเหตุการณ์ที่มี Ack ส่วน `pending_ack` กรองเฉพาะ provider accepted ที่ยังไม่มี Ack เลย ไม่รวม failed/unknown แม้ไม่มีคนรับทราบ

เรียง `created_at` ใหม่ไปเก่า เวลาเท่ากันเรียง `alert_id` มากไปน้อย ไม่มี query เปลี่ยน sort หน้าถัดไปคง from/to/status เดิม; เปลี่ยนตัวกรองแล้วเริ่มหน้าแรก offset เวลาที่มี `+` ใช้ URL encoding เช่น `%2B07%3A00`

cursor ผูกกับ `from`/`to`/`status` เมื่อนำไปใช้กับตัวกรองต่างกันได้ HTTP 400 `invalid_cursor` ส่วน `limit` เปลี่ยนระหว่างหน้าได้

HTTP 200 คืน `items` ซึ่งสมาชิกมีเฉพาะ Alert fields ในข้อ 4 และ `next_cursor` เป็น string หรือ null เมื่อไม่มีหน้าถัดไป ไม่มี total count หรือ `ack_actions`, `deliveries`, `acknowledged`, `delivery_verified` ใน history item ตัวอย่างหน้าแรกที่ยังมีหน้าถัดไป:

```json
{
  "items": [
    {
      "alert_id": "cabinet-001-off-20261118-001",
      "title": "ตู้ 001 ปิดนานเกินกำหนด",
      "message": "ตรวจพบสถานะ OFF ต่อเนื่อง กรุณาตรวจสอบตู้",
      "importance": "critical",
      "status": "provider_accepted",
      "created_at": "2026-11-18T02:00:00Z",
      "provider_accepted_at": "2026-11-18T02:00:01Z",
      "expires_at": "2026-11-18T02:05:00Z",
      "acknowledged_at": null,
      "ack_count": 0,
      "last_ack_at": null
    }
  ],
  "next_cursor": "eyJ0IjoiMjAyNi0xMS0xOFQwMjowMDowMFoiLCJpIjoiY2FiaW5ldC0wMDEtb2ZmLTIwMjYxMTE4LTAwMSIsImYiOiIyMDI2LTExLTE4VDAwOjAwOjAwWiIsInUiOiIyMDI2LTExLTE5VDAwOjAwOjAwWiIsInMiOiJwZW5kaW5nX2FjayJ9"
}
```

หน้าถัดไปใช้ query เดิมและเพิ่ม `cursor=<next_cursor>` ตัวอย่างหน้าว่าง:

```json
{"items": [], "next_cursor": null}
```

ข้อผิดพลาด query เป็น HTTP 400: `invalid_from`, `invalid_to`, `invalid_range`, `invalid_status`, `invalid_limit`, `invalid_cursor`; อ่านไม่ได้เป็น `503 database_unavailable`

## 6. GET /v1/stats

ไม่มี request body รองรับ `from`/`to` และช่วง `created_at` แบบเดียวกับ history ไม่ใช้ status หรือ cursor เป็นตัวกรองสถิติ

```http
GET /v1/stats?from=2026-11-18T00%3A00%3A00Z&to=2026-11-19T00%3A00%3A00Z
Authorization: Bearer <APPLICATION_API_KEY>
```

HTTP 200:

```json
{
  "total_alerts": 3,
  "provider_accepted": 2,
  "provider_failed": 1,
  "delivery_unknown": 0,
  "acknowledged": 1,
  "pending_ack": 1,
  "ack_action_count": 1
}
```

ทุก field เป็น integer ไม่เป็น null ไม่มีข้อมูลเป็น 0 ทั้งหมด `total_alerts` นับเหตุการณ์, สาม field provider นับตาม status, `acknowledged` นับเหตุการณ์ที่มี Ack, `pending_ack` ใช้ความหมายเดียวกับ history ส่วน `ack_action_count` นับการกดที่ไม่ซ้ำ: Ack-all หนึ่งครั้งบนเครื่องเดียวแม้ครอบคลุมหลายเหตุการณ์นับครั้งเดียว คนละเครื่องนับแยก และแยกจากรายการ legacy

ช่วงวันที่เลือกตามเวลาสร้างเหตุการณ์ ไม่ใช่เวลากดรับทราบ ข้อผิดพลาด query เป็น HTTP 400 `invalid_from`, `invalid_to`, `invalid_range`; อ่านไม่ได้เป็น `503 database_unavailable`

## 7. GET /health

ไม่ใช้ request body, key หรือโควตาข้างต้น HTTP 200 ตัวอย่าง:

```json
{
  "ready": true,
  "status": "configuration_only",
  "delivery_verified": false,
  "ack_sync_supported": true
}
```

`ready` เป็น boolean ว่ามี sender configuration, `status` เป็น `configuration_only`, `delivery_verified` เป็น false, `ack_sync_supported` เป็น boolean ว่าตั้งค่าช่องทาง Ack ไว้ ผลนี้ไม่ตรวจการเชื่อมต่อฐาน/FCM/มือถือจริงและไม่ใช่ผลทดสอบ end-to-end

## 8. Error reference

error ทั่วไปเป็น object นี้ ไม่มี `message` หรือ field errors เพิ่ม:

```json
{
  "error": { "code": "invalid_importance" },
  "request_id": "33333333-3333-4333-8333-333333333333"
}
```

ตัวอย่างนี้ใช้ HTTP 422 `request_id` เป็น UUID อ้างอิงคำขอ ไม่ใช่ `alert_id` ส่วน HTTP 502 submission receipt ใช้รูปแบบแยกในข้อ 2

| HTTP | `error.code` | กรณี |
|---|---|---|
| 400 | `idempotency_key_required` | POST ขาด key/รูปแบบไม่ผ่าน |
| 400 | `invalid_schema` | body ไม่ใช่ object, null/field/type/JSON ไม่ผ่าน |
| 400 | `request_interrupted` | อ่าน body ไม่ครบ/ผิดพลาด |
| 400 | `invalid_from`, `invalid_to`, `invalid_range` | วันที่/ช่วงใน history/stats ไม่ผ่าน |
| 400 | `invalid_status`, `invalid_limit`, `invalid_cursor` | query ประวัติไม่ผ่าน |
| 401 | `unauthorized` | application Bearer key ไม่ผ่าน |
| 404 | `not_found` | ไม่พบเหตุการณ์/route |
| 409 | `idempotency_conflict`, `alert_id_conflict`, `delivery_unknown_or_in_progress` | ตามข้อ 3 |
| 409 | `device_limit_reached` | ปลายทางเกินเพดาน 20 เครื่อง ประสานเจ้าของระบบ |
| 413 | `body_too_large` | body เกิน 16,384 bytes |
| 415 | `json_required` | Content-Type ไม่ใช่ JSON ที่รองรับ |
| 422 | `invalid_message` | title/message ขาด ว่าง เกินเพดาน |
| 422 | `invalid_alert_id` | ID ใน body ไม่ผ่าน |
| 422 | `invalid_ttl`, `invalid_importance` | ค่านอกช่วง/enum |
| 422 | `payload_too_large` | payload ส่งมือถือเกินเพดาน |
| 429 | `rate_limited` | มี `Retry-After` ตามข้อ 3 |
| 503 | `database_unavailable`, `temporarily_unavailable` | ระบบไม่พร้อม/ข้อผิดพลาดที่ไม่เปิดเผยรายละเอียด |
| 503 | `fcm_not_configured`, `ack_not_configured`, `targeted_sender_not_configured`, `device_registration_not_configured` | ช่องทางส่ง/รับทราบไม่พร้อม |
| 503 | `no_devices_registered` | ไม่มีปลายทางพร้อมรับ |
| 503 | `delivery_state_unavailable` | ยืนยันผลส่งที่บันทึกไว้ไม่ได้ อาจมีเครื่องรับแล้ว |

## 9. ตัวอย่าง PowerShell

ตัวอย่างนี้ส่ง HTTP จริงเมื่อรัน ใช้กับปลายทางที่ได้รับอนุมัติ `NOTIFY_API_BASE_URL`/`NOTIFY_API_KEY` เป็น configuration ฝั่ง caller ไม่ใช่ค่า credential ในเอกสาร:

```powershell
if ([string]::IsNullOrWhiteSpace($env:NOTIFY_API_BASE_URL) -or
    [string]::IsNullOrWhiteSpace($env:NOTIFY_API_KEY)) {
    throw 'ตั้ง NOTIFY_API_BASE_URL และ NOTIFY_API_KEY ผ่าน configuration ส่วนตัวก่อน'
}
$base = $env:NOTIFY_API_BASE_URL.TrimEnd('/')
if (([uri]$base).Scheme -ne 'https' -or ([uri]$base).UserInfo) {
    throw 'API base ต้องเป็น HTTPS และไม่มี credentials ใน URL'
}
$headers = @{
    Authorization = "Bearer $env:NOTIFY_API_KEY"
    'Idempotency-Key' = 'inventory:job:20261118:001'
}
$body = @{
    alert_id = 'inventory-job-20261118-001'
    title = 'ประมวลผลข้อมูลเสร็จแล้ว'
    message = 'งานรอบ 09:00 เสร็จสมบูรณ์'
    importance = 'normal'
} | ConvertTo-Json -Compress

$result = Invoke-RestMethod -Method Post -Uri "$base/v1/alerts" `
    -Headers $headers -MaximumRedirection 0 -ContentType 'application/json; charset=utf-8' `
    -Body ([Text.Encoding]::UTF8.GetBytes($body))

# Ack ปัจจุบันอ่านแยกจาก POST receipt
$detail = Invoke-RestMethod -Method Get -Uri "$base/v1/alerts/$($result.alert_id)" `
    -Headers @{ Authorization = "Bearer $env:NOTIFY_API_KEY" } -MaximumRedirection 0
$detail.acknowledged
```

## 10. การรับทราบและการหยุดเสียง

`POST /v1/alerts/{alert_id}/ack` เป็นช่องทางมือถือ ใช้ capability เฉพาะเหตุการณ์/เครื่องที่ได้รับไปกับข้อความ ไม่รับ application key แทน capability API สำหรับ service ไม่มีคำสั่ง cancel, resolve, mute หรือ stop alarm

เสียงหยุดจากการกดรับทราบในเครื่องได้แม้ offline แล้วมือถือ sync Ack ภายหลัง จึงมีช่วงที่คนหยุดเสียงแล้วแต่ GET ยังไม่มี Ack การกด Ack-all หนึ่งครั้งใช้ `client_action_id` เดิมข้ามเหตุการณ์บนเครื่องเดียวและ retry ซ้ำ ไม่สร้างการกดใหม่เพราะ HTTP retry

local synthetic sender ไม่ส่ง FCM จริงและไม่ยืนยันผลบนมือถือ การใช้ production/public HTTPS ใช้ปลายทางที่เจ้าของระบบระบุและตรวจสิทธิ์แยกจากเอกสารนี้
