"""Bounded public diagnostic vocabulary; never format arbitrary provider data."""

DIAGNOSTIC_BUILD = '2026-11-21.60'
APP_VERSION = 'v' + DIAGNOSTIC_BUILD.rsplit('.', 1)[1]

OBSERVATION_ERROR_HINTS = {
    'opencv_error': 'ประมวลผลภาพไม่ผ่าน ตรวจภาพอ้างอิงและขนาดกรอบตรวจ',
    'image_io_error': 'อ่านข้อมูลภาพไม่ผ่าน ตรวจภาพและสิทธิ์เข้าถึงไฟล์ภาพ',
    'invalid_image_value': 'ค่าภาพไม่ถูกต้อง ตรวจภาพอ้างอิงและกรอบตรวจให้ตรงภาพ',
    'unexpected_error': 'ตัวตรวจภาพผิดพลาดชนิดอื่น ตรวจภาพด้วยการทดสอบซ้ำก่อนใช้งานจริง',
}


def observation_error_code(error):
    """Classify only by type; never inspect exception text, filenames or args."""
    import cv2
    if isinstance(error, cv2.error):
        return 'opencv_error'
    if isinstance(error, OSError):
        return 'image_io_error'
    if isinstance(error, ValueError):
        return 'invalid_image_value'
    return 'unexpected_error'


ALIGNMENT_HINTS = {
    'reference_has_too_few_static_anchors': 'ภาพอ้างอิงมีจุดคงที่ไม่พอ',
    'current_has_no_static_anchors': 'ภาพล่าสุดไม่มีจุดคงที่ให้จับคู่',
    'not_enough_anchor_matches': 'จุดอ้างอิงตรงกันไม่พอ',
    'not_enough_unoccluded_anchors': 'จุดอ้างอิงนอกกรอบและบริเวณบังไม่พอ',
    'low_anchor_consensus': 'จุดที่จับคู่ได้เห็นพ้องกันไม่พอ',
    'large_reprojection_error': 'ตำแหน่งจุดอ้างอิงหลังจัดแนวยังคลาดมาก',
    'anchors_do_not_surround_slot': 'จุดอ้างอิงไม่ล้อมรอบช่องรับ',
    'camera_motion_out_of_bounds': 'มุมหรือระยะขยับเกินขอบเขตที่ยอมรับ',
    'local_background_does_not_align': 'พื้นหลังใกล้กรอบยังไม่ตรงหลังจัดแนว',
    'ambiguous_background_residual': 'ความต่างในกรอบอาจเกิดจากพื้นหลังที่ยังไม่ตรง',
    'singular_transform': 'คำนวณการแปลงมุมกลับไม่ได้',
    'unstable_initial_transform': 'การแปลงมุมเริ่มต้นไม่เสถียร',
    'invalid_slot_projection': 'รูปทรงกรอบหลังแปลงมุมไม่ถูกต้อง',
    'excessive_scale_or_reflection': 'สเกลเปลี่ยนมากหรือภาพกลับด้าน',
    'slot_outside_current_view': 'กรอบตรวจอยู่นอกภาพล่าสุด',
    'slot_not_fully_observed': 'ภาพล่าสุดเห็นกรอบตรวจไม่ครบ',
    'registration_could_not_be_estimated': 'คำนวณการจัดแนวภาพไม่ได้',
    'unclassified_alignment_failure': 'การจัดแนวไม่ผ่านขั้นตอนตรวจคุณภาพ',
}
