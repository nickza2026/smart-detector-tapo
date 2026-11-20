# SmartTapo — Windows / RTSP

## Source V59

V59 ส่ง End-only และประกาศเสียงข้อมูลไม่ครบเป็น observation แยกจากรอบเติม โดยใช้เวลาประกาศจริง ไม่สร้างเวลาเริ่มรอบ จำนวนครั้งเติม ยอดเงิน หรือรูปขึ้นเอง การส่งใช้ ID และ payload เดิมข้ามการ retry/restart และไม่ส่งประวัติเก่าก่อนอัปเดตย้อนหลังอัตโนมัติ

ฝั่งรับต้องรองรับ `/api/v1/refueling/observations` ก่อนเปิดใช้ client รุ่นนี้กับการส่งข้อมูลไม่ครบ ดู [สัญญา API](docs/refueling-observations-api-th.md) การรับภาพและเสียงปกติใช้ RTSP; MEmu/ADB ด้านล่างเป็นข้อมูลเส้นทางเดิม ไม่ใช่ขั้นตอน RTSP ปัจจุบัน

ผลทดสอบ local ของ source V59: 1,373 เคส รันผ่าน 1,372 และข้ามตามเงื่อนไขแพลตฟอร์ม 1 เคส การสร้าง release ใช้ artifact ของ CI commit ที่ตรงกันและ signed update manifest ตาม [คู่มือ build/release](docs/build-and-release-th.md) ผล local/CI ไม่ใช่การยืนยันตู้หรือ provider จริง

## บันทึก candidate ย้อนหลัง

Historical candidate: **.39** — final clean-source846executed,845pass/1platformskip; the SAME EXE passed65cold semantic flags twice, including both supplied private MP4s through compiled matching and isolated SQLite. Explicitly selected new prepared profile adds temporal-order evidence; legacy profiles are unchanged. Real-clips/gain10/10 and three real negatives passed, but generated extra-interference completion is9/12, NOT universal low-volume/noise reliability. ZIP `.tools/SmartTapo-v39-rtsp-audio-candidate.zip`; EXE `.tools/diagnostic-v39/SmartTapo.exe`; [คู่มือ .39](docs/ram-storage-v39-th.md). **Click “ใช้โปรไฟล์จากเสียงที่เตรียมให้” while stopped to use the new kit; opening the EXE alone retains the old selected profile.** No new native RTSP/VPS/provider/user-config/history operation or production acceptance. See VERIFICATION.md for exact evidence and synthetic-tail limits.

### Historical .38 checkpoint (superseded by .39)

Latest locally verified candidate is **.38**: full/isolated-clean827executed,826pass/1platformskip; finalsameEXE62coldflags twice, including sample-clock/jitter, real audio-gap rejection and actualmainSave→exit→reopen. EXE `.tools/diagnostic-v38/SmartTapo.exe`; ZIP `.tools/SmartTapo-v38-rtsp-av-candidate.zip`; guide `docs/ram-storage-v38-th.md`. Source178/payloadhashes/CRCverified. Native-authorized SOURCE audio120s before/after33→2clockresets,maxcontinuous3.798→73.798s;remaining19/20msoverlapsstillreject. NoPCMrecording/playback/transcript/realprovider/Userhistory/configchanges. Earlier37videoalignmentfixretained. No physicalbank/announcement/phone/productionacceptance or nativeEXEqualification. VERIFICATION.md owns limits and fixture-memory evidence;37artifact unchanged.

**Known .35 issue, fixed and locally verified in .36:** Save-data-root persists successfully, then final audio cleanup after Tk destruction raises TclError. Normal exit can take the same path. Do not reset saved settings to work around it. .36 avoids destroyed-widget UI updates while still closing runtime/futures; realmainSave/exit/reopen cold checks passed. The .35 delivery remains unchanged, not silently overwritten.

**.35** adds boundedRAM/event-only main GUI, selectabledatafolder/restart, safeCapturefailurediagnostics/reconnect, and WindowsCanvas tab-remap regression. Receipt `.tools/diagnostic-v35/verification.json` owns exactartifactidentity. Historical .33 Captureexit cause remains unidentified; controlledIPCfailure recovery is not proof of that pastincident's cause. The earlierfailedcoldbuild is retained separately and is not this delivery. Previous verified **.34** remains the disk-spool baseline (full/clean770executed,769pass/1skip; cold48flags), guide `docs/storage-budget-v34-th.md`.

