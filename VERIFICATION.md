# หลักฐานการตรวจต้นแบบ

## ล่าสุด: ประวัติการหยอดแบงค์และเว็บ localhost (ยังไม่เผยแพร่)

- Snapshot สุดท้าย: 90 tests รันบน Mac ผ่าน 89 ข้อ ข้าม Windows console API 1 ข้อ; source และ frozen windowed macOS smoke ผ่านทุก flag รวมประวัติ/ภาพเต็ม/เว็บ พร้อม `pip check` และ `git diff --check` ผ่าน ตรวจ native viewer เปิด PNG และปุ่มประวัติพอดีหน้าต่างขั้นต่ำ 850×650 แล้วบน Mac
- เพิ่ม GUI ประวัติเรียงเวลาเริ่มล่าสุดก่อน แบ่งหน้าละไม่เกิน 100 รายการ และดูภาพในโปรแกรมได้ ใช้ SQLite reader แบบ read-only ตรวจเจ้าของกล้องและเส้นทางภาพ ไม่เปิดฐานข้อมูลว่างขึ้นมาเอง
- เพิ่ม `deposit_history_hours=24` แยกจากค่า 5 นาทีเดิม เพื่อให้ config เก่าได้กติกาใหม่โดยไม่ rewrite ไฟล์ผู้ใช้ คิดเวลาเต็มหลังเหตุการณ์ปกติจบ และลบรายการพร้อมภาพเมื่อหมดอายุโดยยังป้องกัน active/pending/ลบไฟล์ไม่สำเร็จ
- เก็บ full screenshot ครั้งแรกของเหตุการณ์แยกเป็น `first_full` พร้อม ROI evidence เดิม ไม่เก็บทุกเฟรมเป็นวิดีโอ ภาพ raw ที่ประมวลผลแล้วคงวนลบ 5 นาที ภาพค้างยังเป็น 7 วัน ประวัติชนิดอื่นยังใช้กติกาเดิม และโควต้าภาพรวมยังเป็น 512 MB ตามค่า config
- เว็บใช้ปุ่มเริ่ม/หยุด เลือกและจำพอร์ต ไม่เริ่มเอง จำกัด 127.0.0.1 ใช้ session จากปุ่มเปิดเบราว์เซอร์ ตรวจ Host/Origin ไม่เปิด CORS ไม่รับคำสั่งแก้ข้อมูล/ควบคุม Worker และปิด listener เมื่อปิด GUI รวมเส้นทาง window failure
- ทดสอบ Chromium จริงด้วย profile และข้อมูลจำลองแยก: แสดงหน้าภาษาไทย, ลำดับล่าสุด, คลิกตามลิงก์จริง, โหลด PNG เต็ม 720×1280 และไม่มี horizontal overflow ที่ viewport 390px ผ่าน มี screenshot เฉพาะข้อมูลสังเคราะห์ใน `/tmp` ไม่ใช้ภาพผู้ใช้เป็น fixture
- เพิ่ม frozen GUI smoke ตรวจ `history_menu_created`, `history_event_visible`, `history_full_image`, `history_web_served` นอกเหนือจาก lifecycle/shadow event เดิม ผ่านทั้ง source และ macOS windowed bundle ไม่มี ADB/MEmu/Pushover จริง
- การตรวจ Windows console API/DPAPI และ frozen Windows รุ่นนี้ยังต้องผ่าน Actions หลังอนุญาต push ไม่ถือว่า Mac หรือ mocks แทน Windows จริง และไม่ถือว่าปัญหา alignment หน้างานได้รับการแก้แล้ว

## แก้ console ของ ADB เปิดแทรกหน้าต่าง GUI

