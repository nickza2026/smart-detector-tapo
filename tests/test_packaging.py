from __future__ import annotations

import sys
import unittest

from scripts.smoke_packaged import smoke


class PackagingSmokeTests(unittest.TestCase):
    def test_smoke_flow_in_subprocesses_without_windows_or_live_services(self):
        # Validate smoke-test logic locally. CI separately runs this same flow
        # against the real Windows exe files; this is not packaging proof.
        result = smoke([sys.executable, '-m', 'smart_tapo.capture'],
                       [sys.executable, '-m', 'smart_tapo.worker'])
        self.assertEqual(result, {'stuck_events': 1, 'shadow_notifications': 1, 'real_notifications': 0})


if __name__ == '__main__':
    unittest.main()