Previous **.33** fixes advancingPTS falsely rejected when multiple frames share a Windowsreceive tick. Full/clean754passed1skip; cold46flags. Guide `docs/rtsp-timestamp-v33-th.md`; EXE `.tools/diagnostic-v33/SmartTapo.exe`. Native metadata replay verified, not post-fix physicalLive or phone-delivery qualification.

Latest candidate **.32** fixes old720×1280 markers repeatedly resetting1920×1080 ROI proof. Full/clean752passed1skip; cold44flags includes10frames/9s with4oldmarkers retained. Generic object occupancy, not a bank classifier; no need to wait for a real jam to start. Guide `docs/worker-roi-v32-th.md`; EXE `.tools/diagnostic-v32/SmartTapo.exe`. Actual C500/provider acceptance remains unverified.

## ภาพรวม

โปรแกรมหน้าต่างสำหรับตรวจภาพวัตถุค้างบริเวณช่องรับ และบันทึกประกาศเสียงจาก Tapo ใช้ **Capture / Worker / ตัวฟังเสียงแยก process** และเก็บข้อมูลในเครื่อง ไม่ต้องติดตั้ง PostgreSQL

**สถานะ:** ยังต้องยืนยันกับกล้อง/เสียง/VPS จริง ผลทดสอบจำลองและ build ผ่านไม่ใช่การรับรองความแม่นยำธนบัตรหรือเฝ้าได้ทุกสภาพ ห้ามเริ่มด้วยการเปิดส่งแจ้งเตือนจริง

## ขั้นตอน MEmu/ADB ของเส้นทางเดิม

สำหรับเส้นทาง RTSP ดู [คู่มือภาพและเสียง RTSP](docs/rtsp-audio-v31-th.md) ขั้นตอนต่อไปนี้เก็บไว้เป็นข้อมูลย้อนหลังของเส้นทาง MEmu/ADB

1. เปิด MEmu/Tapo ที่หน้า **Live ของกล้องที่เลือก** ล็อกอินให้เรียบร้อย ไม่ใช่ Playback
2. เปิด SmartTapo ตั้ง `adb.exe`, ADB Serial และชื่อกล้องให้ตรง แล้วบันทึก ใช้ปุ่ม **ดูภาพอุปกรณ์ ADB (ไม่บันทึก)** ตรวจว่าภาพตรงกับ MEmu ที่ดูอยู่
3. หน้าภาพ ADB จะแสดง **กรอบนาฬิกาสีฟ้าและกรอบตรวจวัตถุสีแดง** พร้อมภาพขยายของกรอบนาฬิกา ถ้าสีฟ้าไม่ครอบวัน/เวลาในวิดีโอ ให้ลากใหม่ ยืนยัน และบันทึกเฉพาะกรอบนาฬิกา ไม่เลือกหัวแอปหรือบริเวณภาพที่เคลื่อนไหว การเลือกนี้ไม่ใช่ OCR หรือหลักฐาน Live ด้วยตัวเอง
4. แท็บ **ภาพช่องว่าง** ถ่าย/เลือกภาพที่ตรวจแล้วว่าไม่มีแบงค์ค้าง เลือกกรอบเฉพาะช่องรับ แล้วติ๊กยืนยันและบันทึก การเปลี่ยนกรอบไม่บันทึกเองและ Worker จะไม่เริ่มด้วย calibration ที่ยังไม่ยืนยัน
5. คลิปเดิมมีกรอบทดลอง `249,272,100,80` สำหรับภาพ `720×1280` เท่านั้น มีปุ่ม **กรอบจากคลิป 100×80 (ทดลอง)** ให้เลือกโดยไม่พิมพ์สี่ค่าใหม่ ต้องดูว่ากรอบตรงกับเครื่องปัจจุบันและยืนยันภาพช่องว่างก่อนใช้ ไม่ลด threshold เพื่อบังคับให้ผ่าน
6. เริ่ม Capture และ Worker โดยยังปิดส่งจริงไว้ ตรวจ log ว่ามีภาพสดและคะแนนวัตถุผ่าน ไม่ใช่แค่ว่า process ขึ้น “กำลังทำงาน”

`clock เปลี่ยน=False` หมายถึงพิกเซลในกรอบนาฬิกาไม่เปลี่ยน ไม่ใช่ผลว่าไม่มีแบงค์ ถ้าเวลาใน Tapo เดินแต่ log เป็น False ตลอด ให้ตรวจกรอบจาก **ภาพ ADB จริง** ไม่เดาจากขนาดหน้าต่างบน desktop

