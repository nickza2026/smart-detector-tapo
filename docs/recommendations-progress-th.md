# สถานะการทำตาม recommendations-th.md

เริ่มแก้ 30 ตุลาคม 2026 หลังผู้ใช้สั่งให้อ่านและทำตาม ใช้ working tree เดิมและรักษางานที่ยังไม่ commit เอกสารคำแนะนำต้นฉบับไม่ถูกเขียนทับ ข้อมูลด้านล่างแยกสิ่งที่แก้ในโค้ดออกจากสิ่งที่ยังต้องยืนยันบน VPS

## สิ่งที่แก้ในรอบ .14

| ข้อ | การดำเนินการ | ข้อจำกัด |
|---|---|---|
| 1 ROI | เพิ่มปุ่มกรอบจากคลิป `249,272,100,80` สำหรับภาพ `720×1280` เป็น preview ที่ยังไม่ยืนยัน ไม่เขียน baseline/config เอง; เปลี่ยนกรอบแล้ว Worker ยังเริ่มไม่ได้จนยืนยันภาพช่องว่าง | ไม่ได้ยืนยันว่ากรอบจาก Playback ตรง layout VPS ปัจจุบัน ไม่ลด threshold และไม่เปลี่ยนค่า VPS |
| ปัญหา clock False ล่าสุด | ภาพ ADB แสดงกรอบนาฬิกา/ภาพขยายและกรอบแบงก์จริงตาม config; ลากกรอบเวลาในวิดีโอ ยืนยัน แล้วบันทึกเฉพาะ clock ROI ได้ โดยไม่ต้องพิมพ์ JSON และไม่แก้ baseline | ไม่ตั้งพิกัดใหม่จาก screenshot desktop โดยเดา ไม่อ้างว่าแก้ ROI ของ VPS ให้แล้ว ภาพเดียวไม่พิสูจน์ว่าเวลาเดิน |
| 2.1 capture lock | เปลี่ยน publication ไปใช้ decision/navigation lock ที่แยกจาก UI dump คงตรวจ snapshot/generation/recovering/cutoff และไม่ปล่อย navigation แทรกช่วงตรวจ→publish | ยังต้องยืนยันเวลา capture จริงบน VPS ไม่อ้างว่าดิสก์/ADB/CPU ไม่เป็นคอขวดอื่น |
| 2.2 worker busy | defer เฟรมเดิมเมื่อ lock busy; หยุด scan ไม่ให้เฟรมหลังแซง แล้ว retry ตาม poll ปกติด้วย snapshot/inference ใหม่ ไม่เพิ่มจำนวนหรือแต่งผล UNKNOWN จากการรอเพียงอย่างเดียว | lock พัง/permission/สถานะไม่ถูกต้องยัง fail closed; commit/enqueue ยังอยู่ใน navigation lock ไม่ใช้ snapshot-only ที่มี race |
| 4.1 refresh | ตรวจ refresh หลังยืนยัน activity/ชื่อกล้องและ veto แต่ก่อนบังคับครบทุก control เพื่อให้ overlay ซ่อนปุ่มแล้วยังกู้ได้ | ไม่ลดเงื่อนไข Live ปกติ ไม่เพิ่มคำแปลที่ไม่มี fixture ไม่กดผ่านหน้าไม่รู้จัก |
| 4.4 reason | ส่งเหตุผล PageMode ที่อยู่ใน allowlist ไป log/GUI เช่น `incomplete_live_layout` และแจ้งการ defer โดยไม่ปล่อย XML/ข้อความ exception ดิบ | ยังต้องได้ dump ตอนเกิดปัญหาจริงสำหรับ layout/ภาษาที่ไม่รู้จัก |
| 9 เอกสาร | README ปัจจุบันแยกวิธี GUI/ข้อจำกัด/คำสั่งจำลองออกจากประวัติยาว ย้ายรายละเอียดเดิมไป `README-legacy-th.md`; simulation มี `--synthetic` ไม่ต้องใช้ PNG ส่วนตัวที่ไม่มีใน checkout | ไม่ใช้ภาพสมมติเป็นหลักฐานความแม่นยำจริง |

## เพิ่มเติมใน candidate .15 และการตรวจเครื่องหลัก