- จำลอง Windows กับ adapter เดิมแล้วพบจริงว่า `Adb.run()` ไม่ส่ง `CREATE_NO_WINDOW` จากนั้นแก้ helper กลางให้ใช้ flag เฉพาะ Windows ทั้ง adapter ที่ใช้ Capture/ภาพอ้างอิงและปุ่มค้นหาอุปกรณ์
- ไม่ใช้ flag กับ MEmu launcher ไม่เปลี่ยน argv เป็น shell และยังเก็บ binary stdout/stderr/timeout/error sanitization เหมือนเดิม
- เพิ่ม regression ตรวจ flags, selected/unselected ADB commands, PNG bytes, device list และ MEmu GUI ไม่ถูกซ่อน พร้อม Windows-only test ที่เปิด child จริงและตรวจ `GetConsoleWindow()`; test หลังนี้ต้องรันบน Windows ไม่ถือว่า mock บน Mac เป็นหลักฐานนั้น
- ชุดทดสอบบน Mac รัน 60 ข้อ: ผ่าน 59 และข้าม 1 ข้อที่ต้องใช้ Windows จริง; `compileall`, `pip check` และ `git diff --check` ผ่าน
- ทดสอบ GUI source smoke ใน temporary profile ผ่าน ไม่มีการติดต่อ ADB/MEmu/Pushover จริง การแก้ยังต้อง build Windows และเผยแพร่ exe ใหม่; Release `v0.1.0` เดิมไม่มีการแก้นี้

## ล่าสุด: Desktop GUI แทนการบังคับเปิด CMD

เพิ่ม entrypoint `SmartTapo.exe` แบบ `--onefile --windowed`, Tk GUI สามแท็บ ตั้งค่า/ภาพช่องว่าง/เริ่มหยุด ไม่บังคับ `--config` ในการใช้งานปกติ ส่วน CLI เดิมเก็บสำหรับ developer เท่านั้น

หลักฐานการตรวจรุ่น GUI แยกจาก CLI:

- ชุดสุดท้าย **54 distinct tests ผ่าน** พร้อม `compileall`, `pip check`, `git diff --check` บน Mac ครอบคลุม first-run, credential preservation, calibration guard, process ownership, graceful stop และ window-failure cleanup
- Native Tk บน Mac คือ **9.0.4** เปิดหน้าต่างและสร้าง widgets จริงได้
- first-run สร้าง config ใน temporary profile, บันทึกฟอร์ม, เลือกภาพช่องว่าง/กรอบและผูก manifest, spawn Worker จนได้ shadow event, เริ่ม Capture ด้วย path ADB ที่จงใจไม่มีอยู่จริง และหยุด process ทั้งคู่ได้ ไม่มีการติดต่อกล้อง/ADB จริงหรือส่ง Pushover
- ผ่านการแพ็ก **macOS windowed application ด้วย PyInstaller 6.22.3** และรัน smoke ผ่านตัว frozen `.app` จริง ไม่ใช่เฉพาะ `python -m` ตรวจผล `gui_created`, `auto_config_created`, `capture_process_started`, `worker_stopped`, `shadow_notification`, `calibration_ready` เป็น true และ `real_notifications` เป็น 0
- เปลี่ยน workflow whitelist ให้มีเฉพาะ `SmartTapo.exe` กับ README; YAML/permissions/SHA pins/รายการไฟล์ตรวจผ่านบน Mac ไม่รวม config/คีย์/ภาพส่วนตัว
- Config ผิด/คีย์อ่านไม่ได้ไม่ถูกลบเอง ไม่มี plaintext fallback ของ credentials บน Mac ทดสอบการเข้ารหัสโดย injected test cipher เท่านั้น; implementation จริงบน Windows ใช้ current-user DPAPI
- จับคู่ calibration กับ camera/Serial/geometry/hash ป้องกันการใช้ภาพอ้างอิงผิดชุดเมื่อ save ขาดช่วง เปลี่ยน geometry หรือภาพภายหลัง และไม่ยอมตั้งภาพที่ไม่มี static anchors เป็นภาพพร้อมใช้งาน
- ช่องส่งเตือน/ตั้งค่าถูกล็อกระหว่างทำงาน หยุดด้วย shared Event และมี cleanup ทั้งเส้นทางปิดปกติ/GUI failure ไม่มีการ terminate production jobs ตามปกติ

คำสั่งตรวจสำหรับ developer:

```text
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m smart_tapo.desktop --smoke-report /tmp/smart-tapo-gui-smoke.json
.venv/bin/python -m PyInstaller --noconfirm --onefile --windowed --paths . --name SmartTapo entrypoints/desktop.py
dist/SmartTapo.app/Contents/MacOS/SmartTapo --smoke-report /tmp/smart-tapo-frozen-macos-smoke.json
```