## เสียงต่อเนื่อง

- ใช้ VNC/Console session เดียวกับ MEmu ที่เห็นอุปกรณ์เสียง ไม่สลับเข้า RDP บัญชีเดียวกันระหว่างเฝ้า
- ให้ MEmu ส่งเสียงไป CABLE Input ตัวปกติ แล้วเลือก **CABLE Input [Loopback]** ใน SmartTapo พร้อมโปรไฟล์ที่เตรียมไว้ ยินยอมและกดเริ่มครั้งเดียว
- เริ่มเสียงก่อนหรือหลัง Capture/Worker ได้ ทุกงานใช้การตั้งค่าเดิมที่ตรึงไว้ระหว่างทำงาน ไม่มีการเริ่มฟังเมื่อเปิด EXE หรือสลับอุปกรณ์เอง
- ถ้าเกิด `audio_clock_discontinuity` เวลา PCM ไม่สอดคล้องเวลาจริง อาจมีข้อมูลขาดหรือประมวลผลไม่ทัน ยังใช้ lag อย่างเดียวสรุปสาเหตุไม่ได้ ระบบพักและลองใหม่ ช่วงพักไม่ใช่การรับเสียงต่อเนื่อง และไม่ต่อเหตุการณ์ข้ามช่วงที่ไม่ทราบข้อมูล
- ปุ่มหยุดแต่ละงานหยุดงานนั้น **หยุดทั้งหมด** หยุดทั้งสามส่วน

## การกู้และการนับ

