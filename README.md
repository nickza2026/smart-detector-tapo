# Smart Tapo Desktop — Windows / MEmu

โปรแกรมหน้าต่าง **SmartTapo.exe** สำหรับตั้งค่าและควบคุม **Capture / Worker** โดยไม่ต้องเปิด CMD สองงานยังทำงานแยก process: Capture ควบคุม MEmu/ADB และเผยแพร่ PNG + ready metadata; Worker วิเคราะห์ ROI เก็บ SQLite/หลักฐาน แจ้งเตือน และ cleanup โดยใช้ local disk เครื่องเดียวกัน ไม่ต้องติดตั้ง PostgreSQL

## เริ่มใช้งานผ่าน GUI — วิธีหลัก

1. ติดตั้ง MEmu/Tapo/ADB และล็อกอิน Tapo ด้วยตัวเองครั้งแรก จากนั้นดับเบิลคลิก **SmartTapo.exe** ไม่ต้องติดตั้ง Python บนเครื่องใช้งาน
2. เปิดครั้งแรกโปรแกรมสร้าง `%LOCALAPPDATA%\SmartTapo\config.json` และเปิดแท็บ **ตั้งค่า** ให้อัตโนมัติ เลือก `adb.exe`, MEmu.exe ถ้าต้องการให้เปิด emulator, ชื่อกล้อง แล้วกด **ค้นหาอุปกรณ์** เพื่อเลือก Serial
3. กรอก App Token และ User/Group Key ของ Pushover ในช่องปิดบังข้อความ กดบันทึก คีย์เก็บแยกเป็น `pushover.dat` แบบ **Windows DPAPI current-user** ไม่ใช่ plaintext ใน JSON/command line/log ค่าเริ่มต้นยังไม่ส่งแจ้งเตือนจริง ต้องเปิด checkbox หลังทดลองระบบแล้ว
4. ไปแท็บ **ภาพช่องว่าง** หยุดงานและตรวจว่าช่องรับไม่มีแบงค์ กด **ถ่ายภาพจาก MEmu** หรือเลือก PNG จาก MEmu ที่มีอยู่ ลากกรอบให้ครอบจุดตรวจ ติ๊กยืนยัน แล้วกดบันทึก ไม่ต้องหาไฟล์ empty หรือรัน calibration command เอง
5. ไปแท็บ **เริ่ม / หยุด** กดเริ่ม Worker และ Capture ได้จากหน้าต่างเดียว ดูสถานะ process และข้อผิดพลาดด้านล่าง สถานะ process ทำงานไม่ใช่หลักฐานว่ากล้องสด/จำแนกแบงค์ถูกต้อง
6. ปุ่มหยุด/ปิดหน้าต่างจะขอหยุดงานอย่างปลอดภัยและรอ ADB/งานปัจจุบัน ไม่สั่งปิด MEmu และไม่บังคับ kill งานตามปกติ

บน Windows การเรียก ADB ทั้งค้นหาอุปกรณ์/ถ่ายภาพอ้างอิง/Capture ใช้ `CREATE_NO_WINDOW` เพื่อไม่เปิด console แทรก แต่ยังรับผล/ข้อผิดพลาดไว้ใน GUI ตามเดิม ไม่ใช้ตัวเลือกนี้กับการเปิดหน้าต่าง MEmu

## ประวัติการหยอดแบงค์และเว็บประวัติ