**ไม่ใช่หลักฐาน Windows GUI ผ่าน:** Windows DPAPI จริง, frozen Windows GUI/multiprocessing, antivirus/DPI/layout และ MEmu หน้างานยังต้องตรวจบน Windows รุ่น GUI นี้ไม่ได้ commit/push ณ การพัฒนา จึงไม่มี Windows GUI artifact ใหม่ให้ดาวน์โหลดจาก run เดิม

สคริปต์ Windows เตรียมทดสอบ packaged GUI ใน temporary profile รวม DPAPI round-trip ไว้แล้ว แต่ต้องรัน Actions หลังผู้ใช้อนุญาตเผยแพร่ การเคย build CLI ผ่านที่ commit `fb7da47` ไม่ทดแทนการตรวจ GUI รอบใหม่

## ล่าสุด: GitHub Actions สำหรับ Windows EXE

เพิ่ม `.github/workflows/build-windows-exe.yml` สำหรับ Windows 2022/x64/Python 3.12 และต่อกับสคริปต์ build เดิม Actions checkout/setup-python/upload-artifact pin ด้วย SHA ของรุ่นทางการที่ตรวจ metadata แล้ว ไม่ได้รันบน GitHub ณ การตรวจนี้

ตรวจในเครื่องพัฒนาแล้ว:

- YAML parse ผ่านด้วย Ruby/Psych และตรวจ trigger, read-only contents permission, Windows runner, action SHA 40 ตัว และ artifact whitelist สี่ไฟล์ผ่าน
- Smoke helper ทดสอบด้วย subprocess ของ source Python ผ่าน: calibration, shifted occupied image, OpenCV alignment, SQLite stuck event และ shadow notification โดยใช้ temporary data และตัด credentials ออกจาก child environment
- **32 distinct tests ผ่าน** ด้วย `.venv/bin/python -m unittest discover -s tests -q`
- `compileall` สำหรับ scripts/tests/app/entrypoints ผ่าน และ `pip check` ผ่าน

ยังไม่ได้ตรวจ:

- Windows PowerShell execution ของ build script, PyInstaller Windows bundles และ packaged-exe smoke บน Windows จริง
- GitHub-hosted workflow execution, cache หรือ artifact upload/download จริง
- actionlint (ไม่ได้ติดตั้งในเครื่องนี้); การตรวจ YAML เชิงโครงสร้างไม่ใช่หลักฐานว่า GitHub runner ผ่าน

Workflow จะรัน packaged-exe smoke บน Windows หลัง build จริง โดยต้องได้ shadow event ก่อน upload จึงช่วยตรวจ DLL/imports ของ Pillow/NumPy/OpenCV มากกว่าการตรวจ `--help` อย่างเดียว ไม่มีการ commit/push/สร้าง repository/release/deploy หรือส่ง Pushover จากการเพิ่ม workflow นี้

## ล่าสุด: Auto-alignment

ใช้ Python **3.12.15**, Pillow **13.0.0**, NumPy **2.5.4** และ OpenCV headless **5.0.0.93** ใน `.venv` แยกบน macOS ไม่เปลี่ยน Python/dependencies ของระบบ เครื่องมือ bootstrap/Python อยู่ใน `.tools` ซึ่งไม่ใช่ส่วน runtime สำหรับแจกผู้ใช้

### หลักฐานก่อนและหลังแก้

- ตัวเดิมเทียบ `tapo-screen2.png` กับภาพอ้างอิง `tapo-screen.png` ด้วย fixed ROI: `PRESENT`, mean difference **69.389**, changed-pixel ratio **0.7354**
- เพิ่ม regression `test_empty_scene_translation_is_not_occupancy` ก่อนแก้ พบ FAIL เพราะฉากช่องว่างที่เลื่อนกลับถูกตัดสินเป็น PRESENT ไม่ใช่เพราะขาด dependency/import
- หลังใช้ local ORB + two-way matches + bounded RANSAC + symmetric pixel tolerance: ภาพสองได้ `EMPTY`, mean difference **9.464**, changed ratio **0.0453**, final inliers **100**, consensus ประมาณ **0.478**
- ภาพแรกเทียบตัวเองได้ EMPTY / difference=0 และภาพที่สองแสดง ROI หลังจัดแนวกลับมาครอบชุดกลไกเดียวกัน
- ผลนี้เป็นการตรวจความสอดคล้องกับ reference ไม่ใช่การติดป้าย ground truth ให้ภาพสองหรือการวัดความแม่นยำธนบัตรจากข้อมูลหลายเหตุการณ์

