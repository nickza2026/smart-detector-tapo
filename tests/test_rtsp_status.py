import logging
import queue
import unittest

from smart_tapo.audit_log import valid_detail
from smart_tapo.desktop_controller import DesktopController, _SafeLog


class RtspStatusTests(unittest.TestCase):
    def test_status_bridge_does_not_echo_url_or_arbitrary_fields(self):
        channel=queue.Queue();handler=_SafeLog(channel,'capture')
        message=logging.LogRecord('synthetic',logging.INFO,'',0,'RTSP image status: %s',('SECRET URL',),None)
        detail=dict(reason='healthy',session_id='synthetic',decoded=42,dropped=3)
        message.rtsp_context=detail;handler.emit(message)
        self.assertFalse(channel.empty())
        controller=DesktopController();controller._roles['capture']['messages']=channel
        text='\n'.join(controller.drain_messages())
        self.assertIn('RTSP',text);self.assertIn('42',text);self.assertNotIn('SECRET',text)
        for bad in (dict(detail,url='SECRET'),dict(detail,reason='SECRET'),dict(detail,decoded=True)):
            message.rtsp_context=bad;handler.emit(message)
            self.assertTrue(channel.empty());self.assertFalse(valid_detail('rtsp_status',bad))

