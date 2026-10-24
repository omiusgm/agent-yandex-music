#!/usr/bin/env python3
"""Offline, advisory analysis of public_export.py's normalized library snapshot.

No network, token access, account writes, or playlist mutations. Track equality
requires both track_id and album_id; names, remixes and alternate IDs never merge.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re


STOP_WORDS = set("мой моя мои мое моё новый новая плейлист playlist the and для под точно лучший топ top music".split())
TITLE_CLUES = {
    "работа / фон": {"работа", "work", "loop", "лаундж", "lounge", "кафе", "фон", "видеофон"},
    "видео / монтаж": {"видео", "монтаж", "эдит", "клип", "ютуб", "заставка", "видеофон"},
    "мероприятия": {"мероприятие", "мероприятия", "эфире", "эфиры"},
    "саундтреки / драматургия": {"ost", "осты", "эпик", "саспенс", "драмма", "игры", "атмосфера"},
    "танцы / энергия": {"танцы", "дискотека", "пати", "позитив", "ракета"},
    "электроника": {"электро", "электроника", "electro", "electronic"},
    "повтор / подборки периода": {"репит", "repeat"},
    "спокойное / меланхолия": {"чилл", "вечер", "грусть", "раствориться"},
}


def exact_key(track):
    tid, aid = track.get("track_id"), track.get("album_id")
    if tid is None or aid is None or str(tid) == "" or str(aid) == "":
        return None
    return f"{tid}:{aid}"


def tokens(title):
    return {w for w in re.findall(r"[a-zа-яё]+", (title or "").lower()) if w not in STOP_WORDS}


def artist_key(artist):
    if artist.get("id") is not None:
        return "id:" + str(artist["id"])
    # Name-only artists are descriptive clues, never exact identity evidence.
    name = artist.get("name")
    return "name_only:" + str(name).strip().casefold() if name else None


def dump(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def analyze(library, source_sha256, collection="owned"):
    source_playlists = library.get("playlists", [])
    playlists = [p for p in source_playlists if collection == "all" or "owned" in p.get("collections", ["owned"])]
    ids = [p["playlist_id"] for p in playlists]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate playlist_id in normalized snapshot")
    coverage = library.get("coverage", {})
    generated = datetime.now(timezone.utc).isoformat()
    observed = coverage.get("observed_total_owned_in_ui")
    full = coverage.get("all_account_playlists_exported") is True
    mismatches = coverage.get("count_mismatches") or []
    pager_mismatches = coverage.get("pager_count_mismatches") or []
    failures = coverage.get("failed_playlists") or []
    warnings = []
    if not full:
        if coverage.get("scope") == "public_playlists_only":
            warnings.append(f"НЕПОЛНАЯ БИБЛИОТЕКА: в снимке {len(playlists)} из {observed if observed is not None else 'неизвестного числа'} собственных плейлистов. Приватные плейлисты, понравившиеся плейлисты и лайки отсутствуют.")
        else:
            warnings.append(f"ПОЛНОТА АККАУНТА НЕ ПОДТВЕРЖДЕНА: получено {coverage.get('exported_owned_playlist_count', len(playlists))} собственных плейлистов; ориентир интерфейса {observed}. Проверяйте причины в coverage.")
    if mismatches:
        warnings.append(f"Не устранены расхождения счётчиков: {len(mismatches)}. Нельзя считать снимок доказательством сохранности всех треков аккаунта.")
    if failures:
        warnings.append(f"Не выгружены ответы для {len(failures)} плейлистов из доступного списка.")
    if coverage.get("source_positions_absent_from_response"):
        warnings.append(f"В исходном снимке {coverage['source_positions_absent_from_response']} пропусков исторических позиций originalIndex/pager. Они не считаются автоматически потерянными текущими треками: проверка текущего состава идёт по trackCount и списку ID.")
    if coverage.get("entries_without_track_metadata"):
        warnings.append(f"Для {coverage['entries_without_track_metadata']} позиций отсутствуют подробные метаданные; сохранённые ID продолжают участвовать в точном сравнении.")
    if coverage.get("public_scope_complete") is False:
        warnings.append("Выгрузка публичной части не помечена полной; проверьте coverage.json и исходные ответы.")
    warnings += ["Это присутствие музыки в плейлистах, а не история прослушиваний и не частота прослушивания.",
                 "Предложения ниже требуют решения пользователя. Плейлисты и треки не изменялись."]

    states, artists, mapping, within_duplicates = {}, {}, defaultdict(list), []
    total_entries = unknown_entries = unavailable_entries = 0
    for p in playlists:
        pid = p["playlist_id"]
        counts, artist_ids, per_artist = Counter(), set(), defaultdict(set)
        uncertain = []
        for ordinal, t in enumerate(p.get("tracks", []), 1):
            total_entries += 1
            key = exact_key(t)
            position = t.get("position", ordinal)
            unavailable_entries += t.get("available") is False
            source_key = t.get("key") or f"missing:{pid}:{position}"
            if key:
                counts[key] += 1
                mapping[key].append({"playlist_id": pid, "playlist_title": p.get("title"), "position": position,
                                     "source_index": t.get("source_index"), "source_key": source_key, "available": t.get("available")})
            else:
                unknown_entries += 1
                uncertain.append({"position": position, "source_key": source_key,
                                  "track_id": t.get("track_id"), "album_id": t.get("album_id")})
            for a in t.get("artists") or []:
                if not isinstance(a, dict):
                    continue
                ak = artist_key(a)
                if not ak:
                    continue
                state = artists.setdefault(ak, {"artist_id": a.get("id"), "names": set(), "exact_keys": set(),
                                               "playlists": set(), "occurrences": set(), "unresolved_occurrences": set()})
                if a.get("name"):
                    state["names"].add(a["name"])
                state["playlists"].add(pid)
                state["occurrences"].add((pid, position))
                if key:
                    state["exact_keys"].add(key)
                    per_artist[ak].add(key)
                else:
                    state["unresolved_occurrences"].add((pid, position))
                if a.get("id") is not None:
                    artist_ids.add(ak)
        if any(n > 1 for n in counts.values()):
            within_duplicates.append({"playlist_id": pid, "title": p.get("title"),
                                      "duplicates": [{"track_key": k, "count": n,
                                                      "positions": [o["position"] for o in mapping[k] if o["playlist_id"] == pid]}
                                                     for k, n in sorted(counts.items()) if n > 1]})
        title_tokens = tokens(p.get("title"))
        states[pid] = {"playlist_id": pid, "title": p.get("title"), "url": p.get("url"),
                       "collections": p.get("collections", ["owned"]),
                       "revision": p.get("revision"), "entry_count": len(p.get("tracks", [])),
                       "exact_keys": set(counts), "counts": counts, "unknown_identity_entries": uncertain,
                       "artist_ids": artist_ids, "title_tokens": title_tokens,
                       "per_artist": per_artist,
                       "title_clues": {label for label, words in TITLE_CLUES.items() if title_tokens & words},
                       "has_count_discrepancy": any(m.get("playlist_id") == pid for m in mismatches),
                       "has_source_index_gaps": bool(p.get("missing_source_indices")) or any(m.get("playlist_id") == pid for m in pager_mismatches)}

    # Inverse playlist frequency reduces the influence of ubiquitous artists in
    # artist clues. This is a heuristic for inspection, never track equality.
    artist_weight = {ak: math.log((len(playlists) + 1) / (len(a["playlists"]) + 1)) + 1 for ak, a in artists.items()}
    pairwise, duplicates, subsets, candidates = [], [], [], []
    for aid, bid in itertools.combinations(ids, 2):
        a, b = states[aid], states[bid]
        ka, kb = a["exact_keys"], b["exact_keys"]
        intersection, union = ka & kb, ka | kb
        aa, ab = a["artist_ids"], b["artist_ids"]
        shared_artists = aa & ab
        all_artists = aa | ab
        artist_overlap = (sum(artist_weight[k] for k in shared_artists) / sum(artist_weight[k] for k in all_artists)) if all_artists else 0
        shared_words = a["title_tokens"] & b["title_tokens"]
        shared_clues = a["title_clues"] & b["title_clues"]
        jaccard = len(intersection) / len(union) if union else 0
        containment = len(intersection) / min(len(ka), len(kb)) if ka and kb else 0
        identified = not a["unknown_identity_entries"] and not b["unknown_identity_entries"]
        equal = bool(ka) and ka == kb and identified
        sub = bool(ka) and bool(kb) and ka != kb and (ka < kb or kb < ka) and identified
        pair = {"playlist_a": aid, "title_a": a["title"], "playlist_b": bid, "title_b": b["title"],
                "unique_exact_tracks_a": len(ka), "unique_exact_tracks_b": len(kb),
                "shared_exact_tracks": len(intersection), "union_exact_tracks": len(union),
                "jaccard": round(jaccard, 6), "smaller_set_coverage": round(containment, 6),
                "exact_identity_complete_for_pair": identified,
                "source_count_discrepancy": a["has_count_discrepancy"] or b["has_count_discrepancy"],
                "source_index_gaps": a["has_source_index_gaps"] or b["has_source_index_gaps"],
                "shared_track_keys": sorted(intersection),
                "shared_artist_ids": sorted(k[3:] for k in shared_artists),
                "artist_weighted_jaccard": round(artist_overlap, 6),
                "shared_title_words": sorted(shared_words), "shared_title_clues": sorted(shared_clues)}
        pairwise.append(pair)
        if equal:
            duplicates.append({**pair, "same_multiplicities": a["counts"] == b["counts"],
                               "meaning": "Identical observed sets of track_id:album_id; order and repeated entries can differ."})
        if sub:
            subsets.append({**pair, "subset_playlist": aid if ka < kb else bid,
                            "superset_playlist": bid if ka < kb else aid,
                            "meaning": "Subset by observed exact track/album IDs; no permission to delete."})
        reasons, level = [], None
        if equal:
            level = "identical_observed_sets"
            reasons.append("Одинаковый наблюдаемый набор точных ID трека и альбома.")
        elif sub:
            level = "observed_subset"
            reasons.append("Один наблюдаемый набор точных ID полностью входит в другой.")
        elif len(intersection) >= 3 and (jaccard >= 0.25 or containment >= 0.50):
            level = "track_overlap"
            reasons.append(f"Общих точных ID: {len(intersection)}; покрытие меньшего набора {containment:.0%}.")
        elif ((shared_words or shared_clues) and len(shared_artists) >= 2 and artist_overlap >= 0.12) or (len(shared_artists) >= 4 and artist_overlap >= 0.35):
            level = "title_and_artist_clues"
            reasons.append("Названия и/или исполнители дают повод сравнить назначение; это не доказательство взаимозаменяемости.")
        if level:
            if shared_words:
                reasons.append("Общие слова названий: " + ", ".join(sorted(shared_words)) + ".")
            if shared_clues:
                reasons.append("Подсказки из названий: " + ", ".join(sorted(shared_clues)) + ".")
            if shared_artists:
                reasons.append(f"Общих исполнителей с точными ID: {len(shared_artists)}.")
            candidates.append({**pair, "evidence_level": level, "reasons": reasons,
                               "action": "review_only", "suggested_operation": "Discuss keeping one or building a union; preserve source membership and order in the backup.",
                               "can_delete_sources": False})

    priority = {"identical_observed_sets": 0, "observed_subset": 1, "track_overlap": 2, "title_and_artist_clues": 3}
    candidates.sort(key=lambda p: (priority[p["evidence_level"]], -p["smaller_set_coverage"], -p["jaccard"], -p["shared_exact_tracks"], p["playlist_a"], p["playlist_b"]))
    artist_rows = [{"key": ak, "artist_id": a["artist_id"], "names": sorted(a["names"]),
                    "unique_exact_track_album_count": len(a["exact_keys"]), "playlist_presence_count": len(a["playlists"]),
                    "track_entry_presence_count": len(a["occurrences"]),
                    "unresolved_track_identity_entry_count": len(a["unresolved_occurrences"]),
                    "playlist_ids": sorted(a["playlists"])} for ak, a in artists.items()]
    artist_rows.sort(key=lambda a: (-a["unique_exact_track_album_count"], -a["playlist_presence_count"], a["key"]))
    playlist_rows = [{k: p[k] for k in ("playlist_id", "title", "url", "revision", "collections", "entry_count", "unknown_identity_entries", "has_count_discrepancy", "has_source_index_gaps")}
                     | {"unique_exact_track_album_count": len(p["exact_keys"]), "exact_track_keys": sorted(p["exact_keys"]), "title_clues": sorted(p["title_clues"]),
                        "top_artists_by_distinct_track_presence": [{"key": ak, "names": sorted(artists[ak]["names"]), "unique_exact_track_album_count": len(keys)}
                                                                    for ak, keys in sorted(p["per_artist"].items(), key=lambda item: (-len(item[1]), item[0]))[:5]]}
                     for p in states.values()]
    groups = [{"title_clue": clue, "playlist_ids": [p["playlist_id"] for p in playlist_rows if clue in p["title_clues"]],
               "evidence": "Literal words in playlist titles; not inferred musical genre."} for clue in TITLE_CLUES]
    groups = [g for g in groups if len(g["playlist_ids"]) >= 2]
    common = {"schema_version": 1, "generated_at": generated, "source_generated_at": library.get("generated_at"),
              "analysis_collection": collection, "source_playlist_count": len(source_playlists),
              "excluded_playlist_count": len(source_playlists) - len(playlists),
              "source_sha256": source_sha256, "coverage": coverage, "warnings": warnings,
              "identity_rule": "Only equal, nonempty track_id AND album_id are exact matches. Title/version/artist text never deduplicates tracks. Unknown identities are retained separately.",
              "metrics_are": "Playlist presence at snapshot time, NOT listening frequency, play counts, or ranked personal preference.",
              "account_actions_performed": []}
    summary = {**common, "counts": {"playlists": len(playlists), "track_entries": total_entries,
                                   "unique_exact_track_album_pairs": len(mapping), "unknown_identity_entries": unknown_entries,
                                   "unavailable_entries_retained": unavailable_entries, "pairwise_comparisons": len(pairwise),
                                   "identical_observed_set_pairs": len(duplicates), "observed_subset_pairs": len(subsets),
                                   "advisory_candidate_pairs": len(candidates)},
               "playlists": playlist_rows, "pairwise_overlap": pairwise, "duplicate_observed_sets": duplicates,
               "observed_subsets": subsets, "within_playlist_duplicate_entries": within_duplicates,
               "merge_candidates": candidates, "title_based_groups": groups,
               "artists_by_playlist_presence": artist_rows, "exact_track_occurrence_mapping": dict(mapping)}
    profile = {**common, "purpose": "Portable evidence for another agent to discuss music, artists, and concert searches. No account credentials are included.",
               "not_evidence_for": ["What the person actually listened to", "How often they listen", "Current concert availability", "Permission to modify the account"],
               "playlist_inventory": [{k: p[k] for k in ("playlist_id", "title", "url", "revision", "collections", "entry_count", "title_clues")} for p in playlist_rows],
               "artists_by_playlist_presence": artist_rows, "title_based_groups": groups,
               "handoff_rules": ["Refresh the snapshot before proposing destructive changes.",
                                 "Retain original track and album IDs, unavailable entries and playlist memberships.",
                                 "Do not equate presence in a saved playlist with listening frequency.",
                                 "Check concerts against current primary sources and ask for city/date preferences.",
                                 "Do not forward credentials or source data to another agent without the user's authorization."]}
    return summary, profile


def render(summary):
    count = summary["counts"]
    lines = ["Яндекс Музыка — предварительный разбор плейлистов", "", *summary["warnings"], "",
             "Область анализа: " + ("только собственные плейлисты; понравившиеся чужие плейлисты исключены из кандидатов на объединение." if summary["analysis_collection"] == "owned" else "все плейлисты входного снимка."),
             f"Снимок: {summary['source_generated_at']}", f"SHA-256 входного library.json: {summary['source_sha256']}",
             f"Плейлистов: {count['playlists']}; позиций: {count['track_entries']}; точных пар track_id:album_id: {count['unique_exact_track_album_pairs']}.",
             f"Позиций без полного ID: {count['unknown_identity_entries']}; явно недоступных позиций сохранено: {count['unavailable_entries_retained']}.", "",
             "СОВПАДЕНИЯ И КАНДИДАТЫ НА ОБСУЖДЕНИЕ", "Сравнение по точным ID трека и альбома; одинаковые названия, ремиксы и разные ID не склеивались.",
             f"Одинаковых наблюдаемых наборов: {count['identical_observed_set_pairs']}; пар с вложенным набором: {count['observed_subset_pairs']}.",
             "Проценты показывают долю меньшего уникального набора, найденную во втором плейлисте.", ""]
    for i, c in enumerate(summary["merge_candidates"], 1):
        lines += [f"{i}. «{c['title_a']}» [{c['playlist_a']}] + «{c['title_b']}» [{c['playlist_b']}]",
                  f"   Общих точных треков {c['shared_exact_tracks']}; наборы {c['unique_exact_tracks_a']} и {c['unique_exact_tracks_b']}; покрытие меньшего {c['smaller_set_coverage']:.0%}.",
                  "   " + " ".join(c["reasons"])]
        if c["source_count_discrepancy"]:
            lines.append("   Есть расхождение исходных счётчиков; сначала нужна проверка полноты.")
        if c["source_index_gaps"]:
            lines.append("   У исходных ответов есть пропуски originalIndex/pager; историческое содержимое этих позиций не установлено.")
        if not c["exact_identity_complete_for_pair"]:
            lines.append("   Есть позиции без полного track_id:album_id; сравнение частичное.")
        lines.append("")
    if not summary["merge_candidates"]:
        lines += ["Убедительных пар по заданным порогам не найдено; это не означает, что тематических объединений нет.", ""]
    lines += ["ПОДСКАЗКИ ПО НАЗНАЧЕНИЮ ИЗ НАЗВАНИЙ", "Группы ниже не являются определением жанра и не являются готовым планом объединения."]
    by_id = {p["playlist_id"]: p for p in summary["playlists"]}
    for g in summary["title_based_groups"]:
        lines.append(g["title_clue"] + ": " + "; ".join(f"{by_id[pid]['title']} [{pid}]" for pid in g["playlist_ids"]))
    lines += ["", "ИСПОЛНИТЕЛИ ПО ПРИСУТСТВИЮ В ВЫГРУЖЕННЫХ ПЛЕЙЛИСТАХ", "Это не рейтинг прослушиваний. Уникальные треки считаются по точной паре track_id:album_id."]
    for a in summary["artists_by_playlist_presence"][:30]:
        lines.append(f"{', '.join(a['names'])} [{a['artist_id'] or 'ID отсутствует'}]: {a['unique_exact_track_album_count']} уникальных пар, {a['playlist_presence_count']} плейлистов, {a['track_entry_presence_count']} позиций.")
    lines += ["", "Перед сокращением числа плейлистов необходимо сверить актуальность снимка, согласовать группы, проверить объединённый набор по ID и сохранить исходные принадлежность и порядок. Автоматического удаления или объединения нет.",
              "summary.json хранит все попарные сравнения и исходные позиции. music-profile.json предназначен для передачи другому агенту по решению владельца."]
    return "\n".join(lines) + "\n"


def render_inventory(summary):
    lines = ["Плейлисты: название, число позиций и пять исполнителей по присутствию треков", "",
             *summary["warnings"], "", "Числа возле исполнителей — разные точные пары track_id:album_id в данном плейлисте, не прослушивания.", ""]
    for p in summary["playlists"]:
        top = "; ".join(f"{', '.join(a['names'])}: {a['unique_exact_track_album_count']}" for a in p["top_artists_by_distinct_track_presence"])
        lines += [f"{p['title']} | {p['entry_count']} позиций | {p['playlist_id']}", "  " + (top or "Нет метаданных исполнителей."), p.get("url") or "", ""]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("outputs/current-snapshot/library.json"))
    parser.add_argument("--output", type=Path, default=Path("outputs/analysis"))
    parser.add_argument("--collection", choices=("owned", "all"), default="owned")
    args = parser.parse_args()
    source = args.input.read_bytes()
    library = json.loads(source)
    if library.get("schema_version") != 1:
        parser.error("Expected normalized library schema_version=1")
    summary, profile = analyze(library, hashlib.sha256(source).hexdigest(), args.collection)
    args.output.mkdir(parents=True, exist_ok=True)
    dump(args.output / "summary.json", summary)
    dump(args.output / "music-profile.json", profile)
    (args.output / "report.txt").write_text(render(summary), encoding="utf-8")
    (args.output / "inventory.txt").write_text(render_inventory(summary), encoding="utf-8")
    manifest = {"generated_at": summary["generated_at"], "source_path": str(args.input.resolve()),
                "source_sha256": summary["source_sha256"], "files": []}
    for name in ("summary.json", "music-profile.json", "report.txt", "inventory.txt"):
        path = args.output / name
        manifest["files"].append({"path": name, "bytes": path.stat().st_size,
                                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    dump(args.output / "manifest.json", manifest)
    print(json.dumps({"output": str(args.output.resolve()), "counts": summary["counts"],
                      "full_account_snapshot": library.get("coverage", {}).get("all_account_playlists_exported") is True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