- แท็บ **ประวัติการหยอดแบงค์** แสดงเหตุการณ์ตามเวลาเริ่มล่าสุดก่อน รีเฟรชอัตโนมัติเมื่อเปิดแท็บ มีปุ่มแบ่งหน้าและดับเบิลคลิก/กดดูภาพได้ ไม่สร้างรายการใหม่ทุกวินาที แต่รวมภาพที่ตรวจพบต่อเนื่องเป็นเหตุการณ์เดียว
- เก็บภาพเต็ม **เมื่อเริ่มพบวัตถุครั้งแรกของเหตุการณ์** แยกจากภาพกรอบตรวจที่จัดแนวแล้ว/ภาพเมื่อช่องว่าง จึงดูหลักฐานได้แม้ raw screenshot ต้นฉบับถูก cleanup แล้ว ไม่ใช่การเก็บทุกเฟรมเป็นวิดีโอย้อนหลัง
- เหตุการณ์ปกติที่พบวัตถุแล้วเห็นช่องว่างต่อเนื่อง ทั้งรายการและภาพเก็บ **24 ชั่วโมงหลังเหตุการณ์จบ** ไม่ใช่ลบทันทีเมื่อข้ามเที่ยงคืน เปลี่ยนได้ในแท็บตั้งค่า ช่อง **เก็บประวัติหยอดปกติ (ชั่วโมง)** หยุด Worker ก่อนเปลี่ยนเพื่อให้ worker รอบใหม่ใช้ค่านี้
- `deposit_history_hours` เป็นค่าใหม่แยกจาก `non_alert_images_minutes` เดิม ดังนั้น config รุ่นเก่าที่ตั้งภาพไม่แจ้งเตือนไว้ 5 นาทีจะได้ประวัติเหตุการณ์ปกติ 24 ชั่วโมงโดยไม่ต้องแก้ JSON เอง ภาพไม่ครบ/ยืนยันไม่ได้ยังใช้กติกาเดิม ภาพแบงค์ค้างยังใช้ 7 วันและ metadata 30 วัน
- ภาพทุกวินาทีที่ Worker ประมวลผลแล้ว ยังคงวนลบหลัง **5 นาที** ตาม `rolling_frames_minutes`; Worker ล้างตามรอบประมาณ 30 วินาที ต้องเปิด Worker เพื่อให้ cleanup ทำงาน ภาพ pending/เหตุการณ์ active/การแจ้งเตือนยังไม่จบไม่ถูกลบทิ้งเพื่อให้ quota ผ่าน
- ในแท็บประวัติ เลือก **Port** (เริ่มต้น 8080) → **เริ่มเว็บ** → **เปิดในเบราว์เซอร์** พอร์ตบันทึกเมื่อเริ่มเว็บ มีปุ่มหยุดเว็บและปิดอัตโนมัติเมื่อปิดแอป ไม่เปิดเว็บเองเมื่อเปิดแอปครั้งใหม่
- เว็บเป็น read-only ที่ **127.0.0.1 เฉพาะเครื่องนี้** ไม่เปิด LAN/Internet ไม่เปิด firewall หรือ tunnel ใช้ลิงก์ session จากปุ่มเปิดในเบราว์เซอร์เพื่อเข้าเว็บ; การพิมพ์ URL เปล่าในเบราว์เซอร์ที่ยังไม่มี session จะถูกปฏิเสธ ไม่มีการส่งภาพหรือคีย์ไปเว็บภายนอก
- GUI และเว็บอ่าน SQLite ชุดเดียวกันได้แม้หยุด Capture/Worker อยู่ ใช้ขอบเขตกล้องเดียวกับ config และแสดงสถานะปกติ/ค้าง/กำลังพบ/ข้อมูลไม่ครบแยกกัน **การพบวัตถุไม่ยืนยันว่าเป็นแบงค์จริง ไม่ยืนยันการรับเงิน ยอดเงิน หรือจำนวนใบ** และการหยอดที่ผ่านระหว่างสองภาพอาจไม่ถูกจับได้ ถ้าจัดแนวภาพเป็น UNKNOWN จะไม่เดาว่าเป็นการหยอดปกติ
- ก่อนอัปเกรดให้หยุดรุ่นเดิมและสำรอง config หากต้องการย้อนกลับ รุ่นใหม่นี้อ่าน config เก่าได้ แต่หลังบันทึกจะเพิ่ม `deposit_history_hours`/`history_web_port` ซึ่ง executable รุ่นเก่าอาจไม่รู้จัก ไม่ลบหรือสร้างฐานข้อมูลประวัติใหม่ทับของเดิม

กด **เปิดโฟลเดอร์ข้อมูล** เพื่อดูตำแหน่งตั้งค่าและข้อมูล ข้อมูลปกติอยู่ใต้ `%LOCALAPPDATA%\SmartTapo` ไม่อยู่ใน temporary extraction directory ของ PyInstaller

