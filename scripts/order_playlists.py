#!/usr/bin/env python3
"""Set Yandex's saved playlist order with single-send moves and GET verification."""
import argparse
import json
import os
from pathlib import Path

import requests
from account_export import ReadOnlyAPI
from public_export import API, now, write_json


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--priority', type=Path, default=Path('outputs/analysis/playlist-priority.json'))
    ap.add_argument('--plan', type=Path, help='Optional plan to validate destination identities')
    ap.add_argument('--token-file', type=Path, default=Path('~/.yandex-music-mcp/token'))
    ap.add_argument('--output', type=Path, default=Path('outputs/playlist-order'))
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    os.umask(0o077)
    priority = json.loads(args.priority.read_text())
    desired = [str(k) for k in priority['ordered_destination_kinds']]
    if '3' in desired or len(desired) != len(set(desired)):
        raise SystemExit('Invalid target order or system favorites included')
    api = ReadOnlyAPI(args.token_file, 30)
    uid = str(api.fetch('/account/status')['result']['account']['uid'])
    if args.plan:
        plan = json.loads(args.plan.read_text())
        expected = {g['destination']['playlist_id'] for g in plan['groups']}
        if {uid + ':' + k for k in desired} != expected:
            raise SystemExit('Order targets do not match the plan account and destinations')
    elif str(priority.get('account_uid')) != uid:
        raise SystemExit('Priority owner differs from the authorized account; supply --plan for older priority files')

    def read():
        kinds = api.fetch(f'/users/{uid}/playlists/list/kinds', {'add-playlist-with-likes': 'true'})['result']
        if not isinstance(kinds, list):
            raise RuntimeError('Unexpected order response')
        kinds = [str(k) for k in kinds]
        if len(kinds) != len(set(kinds)):
            raise RuntimeError('Duplicate kind in API order')
        return kinds

    before = read()
    if set(before) != set(desired) | {'3'}:
        raise SystemExit('Collection is not the completed merge target; review remaining sources before ordering')
    favorite_index = before.index('3')
    target_order = desired.copy()
    target_order.insert(favorite_index, '3')
    # Do not cross the protected favorite anchor. Current account stores it last.
    if before[:favorite_index] and set(before[:favorite_index]) != set(target_order[:favorite_index]):
        raise SystemExit('Requested movement would cross the system favorite anchor')
    receipt = {'started_at': now(), 'apply': args.apply, 'before': before, 'requested': target_order,
               'priority_source_snapshot_sha256': priority.get('source_snapshot_sha256'), 'moves': []}
    args.output.mkdir(parents=True, exist_ok=True)
    dest = args.output / ('result.json' if args.apply else 'preview.json')
    write_json(dest, receipt)
    if not args.apply:
        print(json.dumps({'status': 'preview', 'before': before, 'requested': target_order}))
        return
    write = requests.Session()
    write.headers.update(api.session().headers)
    write.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))
    current = before
    for to, kind in enumerate(target_order):
        if kind == '3' or current[to] == kind:
            continue
        fresh = read()
        if fresh != current:
            raise RuntimeError('Playlist order changed concurrently; remaining moves stopped')
        old = current.index(kind)
        predicted = current.copy()
        predicted.insert(to, predicted.pop(old))
        move = {'kind': kind, 'from': old, 'to': to, 'status': 'intent'}
        receipt['moves'].append(move)
        write_json(dest, receipt)
        try:
            response = write.post(API + f'/users/{uid}/playlists/{kind}/change-position',
                                  params={'from': old, 'to': to}, timeout=(12, 30), allow_redirects=False)
            move['http_status'] = response.status_code
        except requests.RequestException as exc:
            move['transport_error'] = type(exc).__name__
        current = read()
        if current != predicted:
            move['status'] = 'unresolved'
            receipt['observed'] = current
            write_json(dest, receipt)
            raise RuntimeError('Move outcome differs from requested order; POST not retried')
        move['status'] = 'verified'
        write_json(dest, receipt)
    current = read()
    actual_list = api.fetch(f'/users/{uid}/playlists/list', {'page': 0, 'pageSize': 1500})['result']
    normal_order = [str(p['kind']) for p in actual_list]
    if current != target_order or normal_order != desired:
        raise RuntimeError('Saved collection order and playlist picker list did not agree')
    receipt.update({'completed_at': now(), 'after': current, 'playlist_list_order': normal_order,
                    'verified': True, 'native_mobile_picker_verified': False})
    write_json(dest, receipt)
    print(json.dumps({'status': 'verified', 'moves': len(receipt['moves']), 'order': normal_order}))


if __name__ == '__main__':
    main()
