from __future__ import annotations

import argparse
from dataclasses import replace
import logging
import time

from smart_tapo.config import load_config
from smart_tapo.capture import capture
from smart_tapo.desktop_controller import audit_session


def main():
    from smart_tapo.runtime import ProcessLock, stop_event
    parser = argparse.ArgumentParser(description='Capture sampled images from a credential-free RTSP relay')
    parser.add_argument('--config', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        config = replace(load_config(args.config), capture_backend='rtsp').validate()
        with ProcessLock(config.data_dir / ('capture-' + config.camera_id + '.lock')), \
             audit_session(config.data_dir, 'capture', scope=config.camera_id) as log:
            log._send('ready', None, time.time())
            with stop_event() as stop:
                result = capture(config, stop, once=args.once)
            log._send('failed' if result else 'stopped', None, time.time())
            return result
    except (OSError, ValueError, RuntimeError) as error:
        logging.error('%s', error)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
