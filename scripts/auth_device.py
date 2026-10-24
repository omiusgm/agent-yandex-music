#!/usr/bin/env python3
"""Yandex device login. Never opens a browser or prints an access token."""
import json
import argparse
import os
import time
from pathlib import Path

from yandex_music import Client
from yandex_music.exceptions import NetworkError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file', type=Path, default=Path.home() / '.yandex-music-mcp' / 'token')
    parser.add_argument('--device-name', default='Personal Music Connector')
    args = parser.parse_args()
    os.umask(0o077)
    target = args.token_file.expanduser().resolve()
    if target.exists():
        raise SystemExit('Existing token preserved; validate it before replacing.')
    client = Client()
    code = None
    for attempt in range(6):
        try:
            code = client.request_device_code(device_name=args.device_name)
            break
        except NetworkError:
            time.sleep(min(attempt + 1, 5))
    if code is None:
        raise SystemExit('Could not contact Yandex OAuth; no credentials created.')
    print(json.dumps({'verification_url': code.verification_url, 'user_code': code.user_code,
                      'expires_in': code.expires_in}, ensure_ascii=False), flush=True)
    deadline = time.monotonic() + code.expires_in
    while time.monotonic() < deadline:
        try:
            result = client.poll_device_token(code.device_code)
        except NetworkError:
            result = None
        if result is not None:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as out:
                out.write(result.access_token)
            print('AUTHORIZED: token saved locally with mode 0600; token not printed.', flush=True)
            return
        time.sleep(max(code.interval, 5))
    raise SystemExit('Device code expired; no credentials saved.')


if __name__ == '__main__':
    main()
