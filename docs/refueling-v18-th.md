# SmartTapo .18 candidate — กู้รอบถัดไปก่อน reconnect ที่ไม่จำเป็น

รุ่น `2026-10-30.18` แก้ลูป Capture หลังตรวจพบ popup/refresh ที่รู้จัก ไม่ใช่การยืนยันว่าปัญหาลูกค้าจริงหรือทุกกรณีของเครื่องจริงหายแล้ว

## สาเหตุที่ทำซ้ำได้

เดิมผลตรวจ UI แบบอ่านอย่างเดียวที่พบคำถาม5นาที/รีเฟรชถูกส่งเข้าทาง reconnect ทั้งหมด แม้เพิ่งอ่าน Android สำเร็จและ transport ยังใช้ได้ reconnect และช่วงพักจึงอาจกินสิทธิ์ selected-camera ที่มีอายุ10วินาทีจนหมด เมื่อหน้า popup บังชื่อกล้องและไม่มี Live ใหม่ หลักฐานไม่ถูกเติมกลับ ทำให้รอบถัดไปปฏิเสธซ้ำ

Whole-loop regression จำลอง reconnect11วินาที: ก่อนแก้ reconnect75ครั้งภายในbudgetทดสอบแทน1ครั้งและกู้20รอบไม่ได้ หลังแก้เชื่อมครั้งแรก1ครั้ง กู้20รอบและยืนยันLiveใหม่ครบ20รอบ (ช่วง5นาทีเป็นvirtual time ไม่ใช่การรัน100นาทีจริง) ไม่ใช้ SDK/ADB/เสียง/ฐานข้อมูลจริง

## สิ่งที่เปลี่ยน

- หาก Capture กำลังต่ออยู่และ verifier ยืนยันว่าพบ `live_inactivity_confirmation` หรือ `live_refresh_required` ให้กู้แบบboundedบนtransportเดิมก่อน ไม่พัก/reconnectล้างเส้นทางก่อนกู้
- ต้องได้ PageMode Live จริงและ navigation generation ใหม่ก่อนกลับไปปล่อยภาพ รีเซ็ต session/clock comparison ไม่ปล่อยภาพหรือเสียงข้ามช่วงกู้
- ถ้ากู้ไม่ผ่านจริงยังกลับไปทาง reconnect เดิม กล้องผิด/หน้าไม่รู้จัก/Playback/SDKเสียไม่ได้สิทธิ์กู้พิเศษ Stopยังชนะ ไม่ลดTTL10s ไม่ใช้พิกัดเก่า ไม่กดTalk/Alarm
- เพิ่มlogปลอดข้อมูลลับ `ui_recovery_started` / `ui_recovery_restored` และ `ui_reason=selected_camera_authorization_missing` เพื่อแยกการกู้อัตโนมัติสำเร็จกับสิทธิ์หมด ไม่เหลือแค่connectionว่าง ไม่มีการบันทึกPCM/credential/UI XMLลงlog
- คงThai refresh/Continue→refreshและretryเร็วจนถึง2วินาทีจาก.17 พร้อมข้อจำกัดตาม `refueling-v17-th.md`

## ข้อจำกัดสำคัญ

**แก้การหมดสิทธิ์จาก reconnect ที่หลีกเลี่ยงได้ ไม่ใช่ยกเลิกการตรวจกล้อง** หากเริ่มมาเจอ popup ค้างโดยไม่มีหลักฐาน หรือหลักฐานหมดจากการหลุด/หน้าเปลี่ยนจริง จะยังไม่กด Continue มั่ว ต้องเปิด Live กล้องที่ถูกต้องใหม่หนึ่งครั้งก่อนเริ่มเฝ้า การที่รูป popup ปรากฏอย่างเดียวไม่พอจะยืนยันกล้องจาก hierarchy ที่ซ่อนชื่อไว้

เส้นทางที่อยู่นอก regression หรือ UI ที่ไม่เคยเก็บ hierarchy ยังต้องตรวจ ไม่มีคำรับประกันว่าไม่พลาดเสียงลูกค้า: หากพลาดทั้งเริ่ม/จบรอบนั้นอาจไม่มีประวัติ ไม่ใช้ช่วงเงียบแทน0ลูกค้าและไม่สร้างendปลอม

## สิ่งที่ตรวจเครื่องจริงในรอบนี้

- SHA256ของEXEที่กำลังเปิดตรงreceipt **.17** ไม่ใช่เดาจากชื่อโฟลเดอร์
- อ่านเฉพาะtypedcapturelogจากWindowsKnownFolder `LocalAppData/SmartTapo/data/logs` ไม่อ่านpushover.dat/credential/ฐานข้อมูลจริง พบconnectionซ้ำในช่วง20:31–20:35 และภายหลังมีfreshframe.17ช่วง20:43–20:44 ไม่สรุปว่าการกลับมานั้นเป็นการกู้อัตโนมัติหรือผู้ใช้กดเอง
- ReadonlyADBล่าสุดได้Live layout87nodes/ชื่อกล้องตรง ไม่มีmodalในhierarchy; ไม่กดปุ่ม ไม่เปิดเสียง ไม่เปลี่ยนforeground/Windowsaudio ทั้งnormalและ`--windows`บนSDK28ให้layoutเดียวกัน ไม่ใช่หลักฐานว่าสามารถอ่านชื่อกล้องใต้modalได้
- Native .18หลายรอบ/เวลารวมกู้/เสียงครบวงจร/VPS/โทรศัพท์ยังไม่ยืนยัน ไม่มีrealtimeprovider/เงิน/Windowssettingschange/commit/push/deploy

## ไฟล์และอัปเดต

```text
D:\works\smart-detector-tapo\.tools\SmartTapo-v18-repeat-recovery-candidate.zip
```

ไฟล์อยู่เครื่องพัฒนา ไม่ได้ย้ายเข้าโปรแกรมที่เปิดอยู่หรือVPSเอง ก่อนเปลี่ยนให้หยุดทุกงานและปิดEXE แทนเฉพาะEXEในโฟลเดอร์/บัญชีWindowsเดิม เก็บconfig/data/ประวัติ/baseline/โปรไฟล์และเสียงkitเดิม ไม่ต้องเตรียมเสียงใหม่ ไม่เปิดสองรุ่นซ้อน

ตรวจผลfullsuite/clean-source/frozenGUIoffscreen/SHA256จากreceiptที่สร้างจริง ไม่ใช้compileหรือการกู้ครั้งเดียวแทนผลnativeระยะยาว ระหว่างทดสอบไม่ต้องรอลูกค้าจริง และไม่ให้ผู้ใช้เปลี่ยน/ลบประวัติเพื่อให้เทสดูผ่าน
