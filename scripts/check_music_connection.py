#!/usr/bin/env python3
"""Read-only MCP handshake and actual Yandex account/playlist check."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def payload(result):
    if result.isError:
        raise RuntimeError('MCP tool reported an error; check network and local authorization')
    value = result.structuredContent
    if value is None:
        for block in result.content:
            if getattr(block, 'type', None) == 'text':
                value = json.loads(block.text)
                break
    return value['result'] if isinstance(value, dict) and set(value) == {'result'} else value


async def check(args):
    env = dict(os.environ)
    if args.token_file:
        env.pop('YANDEX_MUSIC_TOKEN', None)
        env['YANDEX_MUSIC_TOKEN_FILE'] = str(args.token_file.expanduser().resolve())
    params = StdioServerParameters(command=sys.executable, args=['-m', 'yandex_music_mcp.server'], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = sorted(t.name for t in (await session.list_tools()).tools)
            account = payload(await session.call_tool('account_info', {}))
            if args.expected_login and account.get('login', '').casefold() != args.expected_login.casefold():
                raise RuntimeError('Authorized Yandex account is not the expected owner')
            playlists = payload(await session.call_tool('list_my_playlists', {}))
            if not isinstance(playlists, list):
                raise RuntimeError('Unexpected playlist response')
            print(json.dumps({'status': 'connected', 'account_login': account.get('login'),
                              'available_server_tools': len(names), 'ordinary_playlist_count': len(playlists),
                              'first_five_titles': [p.get('title') for p in playlists[:5]],
                              'account_changed': False}, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--token-file', type=Path)
    ap.add_argument('--expected-login', help='Optional expected Yandex login; fail if a different account is authorized')
    args = ap.parse_args()
    try:
        asyncio.run(check(args))
    except Exception as exc:
        print(f'Connection check failed ({type(exc).__name__}); no account changes performed.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
