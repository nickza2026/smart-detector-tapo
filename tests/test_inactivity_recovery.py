"""Native sanitized modal fixture plus explicitly synthetic adversarial cases."""
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET
from unittest.mock import patch
from contextlib import contextmanager
from smart_tapo.capture.coordination import navigation_scope, navigation_snapshot
from smart_tapo.config import Config
from smart_tapo.capture import Adb, CaptureError
from smart_tapo.capture.page_mode import LIVE_ACTIVITY, PLAYBACK_ACTIVITY, LIVE_REQUIRED, classify_page

DESCRIPTION='คุณได้ชมวิดีโอ Live เป็นเวลา 5 นาทีโดยไม่มีการดำเนินการใด ๆ ดูต่อไปหรือไม่ เพิ่มเติม'
def modal():
    root=ET.Element('hierarchy')
    for name,text,bounds in [('description_tv',DESCRIPTION,'[10,800][700,1000]'),('continue_btn','ต่อไป','[259,1030][460,1126]'),('cancel_tv','ยกเลิก','[10,1030][200,1126]'),('close_iv','','[650,700][700,750]')]:
        ET.SubElement(root,'node',{'resource-id':'com.tplink.iot:id/'+name,'text':text,'bounds':bounds,'enabled':'true','clickable':'true','visible-to-user':'true'})
    return ET.tostring(root)
def live():
    root=ET.Element('hierarchy')
    for name in LIVE_REQUIRED|{'title'}:
        ET.SubElement(root,'node',{'resource-id':'com.tplink.iot:id/'+name,'text':'Camera' if name=='title' else '', 'bounds':'[0,0][4,4]'})
    return ET.tostring(root)

def thai_refresh(camera='Camera'):
    root=ET.fromstring(live())
    next(n for n in root.iter('node') if n.get('resource-id')=='com.tplink.iot:id/title').set('text',camera)
    ET.SubElement(root,'node',{'text':'แตะเพื่อรีเฟรช','bounds':'[100,200][300,240]','enabled':'true'})
    return ET.tostring(root)