### ขอบเขตทดสอบที่เพิ่ม

- ฉากว่างเลื่อน/หมุนแล้วยังไม่เป็นวัตถุค้าง
- patch วัตถุที่เพิ่มใน ROI ยังตรวจเป็น PRESENT หลังขยับภาพ ไม่ถูกปรับกลืนเข้า baseline และไฟล์ baseline ไม่เปลี่ยน
- ภาพไม่มี landmark/บังทั้งหมด/มุมเกินขอบเขตเป็น UNKNOWN
- ไม่ใช้ UI, เวลา, logo และ player band เป็น landmark
- เมื่อมุมใหม่ที่ยอมรับเปลี่ยนเกินเกณฑ์ ให้ count เริ่มที่ 1 ใหม่ ไม่รวมภาพชุดเก่า; jitter เล็กน้อยไม่ reset แต่ drift สะสมยังตัดชุดได้
- UNKNOWN ตัด count/live proof และภาพหลักฐานเก็บ ROI ที่จัดแนวแล้ว
- ชุดทดสอบปัจจุบันมี **31 distinct tests** และ simulation 79 เฟรมยังได้ normal 1 / stuck 1 / shadow notifications 2 ที่ frame 17 และ 77 โดยไม่ส่ง Pushover จริง
- `pip check` ผ่าน และ `compileall` ผ่านใน venv ใหม่ ผลอ่านภาพสองพร้อม hash ยืนยันว่าไฟล์ต้นฉบับไม่ถูกเปลี่ยน
- ผู้ตรวจแยกอ่าน source ที่เกี่ยวข้องและรัน alignment regressions 6 กรณี ผ่าน ไม่พบประเด็น high/medium ใน snapshot ที่ตรวจ; full-suite 31 กรณีเป็นผลที่ parent รัน ไม่อ้างว่า reviewer รันครบทุกกรณี

ภาพที่สอง ณ การตรวจ: `352795 bytes`, SHA256 `d1d2d7510b9e76f56dfd4492cf0dfa89d6aabbf71dd05e37734ff9a76ea71f65` การอ่านภาพที่สองหนึ่งรอบหลังเปิด alignment ใช้ประมาณ 0.118 วินาทีบน Mac ครั้งที่วัด ไม่ใช่ benchmark/การรับรอง 1 Hz บน Windows

คำสั่งสำหรับชุดปัจจุบัน:

```text
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m smart_tapo.simulate --config config.example.json --image tapo-screen.png
.venv/bin/python -m pip check
.venv/bin/python -m compileall -q smart_tapo tests entrypoints
```

ไม่รับรองทุกมุม: local homography ไม่แก้ parallax/occlusion ทุกกรณี และ tolerance 2 pixels อาจมองข้ามวัตถุที่บางระดับนั้น Detector ยังเป็น occupancy rule ไม่ใช่ตัวจำแนกแบงค์ ต้องเก็บข้อมูลหน้างานและใช้ shadow mode ก่อน Windows exe, MEmu จริง และ live notifications ยังไม่ทดสอบ

## ประวัติการตรวจต้นแบบก่อนเพิ่ม alignment

สภาพแวดล้อมจริง: macOS arm64, Python **3.9.6**, Pillow **11.3.0** ที่มีอยู่ใน host ใช้เฉพาะทดสอบ logic กับไฟล์ local/temporary fixtures ไม่ใช่ dependency ชุดที่กำหนดให้ติดตั้งหน้างาน

คำสั่งล่าสุดผ่าน exit code 0:

