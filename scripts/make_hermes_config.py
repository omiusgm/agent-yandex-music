#!/usr/bin/env python3
"""Print an MCP YAML fragment. Never changes an existing Hermes configuration."""
import argparse
from pathlib import Path
import yaml

READ_TOOLS = [
    'account_info', 'search', 'get_liked_tracks', 'get_listening_history',
    'list_my_playlists', 'get_playlist_tracks', 'get_album_tracks', 'get_playlist_by_url',
    'get_artist_tracks', 'get_similar_tracks', 'get_my_wave', 'get_chart', 'export_tracks_to_file',
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument('--token-file', type=Path, required=True)
    ap.add_argument('--mode', choices=('read', 'manage'), default='manage')
    args = ap.parse_args()
    root = args.root.expanduser().resolve()
    tools = READ_TOOLS + (['create_playlist', 'add_tracks_to_playlist'] if args.mode == 'manage' else [])
    result = {'mcp_servers': {'yandex_music': {
        'command': str(root / '.venv' / 'bin' / 'python'),
        'args': ['-m', 'yandex_music_mcp.server'],
        'env': {'YANDEX_MUSIC_TOKEN_FILE': str(args.token_file.expanduser().resolve()),
                'YANDEX_MUSIC_EXPORT_DIR': str(root / 'exports')},
        'enabled': True, 'timeout': 120, 'connect_timeout': 60,
        'supports_parallel_tool_calls': False,
        'tools': {'include': tools, 'resources': False, 'prompts': False},
    }}}
    print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False), end='')


if __name__ == '__main__':
    main()
