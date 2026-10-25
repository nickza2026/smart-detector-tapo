from __future__ import annotations

import argparse
import json
import logging

from smart_tapo.config import load_config
from smart_tapo.runtime import ProcessLock, stop_event
from smart_tapo.worker import Worker


def main():
    parser = argparse.ArgumentParser(description='Visual candidate occupancy worker; no receipt/emergency mode')
    parser.add_argument('--config', required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--calibrate', metavar='PNG', help='Save an explicitly confirmed empty-slot reference and exit')
    parser.add_argument('--confirm-empty', action='store_true')
    parser.add_argument('--replace', action='store_true')
    parser.add_argument('--list-events', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        config = load_config(args.config)
        config.data_dir.mkdir(parents=True, exist_ok=True)
        with ProcessLock(config.data_dir / 'worker.lock'):
            if args.calibrate:
                from smart_tapo.calibrate import save_calibration
                preview = save_calibration(config, args.calibrate, args.confirm_empty, args.replace)
                print('Inspect ROI preview:', preview)
                return 0
            worker = Worker(config)
            try:
                if args.list_events:
                    rows = [dict(row) for row in worker.db.execute('SELECT * FROM events ORDER BY started DESC LIMIT 100')]
                    print(json.dumps(rows, ensure_ascii=False, indent=2))
                    return 0
                with stop_event() as stop:
                    while not stop.is_set():
                        worker.scan()
                        if args.once:
                            break
                        stop.wait(config.poll_interval_seconds)
            finally:
                worker.close()
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        logging.error('%s', error)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
