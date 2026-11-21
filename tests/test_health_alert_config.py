from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from smart_tapo.config import Config, load_config


class HealthAlertConfigTests(unittest.TestCase):
    def test_defaults_preserve_global_notifications_off(self):
        config=Config().validate()
        self.assertFalse(config.notify_enabled)
        self.assertTrue(config.health_notify_enabled)
        self.assertEqual((config.health_failure_seconds,config.health_repeat_seconds),(30,900))

    def test_old_config_remains_loadable_without_enabling_global_notifications(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'config.json'
            path.write_text(json.dumps({'notify_enabled':False}),encoding='utf-8')
            config=load_config(path)
            self.assertFalse(config.notify_enabled)
            self.assertEqual(config.health_failure_seconds,30)

    def test_health_bounds_are_finite_and_not_boolean(self):
        for name,lower,upper in [('health_failure_seconds',1,3600),('health_repeat_seconds',60,86400)]:
            for value in (True,False,float('nan'),float('inf'),-1,0,lower-.1,upper+1,'30',None):
                with self.subTest(name=name,value=value),self.assertRaises(ValueError):
                    replace(Config(),**{name:value}).validate()
            for value in (lower,upper):
                replace(Config(),**{name:value}).validate()
        for value in (1,0,'yes',None):
            with self.assertRaises(ValueError):replace(Config(),health_notify_enabled=value).validate()