```text
python3 -m unittest discover -s tests -v
python3 -m smart_tapo.simulate --config config.example.json --image tapo-screen.png
python3 -m compileall -q smart_tapo entrypoints tests
python3 -m smart_tapo.capture --help
python3 -m smart_tapo.worker --help
```

- **21 distinct tests ผ่าน** หลังตัด test cases ที่เคยซ้ำจาก inheritance ออก
- ครอบคลุม config, ROI ไม่ใช้ UI, invalid PNG เป็น UNKNOWN, frame path traversal, process lock, sanitized ADB errors, UI selection safety, quota, missed sample, reset ภาพ 6, ภาพซ้ำ, threshold, gap/UNKNOWN, SQLite history, stale backlog ไม่เตือนสด, restart cooldown, failed/ambiguous send, TTL และ source/symlink protection บน host
- Integration ผ่าน image → PNG + ready marker → worker → SQLite → shadow notifications
- ใช้ PNG จริงในโปรเจกต์เป็นฉาก แล้วเติม **patch สีขาวสมมติ** ในสำเนา ROI: 79 synthetic frames ได้ normal_visual 1 เหตุ, stuck 1 เหตุ, shadow notifications ที่ frame 17 และ 77 รวม 2 รายการ ไม่มีการส่งจริง
- ตรวจ hash ไฟล์ต้นฉบับก่อน/หลัง simulation ตรงกัน ไม่แก้ source PNG

PNG ต้นฉบับ ณ การตรวจ:

```text
tapo-screen.png
342002 bytes, 720×1280
SHA256 58169f400f2d528a42f739b11542d18b3f72bc4c8f676880324a34b4be0f2931
```

## ประวัติข้อจำกัด target ก่อนเตรียม venv ใหม่

ช่วงแรก runtime ที่กำหนดสำหรับ Windows คือ **Python 3.12 + Pillow 13.0.0** และ PyInstaller **6.22.3** สำหรับ build ซึ่งต่างจาก host ที่รัน 21 tests เดิม ภายหลังเพิ่ม alignment ได้เตรียม venv Python 3.12/Pillow 13/NumPy/OpenCV และทดสอบบน Mac แล้วตามหัวข้อล่าสุด แต่ **Windows packaging ยังไม่ผ่านการทดสอบ** สคริปต์ Windows ต้องรัน tests ใหม่ก่อน build

ตรวจ PyPI metadata แล้ว Pillow 13 ต้องการ Python >=3.11 และตรวจ OSV query ของ Pillow 13.0.0 / PyInstaller 6.22.3 ณ เวลาพัฒนาไม่พบ advisory ในผล query นั้น ไม่ใช่การรับรองความปลอดภัยทั้งหมด; ไม่ใช้ Pillow 11.3 ของ host เป็น production dependency

ยัง **ไม่ได้ทำ**:

- สร้างหรือรัน `capture.exe` / `worker.exe` บน Windows หรือ parse/execute PowerShell build script ด้วย PowerShell จริง
- เชื่อม ADB/MEmu/Tapo หรือยืนยัน package/UI hierarchy จริงบน Windows
- วัด capture ครบ 1 Hz โดยเฉพาะผลของ UIAutomator ที่อาจทำให้ sample ขาด
- วัดความแม่นยำแยกธนบัตรจากมือ/เงา/แสง หรือทดสอบมุมกล้องเปลี่ยน
- พิสูจน์ freshness ด้วย OCR หรือการหยุดวิดีโอจริง
- ส่ง Pushover จริง, แนบภาพ, ตรวจ acknowledgement/receipt หรือยกเลิก Emergency retries
- ทดสอบ Windows file locks, antivirus, sleep/minimized rendering, real disk-full, complete crash/orphan recovery หรือ maintenance ของ SQLite/WAL
- commit, push, deploy, สร้าง Git repository หรือแก้ไฟล์ใน MemFS

## ข้อจำกัดการส่งมอบ

นี่เป็น **ต้นแบบที่ตรวจ logic บน host แล้ว** ไม่ใช่ระบบตรวจธนบัตร production ที่ผ่าน acceptance หน้างาน ต้องเริ่ม `notify_enabled=false` และนำไปตรวจบน Windows กับชุด target ก่อนเชื่อถือการแจ้งเตือน