- หากมี `config.local.json` อยู่ข้าง exe ก่อนเปิด จะใช้เป็น **portable config** แทน AppData โดยอ้าง relative paths จากโฟลเดอร์ config นั้น ต้องเขียนโฟลเดอร์ได้
- คีย์ที่เข้ารหัสผูกกับบัญชี Windows นั้น การย้ายเครื่อง/บัญชีอาจต้องกรอกใหม่ หากอ่านไม่ได้โปรแกรมไม่ลบไฟล์คีย์เดิมอัตโนมัติ มีปุ่มล้างคีย์แยกพร้อมยืนยัน
- ระหว่างงานทำงานจะล็อกฟอร์มตั้งค่า เปลี่ยนกล้อง/Serial/กรอบ/ภาพอ้างอิงต้องหยุดงานก่อน และการเปลี่ยนอุปกรณ์/geometry จะต้องยืนยัน calibration ให้ตรงชุดใหม่
- รองรับ screenshot ตามขนาด config (เริ่มต้น 720×1280) ไม่ใช้ภาพเต็มจอ Windows หากเปลี่ยนความละเอียดต้องตั้ง geometry ใหม่ ไม่เดาตำแหน่งช่องรับจากภาพที่ไม่รู้จัก
- CLI ด้านล่างยังอยู่สำหรับ developer/diagnostics แต่ **ไม่ใช่ขั้นตอนบังคับของผู้ใช้ GUI** artifact รุ่นเก่าที่มี capture.exe/worker.exe อย่างเดียวไม่ใช่รุ่น GUI นี้

## ข้อจำกัดที่ต้องอ่านก่อน

- **ยังไม่ใช่โมเดลจำแนกธนบัตร** รุ่นแรกตรวจความต่างจากภาพช่องว่างเฉพาะ ROI มือ เงา แสงหรือวัตถุอื่นอาจให้ผล PRESENT ได้ ผล normal_visual ก็ไม่ยืนยันว่าตู้รับเงินหรือเพิ่มยอดสำเร็จ
- มี Auto-alignment สำหรับมุมที่ขยับภายในขอบเขตและยังเห็น landmark เดิม แต่ไม่มีตัวจำแนกมือ/ธนบัตรเฉพาะทาง ไม่รับประกันทุกมุมหรือทุกสภาพแสง ต้องใช้ shadow mode ก่อน
- เริ่มด้วย `notify_enabled: false` เสมอเพื่อเก็บผลและตรวจ false alarms ก่อน ไม่มีการส่ง Pushover จริงในการทดสอบที่ทำไว้
- Freshness เป็น heuristic ว่าบริเวณนาฬิกา OSD เปลี่ยนพิกเซล ไม่ใช่ OCR และไม่รับรองภาพสดทุกกรณี ต้องทดสอบหยุดสตรีมจริง/แสง/กล้องหลุด ไม่ใช้ screenshot timestamp ในเครื่องแทนเวลาในวิดีโอ
- UIAutomator ตรวจหน้า Live เป็นช่วง ๆ (เริ่มต้น 10 วินาที) เพราะ dump UI ทุกภาพอาจช้ากว่าเป้าหมาย 1 Hz ภาพทุกภาพยังตรวจขนาดและ clock ROI รอบที่เสียไปจะมี sequence gap และ worker เริ่มนับใหม่
- ตั้งเป้าทุก 1 วินาที **ยังไม่ได้วัดบน Windows/MEmu จริง** และอาจพลาดการหยอดสั้นกว่า sampling interval
- รองรับหนึ่งกล้องต่อ `data_dir`; ต้องอยู่ local disk ไม่ใช้ network share/OneDrive สำหรับ SQLite/spool ขณะทำงาน
- ยังไม่รองรับ Pushover Emergency acknowledgement, แนบภาพใน notification, reclaim orphan จาก crash หรือ resume ทุกกรณี การส่งปัจจุบันเป็นข้อความ priority 1 แบบ worker-reminder เท่านั้น
- ไม่รับรองการทำงานขณะ Windows sleep/ล็อกจอหรือ MEmu ถูกย่อ ต้องทดลองหน้างาน

