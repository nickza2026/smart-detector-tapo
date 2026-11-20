"""Operator-requested UI signatures, never screenshots/raw XML/accounts.

This collects evidence for future Live/Playback verification. An operator label
does not authorize capture and is NEVER itself a trusted Live proof.
"""
from dataclasses import replace
import hashlib
import io
import json
import re
import uuid

from .capture import Adb,CaptureError,nodes
from .refueling_store import _safe
from .diagnostics import DIAGNOSTIC_BUILD
from .capture.page_mode import foreground_activity,classify_page

MARKERS=frozenset({'Live','LIVE','Playback','Playback & Download','Pan & Tilt','Call','Talk',
    'Snapshot','Record','Today','Calendar','Video Playback','Timeline',
    'สด','ถ่ายทอดสด','ดูสด','เล่นย้อนหลัง','การเล่นย้อนหลัง','การเล่นและดาวน์โหลด',
    'เล่นย้อนหลังและดาวน์โหลด','การดูย้อนหลังและดาวน์โหลด','แพนและเอียง','พูดคุย','โทร',
    'ภาพถ่าย','บันทึก','วันนี้','ปฏิทิน'})


def sanitized_signature(xml,config,operator_label,activity=None):
    if operator_label not in ('live','playback'):raise ValueError('Invalid page label')
    if not isinstance(xml,(bytes,str)) or len(xml)>1024*1024:raise CaptureError('UI hierarchy too large')
    tree=nodes(xml)
    if len(tree)>4000:raise CaptureError('UI hierarchy too large')
    if any(n.get('password')=='true' for n in tree):raise CaptureError('Do not collect login or password pages')
    output=[];camera=False
    for node in tree:
        texts=[node.get(k,'').strip() for k in ('text','content-desc')]
        camera=camera or config.camera_name in texts
        known=sorted(set(texts)&MARKERS)
        resource=node.get('resource-id','')
        if not re.fullmatch(re.escape(config.tapo_package)+r':id/[A-Za-z0-9_]{1,100}',resource):resource=''
        cls=node.get('class','')
        if not re.fullmatch(r'(?:android|androidx|'+re.escape(config.tapo_package)+r')\.[A-Za-z0-9_.$]{1,180}',cls):cls=''
        bounds=node.get('bounds','')
        if not re.fullmatch(r'\[\d{1,5},\d{1,5}\]\[\d{1,5},\d{1,5}\]',bounds):bounds=''
        if resource or known:
            output.append({'resource_id':resource,'class':cls,'markers':known,'bounds':bounds,
                           'selected':node.get('selected')=='true','clickable':node.get('clickable')=='true'})
    return {'schema_version':1,'operator_label':operator_label,'verified_mode':'unknown',
            'package':config.tapo_package,'configured_camera_present':camera,'activity':activity,
            'nodes':output,'raw_xml_saved':False,'screenshot_saved':False}


def _activity(adb,package):
    data=adb.run('shell','dumpsys','activity','activities')
    try:return foreground_activity(data,package)
    except ValueError:raise CaptureError('Invalid activity diagnostics') from None


def collect_page_signature(config,operator_label,*,adb=None):
    """Caller must stop Capture/Worker first; never launch/navigate an app."""
    if operator_label not in ('live','playback'):raise ValueError('Invalid page label')
    adb=adb or Adb(replace(config,adb_timeout_seconds=min(config.adb_timeout_seconds,5)))
    before=_activity(adb,config.tapo_package)
    xml=adb.ui()  # Adapter owns its unique remote-file lifecycle.
    after=_activity(adb,config.tapo_package)
    if before!=after:raise CaptureError('Page changed during collection; try again')
    signature=sanitized_signature(xml,config,operator_label,after)
    mode=classify_page(xml,config.camera_name,after,config.tapo_package)
    request_id=getattr(adb,'last_ui_request_id',None)
    signature.update(schema_version=2,collector_build=DIAGNOSTIC_BUILD,
                     selected_serial_sha256=hashlib.sha256(config.adb_serial.encode()).hexdigest(),
                     xml_request_id=request_id,
                     fresh_transport=bool(isinstance(adb,Adb) and re.fullmatch('[0-9a-f]{32}',request_id or '')))
    signature.update(observed_page_mode=mode.mode,page_mode_reason=mode.reason,page_mode_profile=mode.profile_id)
    data=json.dumps(signature,ensure_ascii=False,indent=2).encode('utf-8')
    if len(data)>512*1024:raise CaptureError('Sanitized signature too large')
    directory=_safe(config.data_dir/'diagnostics')
    directory.mkdir(parents=True,exist_ok=True)
    output=_safe(directory/f'tapo-{operator_label}-{uuid.uuid4().hex}.json')
    with output.open('xb') as file:file.write(data)
    return output


def device_preview(config,*,adb=None):
    """Explicit user action only: selected ADB PNG -> RAM, no disk/navigation."""
    from PIL import Image
    adb=adb or Adb(replace(config,adb_timeout_seconds=min(config.adb_timeout_seconds,5)))
    data=adb.run('exec-out','screencap','-p')
    if not isinstance(data,bytes) or len(data)>2*1024*1024:raise CaptureError('Invalid preview')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format!='PNG' or image.size!=config.expected_size or image.width*image.height>4_000_000:
                raise CaptureError('Preview dimensions do not match configured device')
            image.load()
            return image.convert('RGB')
    except (OSError,ValueError,Image.DecompressionBombError):
        raise CaptureError('Invalid preview PNG') from None