- ได้ภาพ ADB จริงบนเครื่องหลักแล้ว: `720×1280`, video `[0,176][720,581]`; กรอบเริ่มต้น y132 ไม่เปลี่ยน แต่บริเวณเวลา y176 เปลี่ยนทั้งสองคู่ ไม่เหมารวมว่า VPS ใช้ geometry/config เดียวกัน และไม่ได้แก้ config/baseline ผู้ใช้เอง
- ได้ fresh sanitized XML ป๊อปอัปดู Live 5 นาทีภาษาไทยแล้ว เพิ่มการกู้เฉพาะเมื่อมีหลักฐานกล้องเดิมล่าสุดใน session เดียวกัน อายุเดิม/generation เดียวกัน ไม่ถือ modal ที่บังชื่อกล้องเป็น Live ตรวจ UI ใหม่ภายใน lock ก่อนกดเพื่อไม่แตะปุ่มอื่นเมื่อ modal หาย และต้องยืนยัน Live ใหม่หลังแตะ
- ป๊อปอัปตอนเริ่มที่ไม่มีหลักฐานกล้องก่อนหน้าไม่ถูกกด การหยุด/หมดอายุ/เปลี่ยนกล้อง/หน้าอ่อนไหวต้องไม่แตะ; ช่วงเสียงเปลี่ยน generation จบ epoch เก่าก่อนให้ Watch เริ่มใหม่ ไม่ต่อเหตุการณ์ข้ามช่องว่าง
- ตรวจ Capture จริงเดี่ยวและ CABLE จริงเดี่ยวได้ แต่รอบคู่เคยล้ม `audio_clock_discontinuity` lag2.01 และ fresh UI ล้มชั่วคราว ยังไม่ยืนยันสาเหตุและไม่ผ่อน 2 วินาที
- รอบตัวฟังเดี่ยว 120 วินาทีจบ แต่มี epoch เริ่มใหม่ จึงไม่เท่ากับเสียงต่อเนื่อง 120 วินาทีหรือความแม่นยำประกาศจริง
- รอบยาว Capture+Watch/autoContinue ใหม่ยังไม่เริ่ม: guard พบ Windows Default Output เป็น CABLE ต่างจากตอนแรกที่เป็น Bluetooth speaker จึงหยุดก่อนเปิด stream ไม่เปลี่ยน route/volume/mute แทนผู้ใช้ สาเหตุที่ Windows เปลี่ยนยังไม่ทราบ และไม่มีการบันทึก PCM จริง
- ไม่มี Worker ตรวจแบงก์จริง/ยืนยันภาพช่องว่างปัจจุบัน ไม่มี Pushover/เงิน/การควบคุมตู้จริง รายละเอียดและขอบเขต candidate: `refueling-v15-th.md`

## เพิ่มเติม .16 candidate: ลดเวลารอกู้เสียง

ลดเฉพาะ cooldown ของ clock-discontinuity/audio-timeout/navigation-change/transient-live-check เป็น `0/.25/.5/1/2` วินาที ไม่รอ30วินาทีแบบเดิม ส่วน profile/device/ข้อผิดพลาดอื่นยังค่อยถอยตามเดิม ต้องreapและตรวจLive/profile/deviceใหม่ทุกครั้ง ไม่รับประกันว่ากลับฟังจริงภายใน2วินาที ไม่ต่อช่วงขาดหรือสร้างendปลอม และหากพลาดทั้งประกาศเริ่ม/จบอาจไม่มีรอบลูกค้านั้นในประวัติเลย ไม่ใช่0ลูกค้า

ทดสอบ439เคส438ผ่าน1ข้ามทั้งcheckout/clean-sourceและfrozenGUIนอกจอผ่าน ไม่มีฟังเสียงจริงในงานนี้ Default/Communicationsที่อ่านล่าสุดเป็นSpeakers/Headphonesโดยไม่ได้แก้ค่า ยังต้องยืนยันsource isolationและเวลารวมจริงต่อ ดู`refueling-v16-th.md`และreceipt ไม่ใช่VPS/ธุรกรรมพร้อมใช้งานจริง

## เพิ่มเติม .17 candidate: popup และรีเฟรชภาษาไทย

