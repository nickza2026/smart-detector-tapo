"""Human-readable TEST report; scores are not probabilities or transactions."""
import json
import math

REASONS={
    'accepted_test_candidate':'ผ่านกติกาตรวจวลีในไฟล์ (TEST เท่านั้น)',
    'need_future_lookahead':'ข้อมูลหลังวลียังไม่พอสำหรับตัดสินแบบต่อเนื่อง',
    'onset_guard':'วลีเริ่มใกล้ขอบต้นข้อมูลเกินไป',
    'cross_kind_overlap':'วลีต่างชนิดทับกัน จึงไม่ยอมรับ',
    'duplicate_suppression':'กันผลซ้ำที่ทับวลีเดิม',
    'insufficient_audio':'ข้อมูลเสียงไม่พอ',
    'below_full_pattern_threshold':'คะแนนรูปแบบเต็มต่ำกว่าเกณฑ์',
    'anchor_requirement_failed':'ส่วนย่อยของวลีผ่านไม่ครบอย่างน้อย 2 ใน 3 ส่วน',
    'maximum_passed_matcher_not_stream_decision':'คะแนนสูงสุดผ่าน matcher แต่ไม่ใช่ผลยอมรับจาก stream',
}


def number(value):
    return f'{value:.4f}' if type(value) in (int,float) and math.isfinite(value) else 'ไม่มีข้อมูล'


def format_report(report):
    lines=['ผลทดสอบไฟล์ออฟไลน์ — ไม่ใช่ Live / ไม่ใช่จำนวนลูกค้าหรือรายการเงิน',
        f'ความยาว {number(report.duration)} วินาที | {report.rate} Hz | {report.channels} ช่อง',
        f'ระดับในไฟล์ RMS {number(report.rms)} / peak {number(report.peak)} (ไม่ใช่เกณฑ์รับวลี)',
        f'ข้อมูลอนาคตที่ต้องรอ {number(report.lookahead)} วินาที',
        f'ตัดสินได้ถึง offset {number(report.finalized_until)} วินาที — ไม่เติมเสียงท้ายคลิปเอง',
        'คะแนน NCC คือ correlation ไม่ใช่เปอร์เซ็นต์ความมั่นใจ',
        'ค่าสูงสุดแสดงเพื่อวิเคราะห์ตามขอบเขต window ที่รายงาน ไม่เท่ากับวลีที่ stream ยอมรับ',
        'ไม่ครอบคลุมทุก peak ที่ถูกปฏิเสธ และไม่พิสูจน์ว่า endpoint ของ MEmu ส่งเสียงเข้าโปรแกรม',
        '']
    accepted=getattr(report,'accepted_counts',None)
    if accepted is not None:
        lines.append(f"วลีที่ผ่านกติกา TEST ตลอดไฟล์: พร้อมเติม {accepted.get('paid_ready',0)} / สิ้นสุด {accepted.get('end',0)} — ไม่ใช่จำนวนลูกค้า")
    omitted=getattr(report,'rows_truncated',0)
    if omitted:
        lines.append(f'จำกัดเฉพาะแถวรายละเอียดที่แสดง: ตัดออก {omitted} แถว แต่ยังตรวจไฟล์ต่อจนจบและเก็บสถานะวลีต่อเนื่อง')
    rounds=getattr(report,'test_rounds',None)
    if rounds:
        counts=rounds['counts']
        lines.extend([f"ช่วงวลี TEST ที่พบเริ่มแล้วพบสิ้นสุด: {counts['complete']} | กำกวม: {counts['ambiguous']}",
                      'นี่เป็นช่วงห่างวลีในไฟล์ ไม่ใช่รายการรับเงินหรือยืนยันลูกค้า'])
        for pair in rounds['records']:
            status={'complete':'พบวลีครบ','end_without_start':'พบสิ้นสุดแต่ไม่พบเริ่ม','ambiguous':'กำกวม มีเริ่มซ้ำ/เวลาไม่ชัด'}.get(pair['status'],'ข้อมูลไม่ครบ')
            lines.append(f"  {status}: {number(pair['start'])} → {number(pair['end'])} วินาที | ช่วงห่าง {number(pair['duration'])} วินาที")
        if rounds['pending']:
            lines.append(f"  พบเริ่มที่ {number(rounds['pending']['start'])} วินาที แต่ยังไม่พบสิ้นสุด — ไม่ปิดเองเมื่อจบไฟล์")
        if rounds['records_truncated']:
            lines.append(f"  แสดงรายการช่วงวลีแบบจำกัด: ไม่แสดงเพิ่ม {rounds['omitted_records']} รายการ แต่ตรวจไฟล์ต่อจนจบ")
        lines.append('')
    for row in report.rows:
        label={'paid_ready':'วลีพร้อมเติม','end':'วลีสิ้นสุด'}.get(row.label,'วลีไม่ทราบชนิด')
        source='ผู้สมัครจากการจำลอง stream' if row.source=='accepted_candidates_api' else 'ค่าสูงสุดตามขอบเขต window/ไฟล์ที่รายงาน'
        anchors=' / '.join(number(value) for value in row.anchors) or 'ไม่มีข้อมูล'
        lines.extend([f'{label} | offset {number(row.onset)}–{number(row.end)} วินาที | {source}',
            f'  NCC {number(row.score)} / เกณฑ์ {number(row.threshold)} | ส่วนย่อย {anchors}',
            '  '+REASONS.get(row.reason,'ไม่มีรายละเอียดสาเหตุที่ยืนยันได้'),''])
    lines.extend(['รายละเอียด metadata (ไม่มีเสียงหรือข้อความพูด):',
                  json.dumps(report.metadata(),ensure_ascii=False,indent=2)])
    return '\n'.join(lines)