## โครงสร้าง

```text
smart_tapo/
  capture/          # MEmu, ADB, UI safety, PNG publishing
  worker/           # consecutive proof, SQLite, async notifier, cleanup
  config.py         # typed config ที่ทั้งสองแอปใช้
  frames.py         # immutable frame / ready-marker contract
  vision.py         # prototype ROI occupancy
  alignment.py      # bounded registration จากจุดอ้างอิงใกล้ช่องรับ
  runtime.py        # one owner per process/data directory
  desktop.py        # หน้าต่างตั้งค่า/calibration/control
  desktop_storage.py # AppData / portable config / Windows DPAPI
  desktop_controller.py # spawn และ graceful stop สอง process
  desktop_calibration.py # ผูกการยืนยันภาพกับอุปกรณ์/geometry/hash
entrypoints/        # จุดเริ่มสำหรับ PyInstaller
scripts/build-windows.ps1
tests/
```

## สำหรับนักพัฒนา: CLI เดิม (ไม่ใช่วิธีใช้งาน GUI)

## 1. เตรียม Windows และ config สำหรับ CLI

ติดตั้ง MEmu, Tapo, ADB และ **Python 3.12** บนเครื่อง build (เครื่องที่รับ exe ไม่ต้องมี Python) เปิด Tapo และ login เองครั้งแรก ไม่เก็บรหัสผ่านใน config

ใน Command Prompt ที่โฟลเดอร์โปรเจกต์:

```bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy config.example.json config.local.json
```

แก้ `config.local.json`:

- `adb_path`: path ของ adb.exe จริง
- `adb_serial`: serial ที่ได้จาก `adb devices -l` ต้องระบุชัด ไม่เดาพอร์ต; TCP อนุญาตเฉพาะ loopback ของเครื่องนี้
- `memu_executable`: path ของ executable จริง ถ้าต้องการให้เปิด MEmu เมื่อยังไม่พร้อม ถ้าเว้นว่างต้องเปิด MEmu เอง
- `tapo_package`: เริ่มต้น `com.tplink.iot` ต้องตรวจว่าตรงกับแอปบน emulator จริง
- `camera_name`: ชื่อกล้องตรงตามหน้าจอ (ปัจจุบันตัวอย่าง `กล้องในตู้`)
- `expected_size`, `video`, `roi`, `clock_roi`: พิกัดตัวอย่างมาจาก PNG 720×1280 ของโปรเจกต์ ต้องคง resolution/rotation และมุมกล้องไว้

path ของ data/baseline แบบ relative อ้างจากโฟลเดอร์ **ไฟล์ config** ไม่ใช่ตำแหน่งชั่วคราวของ exe

## 2. ยืนยันภาพช่องว่างหนึ่งครั้ง

ไฟล์ `tapo-screen.png` ใช้เป็น geometry reference ได้แล้ว ไม่ต้องส่งใหม่ แต่ก่อนใช้เป็นภาพอ้างอิงช่องว่าง ผู้ใช้ต้องตรวจว่าในภาพไม่มีแบงค์จริง คำสั่งนี้ไม่แก้ไฟล์ต้นฉบับ:

```bat
.venv\Scripts\python.exe -m smart_tapo.worker --config config.local.json --calibrate tapo-screen.png --confirm-empty
```

เปิด `calibration/roi-preview.png` ตรวจว่าครอบจุดที่ต้องการ ไม่มี UI/เวลา/โลโก้ ไม่ตั้ง reference อัตโนมัติจากภาพแรก เพราะอาจมีแบงค์ติดอยู่แล้ว หากเปลี่ยน reference ต้องหยุด worker แล้วใช้ `--replace` อย่างชัดเจน

## 3. เริ่มสองแอป

เปิดสอง Command Prompt:

```bat
.venv\Scripts\python.exe -m smart_tapo.worker --config config.local.json
```

```bat
.venv\Scripts\python.exe -m smart_tapo.capture --config config.local.json
```

