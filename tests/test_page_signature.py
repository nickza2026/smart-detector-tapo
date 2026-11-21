from dataclasses import replace
import json
import io
from pathlib import Path
import tempfile
import unittest

from smart_tapo.config import Config
from smart_tapo.capture import CaptureError
from smart_tapo.page_signature import sanitized_signature,collect_page_signature,_activity,device_preview

XML='''<hierarchy><node text="private-camera"/><node text="Pan &amp; Tilt" resource-id="com.tplink.iot:id/pan_tilt" class="android.widget.TextView" selected="true"/>
<node text="account@example.com" resource-id="com.tplink.iot:id/account"/>
<node text="Bearer SUPER_PRIVATE_VALUE" content-desc="https://secret.example/?token=PRIVATE"/>
<node text="secret" resource-id="not-a-resource-PRIVATE" class="private-value"/></hierarchy>'''


class SignatureTests(unittest.TestCase):
    def test_explicit_preview_reads_only_selected_screenshot_and_creates_no_files(self):
        from PIL import Image
        config=Config();buffer=io.BytesIO()
        Image.new('RGB',config.expected_size,'blue').save(buffer,format='PNG')
        class Fake:
            calls=[]
            def run(self,*args):self.calls.append(args);return buffer.getvalue()
        fake=Fake()
        with tempfile.TemporaryDirectory() as directory:
            result=device_preview(replace(config,data_dir=Path(directory)),adb=fake)
            self.assertEqual(result.size,config.expected_size)
            self.assertEqual(fake.calls,[('exec-out','screencap','-p')])
            self.assertEqual(list(Path(directory).iterdir()),[])
        with self.assertRaises(CaptureError):
            device_preview(replace(config,expected_size=(4,4)),adb=fake)

    def test_activity_prefers_top_resumed_and_does_not_reuse_background_tapo(self):
        class Fake:
            data=b'mResumedActivity: com.tplink.iot/.OldLive\ntopResumedActivity: com.tplink.iot/.Playback'
            def run(self,*args):return self.data
        fake=Fake()
        self.assertEqual(_activity(fake,'com.tplink.iot'),'com.tplink.iot/.Playback')
        fake.data=b'mResumedActivity: com.tplink.iot/.OldLive\ntopResumedActivity: com.android.launcher/.Home'
        self.assertIsNone(_activity(fake,'com.tplink.iot'))

    def test_only_static_control_markers_and_resource_ids_survive(self):
        config=replace(Config(),camera_name='private-camera')
        result=sanitized_signature(XML,config,'live')
        raw=json.dumps(result)
        for private in ('private-camera','account@example.com','SUPER_PRIVATE_VALUE','secret.example','not-a-resource-PRIVATE'):
            self.assertNotIn(private,raw)
        self.assertTrue(result['configured_camera_present'])
        self.assertEqual(result['verified_mode'],'unknown')
        self.assertIn('Pan & Tilt',raw)
        self.assertFalse(result['raw_xml_saved'])

    def test_login_and_oversized_or_invalid_xml_are_rejected(self):
        with self.assertRaises(CaptureError):sanitized_signature('<hierarchy><node password="true" text="secret"/></hierarchy>',Config(),'live')
        with self.assertRaises(CaptureError):sanitized_signature('x'*(1024*1024+1),Config(),'live')
        with self.assertRaises(CaptureError):sanitized_signature('<invalid',Config(),'live')

    def test_explicit_collection_never_starts_navigates_or_captures_image(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            class FakeAdb:
                def run(self,*args):
                    calls.append(args)
                    if args[1]=='dumpsys':return b'mResumedActivity: com.tplink.iot/.view.ipcamera.CameraPlayV2Activity'
                    return b''
                def ui(self):calls.append(('ui',));return XML
            config=replace(Config(),data_dir=Path(directory),camera_name='private-camera')
            path=collect_page_signature(config,'playback',adb=FakeAdb())
            result=json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(result['operator_label'],'playback')
            self.assertEqual(result['verified_mode'],'unknown')
            self.assertEqual(list(Path(directory).rglob('*.png')),[])
            self.assertTrue(all('monkey' not in cmd and 'screencap' not in cmd and 'connect' not in cmd for cmd in calls))
            self.assertFalse(result['fresh_transport'])
            self.assertIsNone(result['xml_request_id'])
            self.assertEqual(result['schema_version'],2)
            self.assertFalse(any('rm' in cmd for cmd in calls))

    def test_page_changed_during_collection_does_not_publish_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            class FakeAdb:
                count=0
                def run(self,*args):
                    if args[1]=='dumpsys':
                        self.count+=1
                        return f'mResumedActivity: com.tplink.iot/.Page{self.count}'.encode()
                    return b''
                def ui(self):return XML
            with self.assertRaises(CaptureError):collect_page_signature(replace(Config(),data_dir=Path(directory)),'live',adb=FakeAdb())
            self.assertEqual(list(Path(directory).rglob('*.json')),[])
