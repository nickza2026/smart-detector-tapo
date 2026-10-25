from __future__ import annotations

import argparse
import logging

from smart_tapo.config import load_config
from smart_tapo.capture import capture


def main():
    from smart_tapo.runtime import ProcessLock, stop_event
    parser = argparse.ArgumentParser(description='Capture verified Tapo live UI from a local emulator')
    parser.add_argument('--config', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        config = load_config(args.config)
        with ProcessLock(config.data_dir / ('capture-' + config.camera_id + '.lock')):
            with stop_event() as stop:
                return capture(config, stop, once=args.once)
    except (OSError, ValueError, RuntimeError) as error:
        logging.error('%s', error)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