Capture ใช้ instance ที่เปิดอยู่ก่อน ถ้าไม่พร้อมและตั้ง executable ไว้จะพยายามเปิด MEmu หนึ่งครั้ง รอ Android เปิด Tapo และเลือกกล้องจากชื่อที่ตรงใน UI เท่านั้น หน้าจอ login/ปุ่มที่ไม่รู้จัก/ชื่อซ้ำจะหยุดการนำทางและแจ้งให้จัดการเอง ไม่แตะด้วยพิกัดสุ่ม

Ctrl+C หยุดโปรแกรมได้ โดยไม่สั่งปิด MEmu ใช้ `--once` สำหรับทดสอบหนึ่งรอบ; ภาพแรกของ capture session เป็น freshness UNKNOWN ตามตั้งใจ ต้องแคปต่อเนื่องจึงเริ่มมีหลักฐานการเดินของนาฬิกา

ดูประวัติ 100 เหตุล่าสุดหลังหยุด worker:

```bat
.venv\Scripts\python.exe -m smart_tapo.worker --config config.local.json --list-events
```

ข้อมูลอยู่ `data/worker.sqlite3` รูปเหตุการณ์อยู่ `data/events/` ภาพรอ/ผ่านการอ่านอยู่ `data/frames/`

`started` คือ first candidate seen, `last_present` คือครั้งล่าสุดที่ยังเห็น, `first_clear` คือภาพว่างแรก และ `closed` คือเวลายืนยันปิดเหตุ แยกเวลา debounce ออกจากเวลาที่วัตถุอยู่จริง ค่าทั้งหมดเป็น Unix UTC timestamps; ไม่มีการอนุมานจำนวนเงิน

## 4. กติกาที่ปรับได้

ค่าใน config เป็นแบบ flat ตาม `config.example.json` ไม่ใช่ config ตัวอย่างซ้อนในเอกสารแผนเก่า รุ่นแรกอ่านเมื่อเริ่มโปรแกรมใหม่:

- `capture_interval_seconds: 1`
- `stuck_consecutive_frames: 10`: ต้อง PRESENT ในภาพใหม่ที่ผ่าน quality ต่อกัน ภาพ 1–5 PRESENT แต่ภาพ 6 EMPTY → reset 0; ภาพ 7 เริ่ม 1 ใหม่จนภาพ 16 จึงเข้าเกณฑ์
- `clear_consecutive_frames: 2`: เป็นค่าเริ่มต้นเสนอสำหรับปิดเหตุ แต่ภาพว่างหนึ่งภาพตัด count และหยุด reminder ทันที
- UNKNOWN/STALE/sequence gap/session change ก็ตัด count; ไม่รวมภาพซ้ำ 10 ครั้งเป็น 10 หลักฐาน
- `reminder_interval_seconds: 60`: ควบคุม worker reminder ไม่ส่งทุกเฟรม และส่งได้เมื่อมีชุดภาพสดใหม่ยืนยันต่อเนื่องครบเกณฑ์เท่านั้น (บังคับไม่น้อยกว่า 60 วินาทีในต้นแบบนี้)
- 10 ภาพที่ห่างกันวินาทีละภาพครอบคลุม first-to-tenth ประมาณ 9 วินาที ไม่ใช่การรับรองค้าง 10 วินาที
- งานย้อนหลังเก็บประวัติได้ แต่ไม่สร้าง live notification; หลัง worker restart reset live proof และรักษา cooldown เดิม

### Auto-alignment เมื่อมุมกล้องเปลี่ยน

