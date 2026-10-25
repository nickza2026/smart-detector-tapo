# หลักฐานการตรวจต้นแบบ

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
