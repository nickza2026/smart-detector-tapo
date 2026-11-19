"""Observed Tapo v3 page layouts, not proof of video/audio origin or playback rate."""
from dataclasses import dataclass
import re
import xml.etree.ElementTree as ET

PACKAGE='com.tplink.iot'
PROFILE='tapo-v3-observed-2026-10-29'
LIVE_ACTIVITY=PACKAGE+'/.view.ipcamerav3.play.VideoPlayV3Activity'
PLAYBACK_ACTIVITY=PACKAGE+'/.view.cloudvideo.TapoPlaybackAndDownloadActivity'
LIVE_REQUIRED=frozenset({'live_surface_list_view_container','live_surface_list_view',
    'control_tv','talk_tv','tool_sound','playback_and_download'})
PLAYBACK_REQUIRED=frozenset({'playback_memory_tab','play_back_ruler','play_back_center_time'})
PLAYBACK_VETO=frozenset({'play_back_ruler','playback_memory_tab','time_axis_scrollview','play_back_top_bar'})


@dataclass(frozen=True)
class PageMode:
    mode: str
    reason: str
    profile_id: str|None=None
    navigation_generation: str|None=None


def foreground_activity(data,package):
    if not isinstance(data,bytes) or len(data)>2*1024*1024:raise ValueError('Invalid activity diagnostics')
    top=[];legacy=[]
    for line in data.decode('utf-8',errors='replace').splitlines():
        if re.search(r'\bm?topResumedActivity\s*[:=]',line,re.I):target=top
        elif re.search(r'\bmResumedActivity\s*[:=]',line,re.I):target=legacy
        else:continue
        match=re.search(r'(?<![A-Za-z0-9_.])([A-Za-z_][A-Za-z0-9_.]{0,180}/[A-Za-z0-9_.$]{1,200})',line)
        target.append(match.group(1) if match else None)
    candidates=set(top if top else legacy)
    if len(candidates)!=1:return None
    value=candidates.pop()
    return value if value and value.split('/')[0]==package else None


def _component(value):
    if not isinstance(value,str) or '/' not in value:return None
    package,cls=value.split('/',1)
    return package+'/'+(package+cls if cls.startswith('.') else cls)


INACTIVITY_DESCRIPTION='คุณได้ชมวิดีโอ Live เป็นเวลา 5 นาทีโดยไม่มีการดำเนินการใด ๆ ดูต่อไปหรือไม่ เพิ่มเติม'
REFRESH_LABELS=frozenset(('tap to refresh','แตะเพื่อรีเฟรช'))


def inactivity_nodes(tree):
    """Exact observed dialog identity; this is not selected-camera proof."""
    # A second Continue label is ambiguous even if it uses another resource ID.
    if len([n for n in tree if any(n.get(k,'').strip()=='ต่อไป'
            for k in ('text','content-desc'))])!=1:return None
    result={}
    for name,text in (('description_tv',INACTIVITY_DESCRIPTION),('continue_btn','ต่อไป'),
                      ('cancel_tv','ยกเลิก'),('close_iv',None)):
        matches=[n for n in tree if n.get('resource-id')==PACKAGE+':id/'+name]
        if len(matches)!=1:return None
        node=matches[0]
        if node.get('visible-to-user')=='false' or (text is not None and node.get('text','').strip()!=text):return None
        result[name]=node
    return result


def classify_page(xml,camera_name,activity,package=PACKAGE):
    """Fail closed on unknown/contradictory layouts. Never accepts a user mode label."""
    if package!=PACKAGE:return PageMode('unknown','unsupported_package')
    if not isinstance(xml,(str,bytes)) or not xml or len(xml)>1024*1024:
        return PageMode('unknown','invalid_hierarchy')
    try:root=ET.fromstring(xml)
    except (ET.ParseError,ValueError):return PageMode('unknown','invalid_hierarchy')
    tree=[node for node in root.iter('node') if node.get('visible-to-user') != 'false']
    if root.tag!='hierarchy' or len(tree)>4000:return PageMode('unknown','invalid_hierarchy')
    if any(node.get('password')=='true' for node in root.iter('node')):return PageMode('unknown','sensitive_page')
    identifiers=set();titles=[];playback_selected=False
    for node in tree:
        resource=node.get('resource-id','')
        prefix=package+':id/'
        if resource.startswith(prefix):
            name=resource[len(prefix):]
            if not re.fullmatch('[A-Za-z0-9_]{1,100}',name):continue
            bounds=re.fullmatch(r'\[(\d{1,5}),(\d{1,5})\]\[(\d{1,5}),(\d{1,5})\]',node.get('bounds',''))
            if bounds:
                left,top,right,bottom=map(int,bounds.groups())
                if right>left and bottom>top:identifiers.add(name)
            if name=='title':titles.extend(node.get(k,'').strip() for k in ('text','content-desc'))
        if node.get('selected')=='true' and any(node.get(key,'').strip() in ('Playback','เล่นย้อนหลัง','การเล่นย้อนหลัง') for key in ('text','content-desc')):
            playback_selected=True
    component=_component(activity)
    if component==_component(PLAYBACK_ACTIVITY):
        if PLAYBACK_REQUIRED<=identifiers and playback_selected:
            return PageMode('playback','observed_playback_layout',PROFILE)
        return PageMode('unknown','incomplete_playback_layout')
    if identifiers & PLAYBACK_VETO or playback_selected:
        return PageMode('unknown','playback_or_conflicting_controls')
    if component!=_component(LIVE_ACTIVITY):return PageMode('unknown','unrecognized_foreground')
    if inactivity_nodes(list(root.iter('node'))) is not None:
        title_nodes=[node for node in tree if node.get('resource-id')==package+':id/title']
        if title_nodes and (len(title_nodes)!=1 or 'title' not in identifiers
                or not camera_name or camera_name not in titles
                or any(title and title!=camera_name for title in titles)):
            return PageMode('unknown','camera_not_confirmed')
        if not isinstance(camera_name,str) or not camera_name.strip():
            return PageMode('unknown','camera_not_confirmed')
        if {'description_tv','continue_btn','cancel_tv','close_iv'}<=identifiers:
            return PageMode('unknown','live_inactivity_confirmation',PROFILE)
        return PageMode('unknown','incomplete_live_layout')
    if 'title' not in identifiers or not isinstance(camera_name,str) or not camera_name.strip() or camera_name not in titles:
        return PageMode('unknown','camera_not_confirmed')
    title_nodes = [node for node in tree if node.get('resource-id') == package+':id/title']
    if len(title_nodes) != 1 or any(title and title != camera_name for title in titles):
        return PageMode('unknown','ambiguous_camera_title')
    if any(node.get(key, '').strip().casefold() in REFRESH_LABELS
           for node in tree for key in ('text', 'content-desc')):
        return PageMode('unknown','live_refresh_required',PROFILE)
    if not LIVE_REQUIRED<=identifiers:return PageMode('unknown','incomplete_live_layout')
    return PageMode('live','observed_live_layout',PROFILE)