- ค่าเริ่มต้น `alignment_enabled: true` ใช้ OpenCV ORB จับคู่ landmark สองทิศทาง แล้ว RANSAC ประมาณ homography จากจุดรอบช่องรับ ไม่รวม ROI และ margin รอบ ROI ในภาพอ้างอิงและตำแหน่งที่คาดในภาพใหม่
- ไม่ใช้ UI, clock ROI หรือแถบ logo/player ด้านล่างเป็น landmark และ **ไม่เปลี่ยนภาพอ้างอิงช่องว่างเอง**
- ใช้จุดใกล้ช่องรับ (`alignment_anchor_radius_pixels: 220`) เพื่อลดผลของชิ้นส่วนตู้คนละระยะลึก ตรวจจำนวน/สัดส่วน inliers, reprojection, จุดอ้างอิงล้อมบริเวณตรวจ, ขอบเขตภาพ และ residual ของพื้นหลังใกล้ ROI ก่อนยอมรับ
- หากผ่าน จะ warp ภาพให้ตรงกับ reference ก่อนตรวจวัตถุ ภาพหลักฐาน first/threshold/clear จะเป็น **ROI ที่จัดแนวแล้ว** ไม่ใช่กรอบเดิมที่ไปครอบคนละตำแหน่ง
- เมื่อพิกัด ROI ที่ประมาณได้เปลี่ยนจากจุดอ้างอิงชุดนับอย่างน้อย `alignment_reset_pixels: 4` → ตัดช่วงเดิมและเริ่ม count ใหม่ ภาพใหม่ที่ผ่านเกณฑ์นับเป็นภาพที่ 1; jitter เล็กกว่านี้ไม่ reset ทุกเฟรม แต่การค่อย ๆ เคลื่อนสะสมเกินเกณฑ์ยัง reset
- ถ้าจัดแนวไม่ได้/ถูกบัง/ขยับเกินขอบเขต → UNKNOWN, reset count/live proof และไม่ส่ง reminder โดยเดาว่าแบงค์ยังอยู่ ไม่มีการ fallback ไปตรวจพิกัดเก่าแบบเงียบ ๆ
- ค่าเริ่มต้นยอม displacement สูงสุด 160 pixels และ rotation 15 องศา และมีขอบเขต scale ภายใน algorithm นี่เป็นขอบเขตทางเรขาคณิต ไม่ใช่รับรองความแม่นยำถึงขอบเขตนั้นทุกภาพ
- `alignment_spatial_tolerance_pixels: 2` ยอมคลาดแนวขอบเล็กน้อยหลัง warp แบบสมมาตรทั้งภาพใหม่/อ้างอิง เพื่อลด interpolation และ parallax ไม่ใช่การเรียนพื้นหลังใหม่ **ส่วนแบงค์/วัตถุที่บางระดับ 1–2 pixels อาจถูกมองข้ามได้** จึงต้องทดสอบภาพแบงค์จริง
- หากพื้นหลังใกล้ ROI ยังผิดมาก หรือความต่างใน ROI อธิบายได้ด้วย residual ของพื้นหลัง ให้ UNKNOWN แทนการสรุปว่ามีแบงค์
- Homography ไม่แก้ parallax ของตู้สามมิติทุกกรณีหรือเปิดส่วนที่กล้องมองไม่เห็นได้ หากต้องเปลี่ยนมุมมาก/ใหม่ถาวร ให้ยืนยัน ROI และ calibration ใหม่โดยผู้ใช้
- สามารถตั้ง `alignment_enabled: false` เพื่อวิเคราะห์พฤติกรรมแบบเก่าได้ แต่จะเสี่ยงแจ้งผิดเมื่อกล้องขยับ ไม่ใช่ fallback สำหรับใช้งานจริง

runtime ตอนนี้ต้องใช้ Python 3.12, Pillow 13, NumPy 2.5.4 และ OpenCV headless 5.0.0.93 ตาม requirements การแพ็ก exe จะรวม dependencies เหล่านี้และยังต้องทดสอบบน Windows

## 5. Pushover (ค่าเริ่มต้นปิด)

เมื่อตรวจผล shadow mode แล้วจึงพิจารณาเปิด `notify_enabled: true` ตั้ง credentials ผ่าน environment ของเครื่องปลายทาง ไม่ใส่ใน config/โค้ด/ภาพ/log:

```bat
set PUSHOVER_TOKEN=YOUR_APP_TOKEN
set PUSHOVER_USER=YOUR_USER_KEY
```

แสดงตัวอย่างชื่อ environment เท่านั้น ไม่ส่ง key ให้ผู้อื่น ข้อความใช้คำว่า **Suspected banknote/object stuck** เพราะเป็น detector แบบ occupancy ยังไม่มีการแนบภาพไป Pushover

