#!/usr/bin/env python3
"""Create compact, credential-free metadata and an offline agent handoff archive."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', type=Path, default=Path('outputs/current-snapshot/library.json'))
    ap.add_argument('--output', type=Path, default=Path('outputs/handoff'))
    ap.add_argument('--analysis-dir', type=Path,
                    help='Explicitly include supported reports from this directory; no reports are discovered automatically')
    ap.add_argument('--history', type=Path,
                    help='Explicitly include one selected historical library JSON')
    ap.add_argument('--covers', type=Path,
                    help='Explicitly include one selected cover-metadata JSON (not image files)')
    a = ap.parse_args()
    if a.analysis_dir is not None and not a.analysis_dir.is_dir():
        ap.error('--analysis-dir must be an existing directory')
    for option in ('history', 'covers'):
        selected = getattr(a, option)
        if selected is not None and not selected.is_file():
            ap.error(f'--{option} must be an existing file')
    lib = json.loads(a.input.read_text())
    if not lib['coverage'].get('all_account_playlists_exported'):
        raise SystemExit('Complete account snapshot required for final handoff')
    compact = {'schema_version': 'yandex-music-compact/1', 'generated_at': lib['generated_at'],
               'source_snapshot_sha256': hashlib.sha256(a.input.read_bytes()).hexdigest(),
               'coverage': lib['coverage'], 'playlists': [], 'tracks_by_key': {}}
    for p in lib['playlists']:
        cp = {k: p.get(k) for k in ('playlist_id', 'uid', 'kind', 'collections', 'title', 'description',
                                    'url', 'visibility', 'revision', 'declared_track_count', 'system_playlist',
                                    'merge_eligible', 'source_track_list_complete')}
        cp['entries'] = [{k: t.get(k) for k in ('key', 'position', 'source_index', 'timestamp')} for t in p['tracks']]
        compact['playlists'].append(cp)
        for t in p['tracks']:
            if t['key'] in compact['tracks_by_key']:
                continue
            ct = {k: t.get(k) for k in ('key', 'track_id', 'album_id', 'album_ids', 'album_id_source', 'url', 'title',
                                       'version', 'duration_ms', 'available', 'metadata_present',
                                       'explicit', 'content_warning', 'type')}
            ct['artists'] = [{k: ar.get(k) for k in ('id', 'name', 'genres')} for ar in t['artists']]
            ct['albums'] = [{k: al.get(k) for k in ('id', 'title', 'year', 'genre', 'type', 'version')} for al in t['albums']]
            compact['tracks_by_key'][t['key']] = ct
    # The compact format must preserve every ordered membership, including duplicates.
    assert len(compact['playlists']) == len(lib['playlists'])
    for source, target in zip(lib['playlists'], compact['playlists']):
        assert [x['key'] for x in source['tracks']] == [x['key'] for x in target['entries']]
    assert sum(len(p['entries']) for p in compact['playlists']) == lib['coverage']['exported_track_entries']
    dump(a.output / 'music-library.json', compact)
    files = [(a.output / 'music-library.json', 'data/music-library.json'),
             (a.input.parent / 'playlists.txt', 'data/playlists.txt'),
             (a.input.parent / 'coverage.json', 'data/coverage.json'),
             (Path('README.md'), 'README.md'), (Path('requirements.lock.txt'), 'requirements.lock.txt')]
    for folder in ('scripts', 'skills', 'docs'):
        files.extend((p, str(p)) for p in sorted(Path(folder).rglob('*'))
                     if p.is_file() and p.suffix in {'.py', '.sh', '.md', '.txt'} and '__pycache__' not in p.parts)
    # Optional account artifacts must be deliberately selected. In particular,
    # never infer them from old outputs beside a newly selected input snapshot.
    if a.analysis_dir is not None:
        for name in ('music-profile.json', 'summary.json', 'report.txt', 'inventory.txt',
                     'merge-plan.json', 'merge-plan.txt', 'grouping-draft.json', 'grouping-compact-notes.txt',
                     'playlist-priority.json', 'playlist-priority.txt', 'merge-result.json'):
            selected = a.analysis_dir / name
            if selected.is_file():
                files.append((selected, 'analysis/' + name))
    if a.history is not None:
        files.append((a.history, 'history/before-merge-music-library.json'))
    if a.covers is not None:
        files.append((a.covers, 'covers/current-covers.json'))
    # Only this explicit artifact allow-list is packaged. No HOME, config, token, venv or raw account status.
    manifest = {'files': [{'path': name, 'bytes': p.stat().st_size,
                           'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p, name in files]}
    dump(a.output / 'manifest.json', manifest)
    archive = a.output / 'yandex-music-agent-kit.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p, name in files:
            z.write(p, name)
        z.write(a.output / 'manifest.json', 'manifest.json')
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for row in manifest['files']:
            assert hashlib.sha256(z.read(row['path'])).hexdigest() == row['sha256']
    print(json.dumps({'archive': str(archive.resolve()), 'bytes': archive.stat().st_size,
                      'playlists': len(compact['playlists']), 'entries': sum(len(p['entries']) for p in compact['playlists']),
                      'unique_track_album_keys': len(compact['tracks_by_key']), 'verified': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
