import subprocess
import unittest
from dataclasses import replace
from xml.etree import ElementTree as ET

from smart_tapo.config import Config
from smart_tapo.capture import Adb
from smart_tapo.capture.page_mode import classify_page,LIVE_ACTIVITY,PLAYBACK_ACTIVITY,LIVE_REQUIRED,PLAYBACK_REQUIRED


def layout(mode,*,camera='Synthetic camera',extra=()):
    root=ET.Element('hierarchy')
    names=(LIVE_REQUIRED|{'title','live_surface_view'}) if mode=='live' else (PLAYBACK_REQUIRED|{'live_surface_view','tab_text'})
    for name in sorted(set(names)|set(extra)):
        node=ET.SubElement(root,'node',{'resource-id':'com.tplink.iot:id/'+name,'bounds':'[0,0][100,100]'})
        if name=='title':node.set('text',camera)
        if mode=='playback' and name=='tab_text':node.set('text','Playback');node.set('selected','true')
    return ET.tostring(root)


class PageModeTests(unittest.TestCase):
    def test_observed_live_and_playback_are_distinct_without_operator_label(self):
        self.assertEqual(classify_page(layout('live'),'Synthetic camera',LIVE_ACTIVITY).mode,'live')
        self.assertEqual(classify_page(layout('playback'),'Synthetic camera',PLAYBACK_ACTIVITY).mode,'playback')
        self.assertEqual(classify_page(layout('live'),'Wrong camera',LIVE_ACTIVITY).mode,'unknown')

    def test_shared_surface_or_foreground_alone_never_authorizes_live(self):
        shared=b'<hierarchy><node resource-id="com.tplink.iot:id/live_surface_view" bounds="[0,0][100,100]"/></hierarchy>'
        for activity in (LIVE_ACTIVITY,PLAYBACK_ACTIVITY,None,'com.other/.Player'):
            self.assertEqual(classify_page(shared,'Synthetic camera',activity).mode,'unknown')
        self.assertEqual(classify_page(layout('live'),'Synthetic camera',None).mode,'unknown')
        self.assertEqual(classify_page(layout('live'),'Synthetic camera',PLAYBACK_ACTIVITY).mode,'unknown')

    def test_conflicts_hidden_controls_password_and_malformed_are_unknown(self):
        mixed=layout('live',extra={'play_back_ruler'})
        self.assertEqual(classify_page(mixed,'Synthetic camera',LIVE_ACTIVITY).mode,'unknown')
        hidden=layout('live').replace(b'[0,0][100,100]',b'[0,0][0,0]')
        for xml in (hidden,b'<broken',b'x'*(1024*1024+1),layout('live').replace(b'<hierarchy>',b'<hierarchy><node password="true"/>')):
            self.assertEqual(classify_page(xml,'Synthetic camera',LIVE_ACTIVITY).mode,'unknown')

    def test_full_activity_name_is_normalized_but_other_versions_are_not_guessed(self):
        full='com.tplink.iot/com.tplink.iot.view.ipcamerav3.play.VideoPlayV3Activity'
        self.assertEqual(classify_page(layout('live'),'Synthetic camera',full).mode,'live')
        self.assertEqual(classify_page(layout('live'),'Synthetic camera','com.tplink.iot/.UnknownActivity').mode,'unknown')

    def test_real_adapter_gate_is_read_only_and_checks_both_foregrounds(self):
        for activity,xml,expected in ((LIVE_ACTIVITY,layout('live'),'live'),(PLAYBACK_ACTIVITY,layout('playback'),'playback')):
            calls=[]
            def runner(argv,**kwargs):
                calls.append(argv)
                if 'dumpsys' in argv:data=('topResumedActivity: '+activity).encode()
                elif 'cat' in argv:data=xml
                else:data=b''
                return subprocess.CompletedProcess(argv,0,stdout=data,stderr=b'')
            adapter=Adb(replace(Config(),adb_serial='synthetic-device',camera_name='Synthetic camera'),runner=runner)
            self.assertEqual(adapter.read_page_mode().mode,expected)
            self.assertFalse(any(any(word in command for word in ('monkey','input','screencap','connect')) for command in calls))
        counter=[0]
        def changing(argv,**kwargs):
            if 'dumpsys' in argv:
                counter[0]+=1;data=('topResumedActivity: '+(LIVE_ACTIVITY if counter[0]==1 else PLAYBACK_ACTIVITY)).encode()
            elif 'cat' in argv:data=layout('live')
            else:data=b''
            return subprocess.CompletedProcess(argv,0,stdout=data,stderr=b'')
        adapter=Adb(replace(Config(),adb_serial='synthetic-device',camera_name='Synthetic camera'),runner=changing)
        self.assertEqual(adapter.read_page_mode().mode,'unknown')