- กู้ `Tap to refresh` / `แตะเพื่อรีเฟรช` ที่อ่านได้จาก Tapo หน้า Live ของกล้องเดิม มีขอบเขตเป้าหมายชัดเจน ไม่เดากดหน้า Playback/login หรือกล้องผิด
- Candidate .30 เปลี่ยนภาพเป็นRTSP URLกรอกได้ ใช้ตัวรับ/decoderและWorkerแยกprocessจากเสียงWindowsเดิม ตัดหน้าจับภาพMEmuออก เก็บประวัติ/รูปเก่าข้ามความละเอียดและกติกาแบงค์ติดเดิม ต้องยืนยันภาพช่องว่าง/กรอบใหม่ก่อนเริ่ม ดู `docs/rtsp-video-v30-th.md` ทดสอบเฉพาะRTSPloopbackที่สร้างเอง ไม่เปิด8554/เสียงจริง/ส่งแจ้งเตือนโดยอัตโนมัติ
- .29เป็นdiagnosticsของภาพMEmuเดิม ไม่ใช่ตัวรับภาพRTSP ดู `docs/live-ui-diagnostics-v29-th.md`; ภาพRTSPของ30ไม่ใช้Liveproof MEmu แต่เสียงยังยืนยันแหล่งเดิมแยกต่างหาก
- คง.28แก้Save startup**Stable Windows endpoint identity unavailable** อ่านWindowsMMDeviceแทนrawRegistry ดู `docs/startup-endpoint-v28-th.md` ไม่ข้ามguard
- คง.27**ทดสอบฟัง Playback (TEST)** รับเสียงTapoPlaybackในMEmuโดยตรง ไม่ต้องอัปโหลด/เลือกไฟล์ outputGUIDที่เลือกและยืนยัน ดูระดับ/คะแนน/วลีห่างหลายนาทีไม่สร้างรายการจริง ดู `docs/playback-test-v27-th.md` actualaudio/loginยังไม่ยืนยัน
- ปุ่ม**ทดสอบ Notify API จริง (critical)**ที่Settings ใช้ Base URL/key ที่กรอกและยืนยันก่อนส่งหนึ่งเหตุการณ์ มือถืออาจปลุกต่อเนื่องจนกดรับทราบในมือถือ การยกเลิกรอหรือปิดหน้าต่างไม่หยุดเสียง ไม่เปิดnotifyอัตโนมัติ/ไม่หยุดCapture แสดงprovideracceptanceไม่ใช่phoneAck และแสดงรหัสผิดพลาด/HTTP status ที่ปลอดภัยเมื่อส่งไม่สำเร็จ devทดสอบfakeเท่านั้นไม่ยิงจริง
- ปุ่ม**ประวัติการส่ง Notify** แสดงทุก HTTP attempt รวม retry ทั้งสำเร็จ/ล้มเหลว/ไม่แน่ชัดย้อนหลัง 30 วัน กดดู URL, request body และ response ได้ โดยไม่เก็บ Authorization/API key จริง อยู่ในโฟลเดอร์ข้อมูลที่เลือกและไม่มีปุ่มส่งซ้ำ ดูสัญญาและเพดานข้อมูลใน `docs/notify-api-client-th.md`
- คง.25**ทดสอบไฟล์ออฟไลน์ (TEST)** อ่านWAVPCM16ยาวแบบboundedwindow ไม่จำกัดคลิป20s จำวลีข้ามหลายนาทีและแสดงช่วงห่างTEST/progress/เหตุไม่ครบ ไม่สร้างรายการ/ส่งจริง ไม่เล่นเสียง/เปิดTapoPlayback MP4ยังไม่รองรับ ดู `docs/audio-file-test-v25-th.md` ผลไฟล์ไม่พิสูจน์ช่องเสียงMEmuหรือสาเหตุเคสพลาดจริง
- คง.23opt-inWindowsloginเปิดMEmu/Tapo/ภาพ/เสียงเดิมโดยผูกAndroididentity/GUID/profile หยุดยกเลิกretryและไม่fallbackไมค์ เพิ่มhealthlogไม่เก็บเสียง ดู `docs/startup-v23-th.md` actualboot/เสียงลูกค้าทุกเคสยังไม่ยืนยัน
- คง.22Webhookstatusไม่ถูกpollทับเป็นหยุดอยู่และไม่บอกเริ่มซ้ำผิดสถานะ ดู `docs/refueling-v22-th.md` ยังไม่ยืนยันkey/receiverจริง ไม่เพิ่มvisual-onlyAPI/auto-armWebhookข้ามsession
- คง.5capture/2clock boundedpendingwindowsจาก.21 เปิดpreset+Saveไม่เร่งเกณฑ์ค้าง Native21พบwindowmetadataและกู้2รอบ ไม่ใช่bankaccuracy/audio/APIACK ดู `docs/refueling-v21-th.md`และ`VERIFICATION.md`
- คงกู้knownTapo5minContinueจาก.20แบบไม่ติดexpiredpriorproof ไม่กดblindทุกหน้า ยืนยันกล้อง/Liveใหม่ก่อนใช้ภาพ native.20สังเกต1รอบกู้ครบแล้ว ไม่ใช่หายทุกกรณี ดู `VERIFICATION.md`
- คงแท็บRefuelingAPIv1จาก.19กรอกBaseURL/APIkeyDPAPIผูกURL+กล้อง ส่งคิวรูป→eventsเมื่อบันทึกสิทธิ์/ยืนยัน/กดเริ่ม มีหยุด/ACK/retry/กู้คิวแบบopt-in ยังไม่ยืนยันส่งreceiverจริง
- คงกู้คำถาม5นาที→รีเฟรชก่อนreconnectจาก.18 โดยไม่ขยายTTL/ข้ามชื่อกล้อง baseline.18พบเส้นทางกู้→freshclock4รอบจริงจากlog ไม่ใช่การยืนยันเสียงหรือผลnative.19 ดู `VERIFICATION.md`
- การตรวจ UI เบื้องหลังไม่ขวางการปล่อยภาพด้วย mutex เดียวกัน การปล่อยภาพ/ตัดสินผลยังป้องกัน navigation ด้วย generation fence และ decision lock
- ถ้า Worker รอ decision lock ไม่ทัน จะเก็บเฟรมเดิมไว้ลองใหม่ตาม poll โดยไม่เพิ่มจำนวนและไม่ให้เฟรมหลังแซง ต้องตรวจ freshness/navigation/boot ใหม่ก่อนใช้ผล
- **ยังไม่ผ่อนกติกาครบ 10 ภาพหรือเกณฑ์ gap โดยอัตโนมัติ** ข้อเสนอใน recommendations ที่เปลี่ยนกติกาต้องยืนยันก่อน UNKNOWN, ภาพไม่สด, navigation และ boot คนละรอบยังตัดหลักฐาน

## ประวัติและข้อมูล

มีแท็บประวัติวัตถุค้าง/ประวัติการเติมพร้อมรูปที่ผูกไว้ในเครื่อง และเว็บประวัติแบบ loopback การตรวจภาพเป็นวัตถุที่อาจเป็นแบงค์ ไม่ใช่การยืนยันชนิดธนบัตร การชำระเงิน ปริมาณน้ำมัน หรือความเป็นลูกค้าคนเดียวกัน

