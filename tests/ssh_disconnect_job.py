"""Harmless timed job: persist timestamps for a user-performed SSH disconnect."""
import argparse
import datetime as dt
import json
from pathlib import Path
import time


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--seconds', type=float, default=180)
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error('--seconds must be positive')
    root = Path(args.output).resolve()
    start_at, start = timestamp(), time.monotonic()
    with (root / 'heartbeats.jsonl').open('x', encoding='utf-8') as log:
        while True:
            elapsed = time.monotonic() - start
            record = {'at': timestamp(), 'elapsed_seconds': round(elapsed, 3)}
            line = json.dumps(record)
            log.write(line + '\n')
            log.flush()
            print(line, flush=True)
            if elapsed >= args.seconds:
                break
            time.sleep(min(5, args.seconds - elapsed))
    result = {'started_at': start_at, 'ended_at': timestamp(),
              'elapsed_seconds': round(time.monotonic() - start, 3),
              'marker': 'BGJOB_SSH_TIMED_JOB_OK',
              'ssh_disconnect_verified': False,
              'note': 'Client disconnect/reconnect times must be provided by the user.'}
    (root / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print('BGJOB_SSH_TIMED_JOB_OK', flush=True)


if __name__ == '__main__':
    main()
