# V46: ตัวส่งไม่หยุดถาวรเพราะ storage error ชั่วคราว

## สิ่งที่ยืนยันได้

V45 หยุดและทิ้งสิทธิ์การส่งในหน่วยความจำเมื่อ future โยน exception ทุกชนิด ทดสอบจำลอง SQLITE_BUSY ยืนยันพฤติกรรมนี้ได้ แต่ภาพ VPS ที่ขึ้นข้อความรวมหลัง HTTP 201 ไม่บอก exception จริง จึงยังไม่ยืนยันสาเหตุบน VPS หรือความเกี่ยวข้องกับ path-unsafe ครั้งก่อน

## พฤติกรรมที่แก้

- จำแนก error เฉพาะตอนเปิดคิว เรียกเมธอด store และปิด store ไม่เหมารวม transport error เป็น storage error
- เมื่อเคยกดเริ่มพร้อมยินยอมแล้ว storage_busy รอ 2, 4, 8, 16, 32 สูงสุด 60 วินาที; storage_full/permission/io_error เริ่ม 30 สูงสุด 300 วินาที เก็บ scope/ปลายทาง/stop-event เดิม มี future เดียว ไม่มีการหมุนลูปถี่
- เมื่อกลับมาอ่านคิวได้ รีเซ็ต backoff และบอกว่ายังเปิดส่งอยู่ แม้ยังไม่มีคิวเข้าเกณฑ์
- หยุดเอง ปิดโปรแกรม หรือเปลี่ยน scope แล้ว future เก่าต้องไม่เริ่มส่งใหม่
- unsafe path, corrupt, schema, กล้องไม่ตรง และ error ที่จำแนกไม่ได้ยังหยุด พร้อม code/phase เช่น storage_path_unsafe/queue_open หรือ queue_dispatch ไม่แสดง exception ดิบ คีย์ path หรือ payload
- receiver_auth_rejected และ receiver_firewall_blocked ต้องหยุด แม้การบันทึกผลหรือปิดคิวจะ busy พร้อมกัน ห้าม storage retry กลบการปฏิเสธสิทธิ์
- ไม่ข้าม path guard ไม่ย้ายคิวไปปลายทางใหม่ ไม่ต่อ TTL ไม่สร้าง event ID/payload ใหม่ และไม่เริ่มส่งเองหลังเปิดโปรแกรมใหม่

## การตรวจสอบ

เพิ่ม regression ทั้ง busy/backoff/reset, stop ระหว่าง future, unknown transport, permanent storage, ปิด store ล้มเหลวพร้อม error เดิม, SQLite exclusive lock จริงในฐานทดสอบชั่วคราว และ fake idempotent receiver หลัง ACK แต่ local mark/close busy

กรณี mark ล้มเหลวก่อน commit อาจส่ง HTTP ซ้ำด้วย event ID/payload เดิม โดย fake receiver นับเหตุการณ์เดียว กรณี mark commit แล้วแต่ close ล้มเหลวต้องไม่ส่งเหตุการณ์นั้นซ้ำ นี่ไม่ใช่การทดสอบ receiver จริงหรือฐานข้อมูลบน VPS

V46 นี้เป็น local candidate ยังไม่ลงนามหรือเผยแพร่ Release และไม่แก้ cooldown หนึ่งชั่วโมงของ updater การยืนยันสาเหตุ VPS ยังต้องใช้ code/phase จริงหรือรายงาน metadata ที่ขอไว้ ไม่ควรลบ DB หรือ WAL/SHM เพื่อทดลองแก้
