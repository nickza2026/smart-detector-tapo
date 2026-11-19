"""Desktop-only explicit migration to RTSP images; audio configuration is retained."""
from dataclasses import replace
from pathlib import Path

from .config import Rect

# Banknote drop path below the acceptor in the cabinet C500 1080p view. Only a
# starting box: the operator still confirms the empty reference before use.
FULL_HD_ROI = Rect(840, 400, 220, 200)


def image_config(config):
    if getattr(config,'capture_backend','adb')=='rtsp':return config,False
    # Do not overwrite the old MEmu empty scene when the new reference is saved.
    old=Path(config.baseline_path)
    target=old.with_name(old.stem+'-rtsp-v30'+old.suffix)
    index=1
    while target.exists():
        target=old.with_name(old.stem+'-rtsp-v30-'+str(index)+old.suffix)
        index+=1
    width,height=config.expected_size
    return replace(config,capture_backend='rtsp',baseline_path=target,
        clock_window_enabled=False,video=Rect(0,0,width,height)),True


def reference_config(config,image,reset_roi=False):
    width,height=image.size
    if width<32 or height<32 or width>7680 or height>4320:
        raise ValueError('ขนาดภาพ RTSP ไม่อยู่ในช่วงที่รองรับ')
    roi=config.roi
    try:
        if reset_roi or image.size!=config.expected_size:raise ValueError('New source geometry')
        roi.validate((width,height))
    except ValueError:
        roi=(FULL_HD_ROI if (width,height)==(1920,1080) else
             Rect(max(0,width//2-50),max(0,height//2-50),min(100,width),min(100,height)))
    # Clock ROI is retained only for old config compatibility; RTSP does not use it.
    clock=Rect(0,0,min(280,width),min(21,height))
    return replace(config,expected_size=(width,height),video=Rect(0,0,width,height),
                   roi=roi,clock_roi=clock,clock_window_enabled=False,
                   rtsp_source_binding='').validate()