ใช้ priority 1 จึงไม่มี Emergency receipt/ปุ่มรับทราบที่โปรแกรมตรวจได้ ตรวจใหม่และส่งซ้ำเมื่อยังค้างทุกอย่างน้อย 60 วินาทีตาม fallback ที่คุยกัน ไม่ผสม Emergency retry เข้ามาเอง ดูข้อจำกัดใน `notification-plan.md`

การส่งอยู่ใน thread แยกไม่หยุดการตรวจภาพ API error ไม่ถูกนับว่าสำเร็จ กรณี timeout อาจส่งถึงแล้วจึงมีสถานะ ambiguous ไม่รับประกัน exactly-once

## 6. Storage และ cleanup

- ภาพทั่วไปที่ประมวลผลแล้ว: 5 นาที (`rolling_frames_minutes`)
- รายการและภาพ normal หลังปิดเหตุ: 24 ชั่วโมง (`deposit_history_hours`)
- ภาพ incomplete หลังปิดเหตุ: 5 นาที (`non_alert_images_minutes`)
- หลักฐาน stuck หลัง resolve: 7 วัน (`alert_evidence_days`)
- ประวัติ event ชนิดอื่นและ dedup IDs: 30 วัน (`event_history_days`); normal ใช้ `deposit_history_hours` แทน
- เพดานภาพ: 512 MB (`max_image_storage_mb`) ต่อ data directory; ณ ต้นแบบหนึ่งกล้องต่อ directory
- Capture ไม่ลบงานรออ่าน หากเต็มให้พักการเผยแพร่และแสดง warning; worker ลบเฉพาะไฟล์ที่มี record ว่าผ่านการอ่านแล้ว ไม่แตะ PNG ต้นฉบับ/config/baseline
- หลักฐานที่ pending/failed/ambiguous ยังป้องกันการลบ อาจต้องจัดการสถานะเองกรณีส่งไม่สำเร็จถาวร ไม่มีการทิ้งหลักฐานเงียบ ๆ
- ยังไม่ reclaim orphan/tmp จาก crash, ไม่ได้จำกัด SQLite/WAL ด้วย image quota และ SQLite ไม่ย่อไฟล์ทันทีเมื่อ DELETE ต้องติดตามขนาดจริง
- `cleanup_interval_seconds: 30` ตรวจอายุไฟล์ตามรอบที่ตั้งใน worker ไม่เพิ่มแอป scheduler แยก