รองรับThai refreshที่อ่านจากhierarchyได้ ไม่ถือcontrolsด้านหลังเป็นLive กู้Continueแล้วrefreshอีกขั้นภายในbudgetเดิม ลดUIอ่านซ้ำที่ทำให้สิทธิ์10sหมดก่อนกด โดยรอทั้งUI/decisionlockก่อนเก็บXMLที่ใช้แตะ คงTTL/Stop/กล้อง/epoch/generationและreadonlyperiodicprobeเดิม แก้ข้อความGUIของretry0/.25/.5ที่แสดงผิดเป็น30ด้วย

RGRของThai/สองขั้น/expiryจากdumpซ้ำและข้อความdelayผ่าน Full444เคส443ผ่าน1ข้ามทั้งcheckout/clean-sourceและfrozenGUIนอกจอผ่าน ไม่ใช่nativeacceptance ล่าสุดอ่านMEmuเครื่องหลักได้Liveไม่มีmodal ไม่ได้tapหรือเปิดเสียง ยังรอยืนยันhostของภาพป๊อปอัปและสถานะตัวฟังของเคสลูกค้าที่ภาพเจอแต่เสียงไม่เจอ ดู`refueling-v17-th.md`และreceipt

## เพิ่มเติม .18 candidate: กู้ก่อน reconnect

Wholecaptureloopของ.17ยังส่งknownpopupไปทางreconnect ทำให้สิทธิ์10sหมดระหว่างเชื่อมใหม่แล้วปฏิเสธซ้ำ แก้knownmodal/refreshบนtransportเดิมก่อน fallbackถ้ากู้ไม่ผ่าน คงTTL/Stop/ชื่อกล้อง/fence/gap/sessionclockเดิม RGRcontrolledreconnect11sจาก75connects→1และกู้20รอบvirtual;ไม่ใช่100นาทีจริง เพิ่มsafeauditเริ่ม/ยืนยันกู้และreasonสิทธิ์กล้องขาดเพื่อสังเกตจากlogโดยไม่บันทึกเสียง

Full449เคส448ผ่าน1ข้าม checkout/clean-source/frozenGUIนอกจอผ่าน Guide`refueling-v18-th.md`และreceipt ไม่มีnative.18acceptanceหรืออัปเดตEXEผู้ใช้อัตโนมัติ เริ่มเฝ้าreadonlylog20minโดยนับเฉพาะ.18started→restored→freshclock ไม่ต้องรอลูกค้าจริง ไม่ถือเปิดหน้าจอนานหรือ.17เป็นผ่าน ยังมีกรณีpopupไม่มีproofที่ต้องเปิดselectedLiveก่อนเริ่มหนึ่งครั้ง

## ยังรอการตัดสินใจหรือหลักฐาน

01:51.21candidate:full543เคส542ผ่าน1ข้าม+clean-source+FrozenGUIจริงนอกจอผ่าน แยก.5capture/2health boundedpendingProof/ภาพผ่านสั้นรักษาเวลาเดิม/knownfreezeต้องreprime/noearlyready/ภาพหายไม่publish/Stopcleanup และแก้Settings650clockinputถูกตัดด้วยscroll+Tab ไม่ยืนยันactual2FPS/CPU/แบงค์ทุกใบ/native21/audio/receiverจริง UIเสียงunmatchedยังพักตามคำขอ guide`refueling-v21-th.md` Native20watchจบexit0พบ1รอบ01:13ไม่ใช่ผล21 ไม่มีwatch20ค้าง

31ต.ค..20:เพิ่มช่องinterval/preset.5พร้อมguardspanเดิม,แก้exactknown5minContinueไม่ติดexpiredpriorproofโดยยังfreshverifyselectedLiveก่อนใช้ภาพ และแก้clockซ้ำsubsecondไม่รีเซ็ต/ไม่นับคะแนนเพิ่มผิด Full492เคส491ผ่าน1ข้าม source/clean-source/frozenGUIจริงนอกจอผ่าน คู่มือ`refueling-v20-th.md` Noactualnative/2FPS/audio/providerรับรอง.20 ยังไม่เพิ่มUIเสียงเริ่ม/จบที่ผู้ใช้พักไว้และไม่แต่งรอบจากend-only

