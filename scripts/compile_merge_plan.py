#!/usr/bin/env python3
"""Build a reviewable, read-only merge plan with exact source/destination identities."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--library', type=Path, default=Path('outputs/current-snapshot/library.json'))
    ap.add_argument('--groups', type=Path, default=Path('outputs/analysis/grouping-draft.json'))
    ap.add_argument('--output', type=Path, default=Path('outputs/analysis'))
    a = ap.parse_args()
    lib = json.loads(a.library.read_text())
    groups = json.loads(a.groups.read_text())['groups']
    if not lib['coverage'].get('all_account_playlists_exported'):
        raise SystemExit('Complete account snapshot required')
    owned = {str(p['kind']): p for p in lib['playlists'] if 'owned' in p.get('collections', []) and str(p['kind']) != '3'}
    counts = Counter(str(k) for g in groups for k in g['source_kinds'])
    if set(counts) != set(owned) or any(v != 1 for v in counts.values()):
        raise SystemExit('Every editable owned playlist must appear exactly once')
    plan = {'schema_version': 1, 'status': 'proposal_not_applied',
            'source_snapshot_sha256': hashlib.sha256(a.library.read_bytes()).hexdigest(),
            'owned_before': len(owned), 'owned_after': len(groups),
            'displayed_before': len(owned) + 1, 'displayed_after': len(groups) + 1,
            'system_liked_tracks_preserved': True, 'liked_playlists_preserved': True,
            'deduplication_rule': 'Only identical track_id:album_id across sources; keep destination existing duplicates and archive all source occurrences.',
            'groups': []}
    all_before, all_after = set(), set()
    retire_count = additions = unavailable_additions = 0
    lines = [f"План объединения: {len(owned)+1} → {len(groups)+1} плейлистов",
             f"{len(owned)} обычных → {len(groups)}; «Мне нравится» остаётся отдельно.",
             'Это предложение. Изменения в Яндекс Музыке не выполнены.',
             'Понравившиеся чужие плейлисты не входят в объединение.',
             'Все исходные ID, версии и принадлежность к плейлистам сохранены в архиве.',
             'Если в группе есть приватный источник, перед переносом основа тоже должна стать приватной.',
             'Перед удалением каждого источника — повторная проверка всего состава назначения.',
             'Если недоступный трек не удаётся добавить, источник остаётся и сокращение этой группы не выполняется.', '']
    for i, g in enumerate(groups, 1):
        sources = [owned[str(k)] for k in g['source_kinds']]
        # Keeping the source with most unavailable memberships minimizes risky re-additions.
        target = max(sources, key=lambda p: (sum(t.get('available') is False for t in p['tracks']), len(p['tracks'])))
        required_visibility = 'private' if any(p.get('visibility') != 'public' for p in sources) else 'public'
        ordered, by_key, origins = [], {}, {}
        for p in [target] + [p for p in sources if p is not target]:
            for t in p['tracks']:
                k = t['key']
                origins.setdefault(k, []).append({'playlist_id': p['playlist_id'], 'position': t['position']})
                if k not in by_key:
                    ordered.append(k)
                    by_key[k] = t
        existing = {t['key'] for t in target['tracks']}
        new_keys = [k for k in ordered if k not in existing]
        unavailable = [k for k in new_keys if by_key[k].get('available') is False]
        retire = [{'playlist_id': p['playlist_id'], 'kind': p['kind'], 'title': p['title'], 'revision': p['revision']}
                  for p in sources if p is not target]
        row = {'group_id': g.get('group_id', f'g{i:02}'), 'proposed_title': g['title'],
               'rationale': g['rationale'], 'sources': [{'playlist_id': p['playlist_id'], 'kind': p['kind'],
                   'title': p['title'], 'revision': p['revision'], 'entries': len(p['tracks'])} for p in sources],
               'destination': {'playlist_id': target['playlist_id'], 'kind': target['kind'], 'title': target['title'],
                   'revision': target['revision'], 'current_visibility': target.get('visibility'),
                   'required_visibility': required_visibility,
                   'make_private_before_insert': required_visibility == 'private' and target.get('visibility') != 'private'},
               'required_unique_keys': ordered, 'add_keys': new_keys,
               'unavailable_add_keys': unavailable, 'retire_only_after_verification_and_approval': retire,
               'origins': origins, 'expected_unique_tracks': len(ordered),
               'expected_entries_preserving_destination_duplicates': len(target['tracks']) + len(new_keys),
               'metadata_album_inferences': [k for k in ordered if by_key[k].get('album_id_source') != 'playlist_entry']}
        plan['groups'].append(row)
        all_before.update(t['key'] for p in sources for t in p['tracks'])
        all_after.update(ordered)
        retire_count += len(retire)
        additions += len(new_keys)
        unavailable_additions += len(unavailable)
        lines += [f"{i:02}. {g['title']} — {len(ordered)} уникальных треков",
                  '    Источники: ' + ' + '.join(f"«{p['title']}» ({len(p['tracks'])})" for p in sources),
                  f"    Основа: «{target['title']}»; добавить {len(new_keys)}, из них недоступных сейчас {len(unavailable)}.",
                  '    Обоснование: ' + g['rationale']]
        if retire:
            lines.append('    Удалить только после проверки и подтверждения: ' + '; '.join('«'+p['title']+'»' for p in retire))
        else:
            lines.append('    Оставить отдельным плейлистом.')
        lines.append('')
    assert all_before == all_after
    plan['checks'] = {'every_owned_playlist_mapped_once': True, 'no_unique_track_loss_in_proposal': True,
                      'unique_owned_track_album_keys': len(all_before), 'source_playlists_to_retire': retire_count,
                      'total_insertions_proposed': additions, 'unavailable_insertions_requiring_live_check': unavailable_additions}
    lines[1:1] = [f"Уникальных исходных пар трек/альбом: {len(all_before)}; в плане сохранены все.",
                  f"Потенциальное удаление исходных списков: {retire_count}; добавлений треков: {additions}."]
    a.output.mkdir(parents=True, exist_ok=True)
    (a.output / 'merge-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2)+'\n')
    (a.output / 'merge-plan.txt').write_text('\n'.join(lines)+'\n')
    print(json.dumps({k:v for k,v in plan.items() if k != 'groups'},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