class InactivityTests(unittest.TestCase):
    def test_native_sanitized_fixture_is_unknown_and_has_observed_target(self):
        from smart_tapo.capture import inactivity_center
        xml=(Path(__file__).parent/'fixtures'/'tapo-inactivity-native-sanitized.xml').read_bytes()
        self.assertEqual(len(list(ET.fromstring(xml).iter('node'))),19)
        self.assertEqual(classify_page(xml,'Camera',LIVE_ACTIVITY).reason,'live_inactivity_confirmation')
        self.assertEqual(inactivity_center(xml,Config()),(359,1078))
    def test_native_fixture_requires_recent_selected_camera_and_restores_full_live(self):
        xml=(Path(__file__).parent/'fixtures'/'tapo-inactivity-native-sanitized.xml').read_bytes()
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);a.xml=xml
            a.ensure_live();self.assertEqual(a.taps,1)
            a.xml=live();a.read_page_mode();a.xml=xml
            a.ensure_live();self.assertEqual(a.taps,2)
    def adapter(self,d):
        class Fake(Adb):
            xml=live(); taps=0; activity=LIVE_ACTIVITY; finish=True
            def run(self,*args,**kwargs):
                if 'dumpsys' in args:return ('topResumedActivity: '+self.activity).encode()
                if 'tap' in args:
                    self.taps+=1
                    if self.finish:self.xml=live()
                return b'device'
            def ui(self):return self.xml
        return Fake(replace(Config(),data_dir=Path(d),adb_serial='synthetic',camera_name='Camera'))
    def test_visible_selected_title_is_checked_even_on_known_modal(self):
        for title in ('Camera','Wrong'):
            with self.subTest(title=title),tempfile.TemporaryDirectory() as d:
                a=self.adapter(d)
                root=ET.fromstring(modal())
                ET.SubElement(root,'node',{'resource-id':'com.tplink.iot:id/title','text':title,'bounds':'[0,0][4,4]'})
                a.xml=ET.tostring(root)
                with patch('threading.Event.wait',return_value=False):
                    if title=='Camera':self.assertEqual(a.ensure_live().mode,'live')
                    else:
                        with self.assertRaises(CaptureError):a.ensure_live()
                self.assertEqual(a.taps,int(title=='Camera'))

    def test_slow_dump_and_midread_fences_cannot_authorize_continue(self):
        for variant in ('stale','generation','config','epoch'):
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as d:
                a=self.adapter(d);a.xml=modal();now=[100.];original=a.ui
                def ui():
                    value=original()
                    if variant=='stale':now[0]+=11
                    if variant=='generation':
                        with navigation_scope(a.config):pass
                    if variant=='config':a.config=replace(a.config,camera_name='Other')
                    if variant=='epoch':a._epoch='changed'
                    return value
                with patch.object(a,'ui',ui),patch('smart_tapo.capture.time.monotonic',side_effect=lambda:now[0]):
                    with self.assertRaises(CaptureError):a.ensure_live()
                self.assertEqual(a.taps,0)

    def test_modal_is_unknown_not_live(self):
        result=classify_page(modal(),'Camera',LIVE_ACTIVITY)
        self.assertEqual((result.mode,result.reason),('unknown','live_inactivity_confirmation'))
    def test_recent_actual_live_allows_one_tap_and_requires_full_live(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);self.assertEqual(a.read_page_mode().mode,'live');a.xml=modal()
            a.ensure_live(threading.Event());self.assertEqual(a.taps,1)
    def test_initial_known_modal_allowed_but_not_itself_live(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);a.xml=modal()
            self.assertEqual(a.verify_live().mode,'unknown')
            self.assertEqual(a.ensure_live().mode,'live')
            self.assertEqual(a.taps,1)

    def test_thai_refresh_is_not_live_despite_controls_behind_it(self):
        from smart_tapo.capture import refresh_center
        mode=classify_page(thai_refresh(),'Camera',LIVE_ACTIVITY)
        self.assertEqual((mode.mode,mode.reason),('unknown','live_refresh_required'))
        self.assertEqual(refresh_center(thai_refresh(),Config()),(200,220))

    def test_continue_then_thai_refresh_requires_two_verified_steps(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);a.read_page_mode();a.xml=modal();original=a.run
            def run(*args,**kwargs):
                result=original(*args,**kwargs)
                if 'tap' in args:a.xml=thai_refresh() if a.taps==1 else live()
                return result
            a.run=run
            self.assertEqual(a.recover_live().mode,'live')
            self.assertEqual(a.taps,2)

    def test_modal_recovery_does_not_spend_recent_proof_on_redundant_ui_reads(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);now=[100.];original=a.ui;run=a.run;tap_times=[]
            def ui():
                now[0]+=1.3  # Controlled UI cost, not a claimed live latency.
                return original()
            def command(*args,**kwargs):
                if 'tap' in args:tap_times.append(now[0])
                return run(*args,**kwargs)
            class Stop:
                def is_set(self):return False
                def wait(self,delay):now[0]+=delay;return False
            with patch('smart_tapo.capture.time.monotonic',side_effect=lambda:now[0]),patch.object(a,'ui',ui),patch.object(a,'run',command):
                a.read_page_mode();now[0]=105.;a.xml=modal();a.verify_live()
                now[0]+=1.  # Capture recovery scheduling after the negative check.
                a.ensure_live(Stop())
            self.assertEqual(len(tap_times),1)
            self.assertLess(tap_times[0]-100.,10.)

    def test_continue_does_not_refresh_wrong_camera_or_sensitive_page(self):
        for variant in ('wrong_camera','password'):
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as d:
                a=self.adapter(d);a.read_page_mode();a.xml=modal();original=a.run
                def run(*args,**kwargs):
                    result=original(*args,**kwargs)
                    if 'tap' in args:
                        a.xml=thai_refresh('Other' if variant=='wrong_camera' else 'Camera')
                        if variant=='password':a.xml=a.xml.replace(b'</hierarchy>',b'<node password="true"/></hierarchy>')
                    return result
                a.run=run
                with self.assertRaises(CaptureError):a.recover_live()
                self.assertEqual(a.taps,1)
    def test_readonly_verifier_never_taps(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);a.read_page_mode();a.xml=modal();a.verify_live();self.assertEqual(a.taps,0)
    def test_modal_disappeared_during_navigation_wait_does_not_tap_other_control(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);a.read_page_mode();a.xml=modal()
            @contextmanager
            def acquired(*args):
                a.xml=live();yield
            with patch('smart_tapo.capture.navigation_lock',acquired):a.ensure_live()
            self.assertEqual(a.taps,0)
    def test_wrong_camera_replaced_modal_during_navigation_wait_never_taps(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);a.read_page_mode();a.xml=modal()
            @contextmanager
            def acquired(*args):
                a.xml=live().replace(b'Camera',b'Wrong');yield
            with patch('smart_tapo.capture.navigation_lock',acquired):
                with self.assertRaises(CaptureError):a.ensure_live()
            self.assertEqual(a.taps,0)
    def test_expiry_rollback_unknown_stop_and_bad_target_deny(self):
        for variant in ['stop','duplicate','outside','disabled','invisible','nonclickable','missing','playback','password']:
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as d:
                a=self.adapter(d);a.read_page_mode();a.xml=modal();stop=threading.Event()
                if variant=='expired':a._live_authorization=(a._live_authorization[0]-11,*a._live_authorization[1:])
                if variant=='rollback':a._live_authorization=(a._live_authorization[0]+11,*a._live_authorization[1:])
                if variant=='unknown':a.xml=b'<hierarchy/>';a.read_page_mode();a.xml=modal()
                if variant=='stop':stop.set()
                if variant=='duplicate':a.xml=a.xml.replace(b'</hierarchy>',b'<node resource-id="com.tplink.iot:id/continue_btn"/></hierarchy>')
                if variant=='outside':a.xml=a.xml.replace(b'[259,1030][460,1126]',b'[259,1030][9999,1126]')
                if variant=='disabled':a.xml=a.xml.replace(b'enabled="true"',b'enabled="false"')
                if variant=='invisible':a.xml=a.xml.replace(b'visible-to-user="true"',b'visible-to-user="false"')
                if variant=='nonclickable':a.xml=a.xml.replace(b'clickable="true"',b'clickable="false"')
                if variant=='missing':a.xml=a.xml.replace(b'continue_btn',b'other_btn')
                if variant=='playback':a.activity=PLAYBACK_ACTIVITY
                if variant=='password':a.xml=a.xml.replace(b'</hierarchy>',b'<node password="true"/></hierarchy>')
                with self.assertRaises(CaptureError):a.ensure_live(stop)
                self.assertEqual(a.taps,0)
    def test_old_proof_is_not_required_but_selected_title_after_dismissal_is(self):
        for variant in ('adapter','navigation','config','epoch','session'):
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as d:
                a=self.adapter(d);a.read_page_mode();a.xml=modal()
                if variant=='adapter':a=self.adapter(d);a.xml=modal()
                if variant=='navigation':
                    with navigation_scope(a.config):pass
                if variant=='config':a.config=replace(a.config,camera_name='Wrong')
                if variant=='epoch':a._epoch='new'
                if variant=='session':a.begin_session()
                if variant=='config':
                    with self.assertRaises(CaptureError):a.ensure_live()
                else:self.assertEqual(a.ensure_live().mode,'live')
                self.assertEqual(a.taps,1)

    def test_errors_and_stop_after_ui_lock_clear_authorization(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);a.read_page_mode()
            with patch.object(a,'ui',side_effect=CaptureError('dump failed')):
                with self.assertRaises(CaptureError):a.read_page_mode()
            self.assertIsNone(a._live_authorization)
            a.read_page_mode();a.xml=modal();stop=threading.Event()
            @contextmanager
            def acquired(*args):
                stop.set();yield
            with patch('smart_tapo.capture.ui_lock',acquired):
                with self.assertRaises(CaptureError):a.ensure_live(stop)
            self.assertEqual(a.taps,0);self.assertIsNone(a._live_authorization)

    def test_foreground_changes_during_collection_clear_authorization(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);a.read_page_mode();a.xml=modal();original=a.run;count=0
            def run(*args,**kwargs):
                nonlocal count
                if 'dumpsys' in args:
                    count+=1
                    return ('topResumedActivity: '+(LIVE_ACTIVITY if count==1 else PLAYBACK_ACTIVITY)).encode()
                return original(*args,**kwargs)
            with patch.object(a,'run',run):
                self.assertNotEqual(a.read_page_mode().reason,'live_inactivity_confirmation')
            self.assertIsNone(a._live_authorization);self.assertEqual(a.taps,0)

    def test_cancel_after_navigation_lock_acquisition(self):
        stop=threading.Event()
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);a.read_page_mode();a.xml=modal()
            @contextmanager
            def acquired(*args):
                stop.set();yield
            with patch('smart_tapo.capture.navigation_lock',acquired):
                with self.assertRaises(CaptureError):a.ensure_live(stop)
            self.assertEqual(a.taps,0);self.assertIsNone(a._live_authorization)

    def test_cancel_inside_navigation_scope(self):
        stop=threading.Event()
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);a.read_page_mode();a.xml=modal()
            @contextmanager
            def acquired(config):
                with navigation_scope(config) as state:
                    stop.set();yield state
            with patch('smart_tapo.capture.navigation_scope',acquired):
                with self.assertRaises(CaptureError):a.ensure_live(stop)
            self.assertEqual(a.taps,0)

    def test_request_start_not_completion_ages_authorization(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d)
            with patch('smart_tapo.capture.time.monotonic',side_effect=[100.,111.]):
                a.read_page_mode()
                self.assertEqual(a._live_authorization[0],100.)
                self.assertFalse(a._authorized())

    def test_transport_probe_preserves_only_continuous_recognized_modal(self):
        for disconnected in (False,True):
            with self.subTest(disconnected=disconnected),tempfile.TemporaryDirectory() as d:
                a=self.adapter(d);a.read_page_mode();a.xml=modal();a.verify_live()
                original=a.run
                def run(*args,**kwargs):
                    if args==('get-state',):return b'offline' if disconnected else b'device'
                    if 'sys.boot_completed' in args:return b'1'
                    return original(*args,**kwargs)
                with patch.object(a,'run',run):a.connect(threading.Event())
                self.assertEqual(a._live_authorization is None,disconnected)

    def test_failed_post_tap_selected_title_and_ids_do_not_succeed(self):
        for result in (live().replace(b'Camera',b'Wrong'),live().replace(b'talk_tv',b'not_talk_tv'),b'<hierarchy/>'):
            with tempfile.TemporaryDirectory() as d:
                a=self.adapter(d);a.read_page_mode();a.xml=modal();original=a.run
                def run(*args,**kwargs):
                    value=original(*args,**kwargs)
                    if 'tap' in args:a.xml=result
                    return value
                with patch.object(a,'run',run),patch('threading.Event.wait',return_value=False):
                    with self.assertRaises(CaptureError):a.ensure_live()
                self.assertEqual(a.taps,1);self.assertIsNone(a._live_authorization)

    def test_peer_recovery_invalidates_other_cached_authorization(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);b=self.adapter(d);a.read_page_mode();b.read_page_mode()
            a.xml=modal();b.xml=modal()
            with patch('threading.Event.wait',return_value=False):a.ensure_live()
            self.assertFalse(b._authorized())
            # A new locked observation may authorize only the known Continue.
            with patch('threading.Event.wait',return_value=False):b.ensure_live()
            self.assertEqual(a.taps+b.taps,2)

    def test_sensitive_conflicting_or_changed_dialog_is_not_recognized(self):
        for xml,activity in [(modal(),PLAYBACK_ACTIVITY),(modal(),None),
                (modal().replace(b'</hierarchy>',b'<node password="true" visible-to-user="false"/></hierarchy>'),LIVE_ACTIVITY),
                (modal().replace(b'</hierarchy>',b'<node resource-id="com.tplink.iot:id/play_back_ruler" bounds="[0,0][4,4]"/></hierarchy>'),LIVE_ACTIVITY),
                (modal().replace(b'Live',b'Unknown'),LIVE_ACTIVITY),
                (modal().replace(b'</hierarchy>',b'<node resource-id="com.tplink.iot:id/title" text="Wrong" bounds="[0,0][4,4]"/></hierarchy>'),LIVE_ACTIVITY)]:
            self.assertNotEqual(classify_page(xml,'Camera',activity).reason,'live_inactivity_confirmation')

    def test_audio_default_reader_owns_cache_and_passes_stop(self):
        from smart_tapo import continuous_listener as mod
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);reader=mod.AudioModeReader();reader.adb=a
            config=replace(a.config,adb_timeout_seconds=min(5,a.config.adb_timeout_seconds));a.config=config
            stop=threading.Event()
            _,mode=mod._prepare(config,'unused',reader,False,stop=stop)
            self.assertEqual(mode.mode,'live');a.xml=modal()
            with patch('threading.Event.wait',return_value=False):
                _,mode=mod._prepare(config,'unused',reader,False,stop=stop)
            self.assertEqual(mode.mode,'live');self.assertEqual(a.taps,1)
            stop.set();a.xml=modal()
            with self.assertRaises(mod.CheckFailure):reader(config,stop=stop)
            self.assertIsNone(a._live_authorization)

    def test_each_audio_start_has_fresh_reader_but_injected_seam_is_unchanged(self):
        from smart_tapo import continuous_listener as mod
        device=dict(index=0,name='offline',rate=16000,channels=1)
        with tempfile.TemporaryDirectory() as d:
            config=self.adapter(d).config
            listener=mod.ContinuousListener()
            with patch.object(listener,'_submit'):
                listener.start(config,device,'unused',consent=True);first=listener._reader
                listener.stop()
                listener.start(config,device,'unused',consent=True)
                self.assertIsNot(first,listener._reader)
            injected=lambda config:classify_page(live(),'Camera',LIVE_ACTIVITY)
            listener=mod.ContinuousListener(mode_reader=injected)
            with patch.object(listener,'_submit'):
                listener.start(config,device,'unused',consent=True)
                self.assertIs(listener._reader,injected)
                self.assertEqual(mod._prepare(config,'unused',injected,False)[1].mode,'live')

    def test_persistent_modal_retries_one_tap_per_bounded_attempt(self):
        with tempfile.TemporaryDirectory() as d:
            a=self.adapter(d);a.read_page_mode();a.xml=modal();a.finish=False
            with patch('threading.Event.wait',return_value=False):
                with self.assertRaises(CaptureError):a.ensure_live()
                with self.assertRaises(CaptureError):a.ensure_live()
            self.assertEqual(a.taps,2)