Webhook, การผูกรูปเข้ารอบเสียง และเสียง→Pushover ยังไม่ใช่การเชื่อม production ที่พร้อมใช้ ดู [สถานะตามคำแนะนำ](docs/recommendations-progress-th.md) ก่อนคาดหวังว่ารอบเติมถูกส่งถึง dashboard แล้ว

ข้อมูลค่าปริยายอยู่ใต้ `%LOCALAPPDATA%\SmartTapo` ของบัญชี Windows เดิม ถ้ามี `config.local.json` ข้าง EXE จะใช้ชุด portable นั้น Notify API key เก็บด้วย DPAPI ของบัญชีเดิมใน `notify-api.dat` ผูกกับ Base URL และรหัสกล้อง ไม่อยู่ใน config/log

แจ้งเตือนอัตโนมัติ (วัตถุค้างจาก Worker และสุขภาพระบบภาพ/เสียง) ส่งผ่าน Notify API ตาม [notify-api-integration-th.md](notify-api-integration-th.md) แทน Pushover แล้ว ตั้ง Base URL และ API key ในแท็บตั้งค่า วัตถุค้างส่งแบบ `critical` เสมอ ส่วนระดับ `normal`/`critical` ที่เลือกได้ใช้กับแจ้งระบบภาพ/เสียงขัดข้อง และแจ้งกลับมาปกติส่งแบบ `normal` เสมอ คีย์ Pushover เดิม (`pushover.dat`) ไม่ถูกอ่านหรือลบ ต้องกรอก Notify API key ใหม่ รายละเอียด client ดู `docs/notify-api-client-th.md`

อัปเดตโดยหยุดทุกงาน ปิดรุ่นเก่า แล้วแทนเฉพาะ EXE ในโฟลเดอร์เดิม **ไม่ลบ config/data/ภาพอ้างอิง/โปรไฟล์/ประวัติ** ไม่เปิดสองรุ่นซ้อนกัน

## พัฒนาและตรวจสอบอย่างปลอดภัย

ใช้ Python/dependencies ตาม `pyproject.toml` และ `requirements.txt` ไม่มี private PNG รวมมาใน repository ตัวอย่างจำลองสร้างภาพสังเคราะห์และใช้ฐานข้อมูลชั่วคราวของตัวเอง ไม่เปิด ADB/เสียงหรือส่ง Notify API:

```powershell
.venv\Scripts\python.exe -m smart_tapo.simulate --config config.example.json --synthetic
.venv\Scripts\python.exe .tools\run_local_checks.py
```

`.tools/run_local_checks.py` เป็น runner เฉพาะเครื่องพัฒนาที่จัดหน้าต่างออกนอกจอ ไม่ใช่ไฟล์ที่รับประกันว่ามีใน checkout สะอาด สำหรับชุดทดสอบมาตรฐานใช้ `python -m unittest discover -s tests -v`; การทดสอบ GUI บนเครื่องที่มีผู้ใช้ทำงานอยู่ต้องใช้ policy นอกจอ/no-focus ไม่รันให้บังงาน

## เอกสาร

- [ผลตรวจและข้อเสนอแนะ](docs/recommendations-th.md) — แยกหลักฐานออกจากสมมติฐาน
- [สถานะการทำตามคำแนะนำ](docs/recommendations-progress-th.md)
- [หลักฐานการตรวจแต่ละรุ่น](VERIFICATION.md)
- [Refueling API: หลักฐานเสียงข้อมูลไม่ครบ](docs/refueling-observations-api-th.md) — observation แยกจากรอบเติมและยอดเงิน
- [คู่มืออัปเดต .13](docs/refueling-parallel-v13-th.md) — ประวัติฐานก่อนแก้ contention
- [เอกสาร CLI/รายละเอียดรุ่นก่อน](README-legacy-th.md)
- Candidate .31 ใช้RTSPทั้งภาพและเสียง เอาMEmu/ADB/Windowsloopbackออกจากเส้นทางปกติ พร้อมstartupv2และretryแหล่งเดิมหลังdecoderค้าง มีPyAV/FFmpegbundled ไม่ใช้FFmpeg/PATHระบบ ดู `docs/rtsp-audio-v31-th.md` Full/clean744เคส743ผ่าน1ข้าม และcoldEXE42flagsผ่านบนownedA/Vloopback ไม่ใช่C500/production; artifact30ยังเป็นเสียงWindowsเดิม