อัปเดต.19:มีWebhookGUI+scopedDPAPI+explicitqueuebinding/recoveryตามAPIv1เดิมแล้ว Full467เคส466ผ่าน1ข้ามทั้งcheckout/clean-sourceและfrozenGUIจริงนอกจอ/fakeclientภาพ→completed/ACKdurableผ่าน ไม่มีบริการจริง หลักฐาน`refueling-v19-th.md`และreceipt รุ่น.18runtimeเก็บlogกู้→freshclockครบ4รอบsame-runแล้ว ไม่เท่ากับaudio/receiverE2E.19ผ่าน

- **2.3 กติกานับ:** เสนอให้ยังครบ 10 ภาพสดจริง ยอมขาดไม่เกินหนึ่งลำดับเมื่อระยะห่างไม่เกิน 3 วินาที แต่ยังไม่ได้รับคำยืนยัน จึงคงกติกา sequence/gap เดิม ไม่ขยายช่วงเอง
- **2.4 proof TTL:** ไม่ขยายจาก 10 เป็น 30 วินาทีหรือมองข้ามผลลบเอง การแยก lock แก้สาเหตุ contention ก่อน การผ่อนความสดเป็นคนละการตัดสินใจ
- **3 เปิดเอง/นำทาง/Windows:** ยังไม่ติดตั้ง service/Task Scheduler/auto-logon หรือเปลี่ยน Windows การเริ่มเสียงยังต้องยินยอม ไม่เพิ่ม guessed Home/Back หลังหน้าไม่รู้จัก ระยะถัดไปต้องยืนยันเงื่อนไขและ fixture
- **4 ภาษา/layout:** ต้องมี fresh sanitized XML จริงในหน้า refresh/popup ก่อนเพิ่มข้อความหรือผ่อน normal Live contract
- **5 Git:** ไม่มี commit/push การตรวจ source snapshot ที่รวมโมดูลใหม่ใช้ยืนยันว่าไม่พึ่ง private assets ได้ แต่ยังไม่ใช่การพิสูจน์ว่าไฟล์ untracked ถูก commit แล้ว เมื่ออนุมัติส่ง Git ต้องรวม runtime/tests/docs ที่พร้อมทั้งหมดและตรวจ secrets ทั้งไฟล์และ outgoing history
- **6 webhook/รูป/เสียงแจ้งเตือน:** เลื่อนตามคำแนะนำจนฐานภาพผ่านการยืนยันจริง ต้องตกลง URL/API/รูปช่วงเวลา ไม่ค้นหา token หรือส่งข้อมูลออกเอง
- **7 quota:** ยังไม่มีขนาดข้อมูล VPS จึงไม่เพิ่ม quota/ลดอายุข้อมูล/แปลงภาพย้อนหลังโดยเดา
- **8 เสียง:** มีผลจริงในเครื่องหลักตามข้างต้นแล้ว แต่ยังไม่ยืนยันสาเหตุ lag/ผลหลังปิด Viewer บน VPS ต้องคืนการแยกเส้นทางเสียงหลักก่อนทดสอบต่อ ไม่เพิ่มเกณฑ์ 2 วินาทีเพื่อซ่อนอาการ

## การตรวจสอบ

Regression ของ capture contention และ worker ordered defer ล้มเชิงพฤติกรรมก่อนแก้ และผ่านหลังแก้ใน snapshot แยก GUI clock/ROI ทดสอบนอกจอ ไม่แย่งโฟกัส ผล fullsuite/frozen EXE และเลข hash ให้ดู `VERIFICATION.md` ของรุ่นที่สร้างจริง ไม่ใช้ข้อความนี้แทนผลทดสอบที่ยังไม่เสร็จ

มีการเชื่อม ADB/เสียง CABLE จริงเฉพาะ MEmu เครื่องหลักที่ผู้ใช้อนุญาตแล้ว ไม่มีการเชื่อม VPS ส่ง Pushover หรือใช้เงินจริง/สั่งตู้จริง ไม่อ้างว่าไม่มีแหล่งเสียงอื่นปนตลอดช่วงก่อนพบ Default เปลี่ยน และข้อ 1–4/false-positive acceptance ยังไม่สำเร็จเพียงเพราะ local tests ผ่าน