## 7. จำลองและทดสอบโดยไม่เปิดกล้อง

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m smart_tapo.simulate --config config.local.json --image tapo-screen.png
```

Simulation ใช้ ROI patch สีขาวสมมติบนสำเนาภาพ เก็บ temporary SQLite/spool แล้วลบเฉพาะ temporary directory ของตัวเอง ไม่แก้ PNG ต้นฉบับ ไม่ใช้ production data และไม่ส่ง Pushover ผลที่คาดคือ normal 1 เหตุ, stuck 1 เหตุ, shadow notifications 2 รายการที่ frame 17 และ 77 **ไม่ใช่การวัดความแม่นยำธนบัตรหรือ freshness**

## 8. แพ็กเป็น exe บน Windows

จาก PowerShell ที่โฟลเดอร์โปรเจกต์ (ต้องติดตั้ง Python 3.12 บนเครื่อง build):

```powershell
.\scripts\build-windows.ps1
```

สคริปต์สร้าง venv สำหรับ build ติดตั้งเวอร์ชันที่ pin ไว้ รัน tests แล้วใช้ PyInstaller `--onefile --windowed` สร้าง:

```text
dist/SmartTapo.exe
dist/README.md
```

นำ SmartTapo.exe ไปยัง Windows ปลายทาง ติดตั้ง MEmu/ADB แยก แล้วดับเบิลคลิกตั้งค่าผ่านหน้าต่างตามหัวข้อแรก ไม่ต้องมี Python หรือพิมพ์ `--config`

**สถานะ build:** CLI รุ่นเดิมเคย build ผ่านบน Windows Actions แต่ไม่ใช่หลักฐานว่า GUI รุ่นใหม่นี้ผ่าน การ build ใหม่ต้องตรวจ packaged Tk/DPAPI/multiprocessing จริง และ antivirus อาจตรวจไฟล์ one-file ไม่ข้าม tests เพื่อบอกว่าสำเร็จ

ผลทดสอบล่าสุดใน `VERIFICATION.md` ใช้ venv Python 3.12/Pillow 13/NumPy/OpenCV บน Mac แล้ว แต่ยังไม่ใช่ Windows build หรือการทดสอบ MEmu จริง

## 9. Build ผ่าน GitHub Actions โดยไม่ต้องติดตั้ง Python เอง

workflow อยู่ที่ `.github/workflows/build-windows-exe.yml` ใช้ GitHub-hosted **Windows Server 2022 / x64 / Python 3.12** ไม่ใช้ Mac เพื่อสร้าง Windows exe

เมื่อโปรเจกต์ถูกนำขึ้น GitHub แล้ว:

1. เปิด repository → **Actions → Build Windows EXE**
2. กด **Run workflow** เลือก branch ที่มี workflow แล้วกดรัน (ปุ่ม manual ต้องมี workflow บน default branch ของ repository ก่อน)
3. เปิด run ที่สำเร็จ → **Artifacts** → ดาวน์โหลด `smart-tapo-desktop-windows-x64-<run>-<attempt>`
4. แตก ZIP จะได้ `SmartTapo.exe`, `README.md` แล้วดับเบิลคลิก exe

ไม่ต้องติดตั้ง Python บนเครื่องใช้งาน แต่ยังต้องติดตั้ง MEmu/Tapo/ADB ตั้งค่าและยืนยัน calibration ใน GUI `notify_enabled` ยังคงปิดไว้จนผู้ใช้เลือกเปิดเอง

workflow ทำงานอัตโนมัติเมื่อ push ที่เปลี่ยนโค้ด/tests/scripts/dependency/config ตัวอย่าง/README หรือ workflow เองด้วย ไม่ผูกชื่อ branch ว่าต้องเป็น main และยกเลิก build เก่าของ ref เดียวกันเมื่อมี run ใหม่

สิ่งที่ CI ทำตามลำดับ:

- Checkout แบบไม่เก็บ credentials ลง working copy, ตั้ง Python และ cache pip dependencies
- สร้าง venv โดยใช้ interpreter ที่ `setup-python` จัดให้ ตรวจ dependency และรัน unit/integration tests ก่อน build
- PyInstaller แบบ one-file/windowed สำหรับ SmartTapo.exe
- รัน **packaged GUI จริง** ใน temporary profile: สร้าง config, สร้าง Tk widgets, calibration ในหน้าต่าง, spawn Worker จนได้ shadow event, spawn Capture กับ path ADB ที่ไม่มีอยู่จริง แล้วหยุดทั้งสอง ตรวจ DPAPI round trip เฉพาะบน Windows โดยไม่เปิดอุปกรณ์จริงหรือใช้คีย์จริง
- Upload เฉพาะ SmartTapo.exe และ README เป็น artifact อายุ **14 วัน** หลัง build/GUI smoke ผ่านเท่านั้น

ไม่แนบ `config.local.json`, token, `data/`, calibration หรือ screenshot ส่วนตัว ไม่ส่ง Pushover/เปิดกล้องจริง ไม่สร้าง release หรือ deploy ใช้ GitHub token สิทธิ์ `contents: read` เท่านั้น Actions ทั้งสาม pin เป็น commit SHA ของรุ่นทางการ

GitHub-hosted Windows runner อาจใช้ quota/ค่าใช้จ่ายตามบัญชีและ repository ของคุณ artifact ไม่ใช่ signed executable และผล build สำเร็จก็ยังไม่ใช่การทดสอบกับกล้อง/MEmu หน้างาน

**สถานะ GUI ณ พัฒนา:** รุ่น CLI ก่อนหน้าเคยผ่าน Actions แล้ว ส่วน GUI ใน branch นี้ยังต้องผ่าน Windows run ใหม่ อย่าสับสน artifact CLI เดิมกับ desktop artifact
