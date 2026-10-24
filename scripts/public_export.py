#!/usr/bin/env python3
"""Export public Yandex Music playlist metadata, without authentication or audio.

The initial list may be supplied from a previously fetched API response. Existing
detail responses are reused on --resume; a manifest records capture timestamps and
hashes. Run without --resume for a fresh snapshot. Only GET requests are made.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API = "https://api.music.yandex.net"
LOCAL = threading.local()
try:
    import requests
except ImportError:
    requests = None


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def payload(data):
    result = json.loads(data)
    if not isinstance(result, dict) or "result" not in result or result.get("error"):
        raise ValueError("API response contains no successful result")
    return result


def fetch(url, timeout):
    """Reuse optional requests sessions; stdlib works too. Curl is the fallback."""
    errors = []
    try:
        if requests is not None:
            if not hasattr(LOCAL, "session"):
                LOCAL.session = requests.Session()
                LOCAL.session.headers["User-Agent"] = "YandexPlaylistMetadataExport/1.0"
            with LOCAL.session.get(url, timeout=(min(12, timeout), timeout)) as response:
                response.raise_for_status()
                data = response.content
        else:
            with urlopen(Request(url, headers={"User-Agent": "YandexPlaylistMetadataExport/1.0"}), timeout=timeout) as response:
                data = response.read()
        payload(data)
        return data
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc))
    try:
        proc = subprocess.run(
            ["curl", "--fail", "--silent", "--show-error", "--location",
             "--retry", "2", "--retry-all-errors", "--retry-delay", "1",
             "--connect-timeout", str(min(12, timeout)), "--max-time", str(timeout),
             "--user-agent", "YandexPlaylistMetadataExport/1.0", url],
            check=False, capture_output=True, timeout=timeout * 3 + 10,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip())
        payload(proc.stdout)
        return proc.stdout
    except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError) as exc:
        errors.append(type(exc).__name__ + ": " + str(exc))
    raise RuntimeError("; ".join(errors))


def normalize_entry(entry, position, playlist_id):
    # Preserve even unavailable entries and entries whose embedded track is absent.
    if not isinstance(entry, dict):
        entry = {"raw_entry": entry}
    track = entry.get("track") or {}
    if not isinstance(track, dict):
        track = {}
    track_id = entry.get("id", track.get("id"))
    album_ids = [str(a["id"]) for a in track.get("albums", []) if isinstance(a, dict) and a.get("id") is not None]
    album_id = entry.get("albumId")
    album_id_source = "playlist_entry" if album_id is not None else None
    if album_id is None and len(album_ids) == 1:
        album_id = album_ids[0]
        album_id_source = "sole_track_album"
    track_id = str(track_id) if track_id is not None else None
    album_id = str(album_id) if album_id is not None else None
    # If there are several albums, retain all IDs without guessing which was selected.
    key = f"{track_id}:{album_id or ''}" if track_id else f"missing:{playlist_id}:{position}"
    album_path = f"/album/{album_id}" if album_id else ""
    url = f"https://music.yandex.ru{album_path}/track/{track_id}" if track_id else None
    normalized = {
        "position": position, "source_index": entry.get("originalIndex"), "key": key, "track_id": track_id,
        "album_id": album_id, "album_ids": album_ids, "album_id_source": album_id_source,
        "url": url, "timestamp": entry.get("timestamp"),
        "metadata_present": bool(track), "title": track.get("title"),
        "metadata_source": entry.get("_metadata_source"),
        "version": track.get("version"), "duration_ms": track.get("durationMs"),
        "artists": track.get("artists", []), "albums": track.get("albums", []),
        "available": track.get("available"),
        "available_for_premium_users": track.get("availableForPremiumUsers"),
        "available_for_options": track.get("availableForOptions"),
        "explicit": track.get("explicit"), "content_warning": track.get("contentWarning"),
        "type": track.get("type"), "track_metadata": track,
        "entry_metadata": {k: v for k, v in entry.items() if k not in ("track", "_metadata_source")},
    }
    return normalized


def export(args):
    started = now()
    out = args.output.resolve()
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    list_url = f"{API}/users/{quote(args.login, safe='')}/playlists/list"
    if args.list_file:
        data = args.list_file.read_bytes()
        list_source = str(args.list_file.resolve())
    else:
        data = fetch(list_url, args.timeout)
        list_source = list_url
    listing = payload(data)["result"]
    if not isinstance(listing, list):
        raise ValueError("Playlist list result must be an array")
    (raw / "playlists-list.json").write_bytes(data)
    previous = {}
    provenance_path = out / "fetch-provenance.json"
    if args.resume and provenance_path.exists():
        previous = json.loads(provenance_path.read_text(encoding="utf-8")).get("playlists", {})
    provenance = {}
    failed = []
    results = {}

    def one(summary):
        uid, kind = str(summary["uid"]), str(summary["kind"])
        pid = uid + ":" + kind
        url = f"{API}/users/{uid}/playlists/{kind}"
        path = raw / f"playlist-{uid}-{kind}.json"
        if args.resume and path.exists():
            response = payload(path.read_bytes())
            record = dict(previous.get(pid, {}))
            if "fetched_at" not in record:
                record["fetched_at"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                record["timestamp_source"] = "raw_file_mtime"
            record.update({"url": url, "raw_file": str(path.relative_to(out)), "reused": True})
        else:
            body = fetch(url, args.timeout)
            response = payload(body)
            path.write_bytes(body)
            record = {"url": url, "raw_file": str(path.relative_to(out)),
                      "fetched_at": now(), "reused": False}
        detail = response["result"]
        if not isinstance(detail, dict):
            raise ValueError("Playlist detail result must be an object")
        if str(detail.get("uid")) != uid or str(detail.get("kind")) != kind:
            raise ValueError("Returned playlist identity does not match request")
        # The default rich response filters unavailable source positions. The short
        # response is the authoritative membership list, including ID and albumId.
        membership_url = url + "?rich-tracks=false"
        membership_path = raw / f"playlist-{uid}-{kind}-membership.json"
        if args.resume and membership_path.exists():
            membership = payload(membership_path.read_bytes())["result"]
        else:
            body = fetch(membership_url, args.timeout)
            membership = payload(body)["result"]
            membership_path.write_bytes(body)
            record["membership_fetched_at"] = now()
        if not isinstance(membership, dict) or str(membership.get("uid")) != uid or str(membership.get("kind")) != kind:
            raise ValueError("Returned membership identity does not match request")
        record.update({"membership_url": membership_url, "membership_raw_file": str(membership_path.relative_to(out))})
        return pid, {"rich": detail, "membership": membership}, record

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(one, s): s for s in listing}
        for future in concurrent.futures.as_completed(futures):
            summary = futures[future]
            pid = f"{summary['uid']}:{summary['kind']}"
            try:
                pid, detail, record = future.result()
                results[pid] = detail
                provenance[pid] = record
                print(f"Fetched {len(results):02d}/{len(listing)} {pid}: {len(detail['membership'].get('tracks') or [])} entries", flush=True)
            except Exception as exc:
                failed.append({"playlist_id": pid, "title": summary.get("title"), "error": str(exc)})
                print(f"Failed {pid}: {exc}", file=sys.stderr, flush=True)
            write_json(provenance_path, {"started_at": started, "list_source": list_source,
                                        "list_url": list_url, "playlists": provenance})

    playlists, unique = [], {}
    mismatches = []
    pager_mismatches = []
    total_pager_positions = 0
    total_missing_source_positions = 0
    supplemental_tracks = {}
    supplemental_provenance = {}
    supplemental_errors = []
    unresolved = unavailable = count = 0
    text_lines = ["Яндекс Музыка — публичные плейлисты", f"Снимок UTC: {started}",
                  f"Пользователь: {args.login}",
                  "Экспорт только списков и метаданных. Аудиофайлы не загружались.",
                  "Порядок треков и недоступные позиции сохранены.", ""]
    for summary in listing:
        pid = f"{summary['uid']}:{summary['kind']}"
        captured = results.get(pid)
        if captured is None:
            continue
        detail, rich = captured["membership"], captured["rich"]
        rich_by_index = {e.get("originalIndex"): e for e in rich.get("tracks", []) if isinstance(e, dict)}
        rich_by_id = {}
        for rich_entry in rich.get("tracks", []):
            if isinstance(rich_entry, dict):
                rich_by_id.setdefault(str(rich_entry.get("id")), []).append(rich_entry)
        entries = []
        for n, short in enumerate(detail.get("tracks") or [], 1):
            merged = dict(short) if isinstance(short, dict) else {"raw_entry": short}
            rich_entry = rich_by_index.get(merged.get("originalIndex"))
            if not rich_entry or str(rich_entry.get("id")) != str(merged.get("id")):
                candidates = rich_by_id.get(str(merged.get("id")), [])
                exact = [e for e in candidates if str(merged.get("albumId")) in {
                    str(a.get("id")) for a in (e.get("track") or {}).get("albums", [])}]
                rich_entry = exact[0] if exact else (candidates[0] if len(candidates) == 1 else None)
            if rich_entry and str(rich_entry.get("id")) == str(merged.get("id")):
                merged["track"] = rich_entry.get("track")
                merged["_metadata_source"] = "playlist_rich_response"
            if not merged.get("track") and merged.get("id") is not None:
                tid = str(merged["id"])
                if tid not in supplemental_tracks:
                    supplemental_tracks[tid] = None
                    info_url = f"{API}/tracks/{quote(tid, safe='')}/full-info"
                    info_path = raw / f"track-{tid}-full-info.json"
                    try:
                        if args.resume and info_path.exists():
                            info = payload(info_path.read_bytes())["result"]
                        else:
                            body = fetch(info_url, args.timeout)
                            info = payload(body)["result"]
                            info_path.write_bytes(body)
                        track = info.get("track") or {}
                        if str(track.get("id")) != tid:
                            raise ValueError("Supplemental track identity does not match request")
                        supplemental_tracks[tid] = track
                        supplemental_provenance[tid] = {"url": info_url, "raw_file": str(info_path.relative_to(out)),
                            "captured_at_file_mtime": datetime.fromtimestamp(info_path.stat().st_mtime, timezone.utc).isoformat()}
                    except Exception as exc:
                        supplemental_errors.append({"track_id": tid, "error": str(exc)})
                if supplemental_tracks[tid]:
                    merged["track"] = supplemental_tracks[tid]
                    merged["_metadata_source"] = supplemental_provenance[tid]
            entries.append(normalize_entry(merged, n, pid))
        declared = detail.get("trackCount")
        pager_total = (detail.get("pager") or {}).get("total")
        source_indices = {e["source_index"] for e in entries if isinstance(e.get("source_index"), int)}
        missing_source_indices = sorted(set(range(pager_total)) - source_indices) if isinstance(pager_total, int) else []
        total_pager_positions += pager_total if isinstance(pager_total, int) else len(entries)
        total_missing_source_positions += len(missing_source_indices)
        if isinstance(pager_total, int) and pager_total != len(entries):
            pager_mismatches.append({"playlist_id": pid, "pager_total": pager_total,
                                     "exported": len(entries), "missing_source_indices": missing_source_indices})
        if declared != len(entries):
            mismatches.append({"playlist_id": pid, "type": "detail_vs_entries", "declared": declared, "exported": len(entries)})
        if summary.get("trackCount") != declared:
            mismatches.append({"playlist_id": pid, "type": "list_vs_detail", "list_declared": summary.get("trackCount"), "detail_declared": declared})
        item = {
            "playlist_id": pid, "uid": str(summary["uid"]), "kind": str(summary["kind"]),
            "title": detail.get("title"), "description": detail.get("description"),
            "visibility": detail.get("visibility"), "revision": detail.get("revision"),
            "url": f"https://music.yandex.ru/users/{quote(args.login, safe='')}/playlists/{summary['kind']}",
            "declared_track_count": declared, "exported_track_count": len(entries),
            "pager_total": pager_total, "missing_source_indices": missing_source_indices,
            "rich_response_track_count": rich.get("trackCount"),
            "rich_response_entries": len(rich.get("tracks") or []),
            "provenance": provenance[pid],
            "playlist_metadata": {k: v for k, v in detail.items() if k != "tracks"},
            "tracks": entries,
        }
        playlists.append(item)
        text_lines += [f"=== {item['title']} ===", item["url"],
                       f"ID {pid}; треков заявлено {declared}, выгружено {len(entries)}", ""]
        if missing_source_indices:
            text_lines += [f"API pager.total={pager_total}; в ответе отсутствуют исходные позиции (индексы с 0): "
                           + ", ".join(map(str, missing_source_indices)),
                           "Это разрывы индексов API; они сами по себе не доказывают наличие текущих пропущенных треков.", ""]
        for entry in entries:
            count += 1
            unresolved += not entry["metadata_present"]
            unavailable += entry.get("available") is False
            title = entry.get("title") or "[метаданные трека отсутствуют]"
            artists = ", ".join(a.get("name", "") for a in entry.get("artists", []))
            version = f" ({entry['version']})" if entry.get("version") else ""
            availability = " [недоступен]" if entry.get("available") is False else ""
            text_lines += [f"{entry['position']}. {artists} — {title}{version}{availability}",
                           f"   ID {entry['key']} | {entry['url'] or 'нет ссылки'}"]
            if entry["key"] not in unique:
                unique[entry["key"]] = {k: v for k, v in entry.items() if k not in ("position", "timestamp")}
                unique[entry["key"]]["occurrences"] = []
            unique[entry["key"]]["occurrences"].append({"playlist_id": pid, "playlist_title": item["title"], "position": entry["position"]})
        text_lines.append("")

    # Summary and pager counters can describe a different set of positions. The
    # short response's explicit trackCount is the current API membership count.
    complete = (len(results) == len(listing)
                and not any(m["type"] == "detail_vs_entries" for m in mismatches))
    coverage = {
        "scope": "public_playlists_only", "all_account_playlists_exported": False,
        "observed_total_owned_in_ui": args.expected_owned,
        "public_list_count": len(listing), "exported_public_playlist_count": len(playlists),
        "remaining_owned_not_in_public_list": args.expected_owned - len(listing) if args.expected_owned is not None else None,
        "public_scope_complete": complete, "declared_entries_in_list": sum(s.get("trackCount", 0) for s in listing),
        "public_scope_complete_definition": "Every public playlist and every member returned by rich-tracks=false is preserved and matches its explicit trackCount; this does not assert that pager slots without records are current tracks.",
        "pager_counts_match_memberships": not pager_mismatches,
        "all_returned_memberships_preserved": len(results) == len(listing) and not any(m["type"] == "detail_vs_entries" for m in mismatches),
        "exported_track_entries": count, "unique_track_album_keys": len(unique),
        "pager_total_positions": total_pager_positions,
        "source_positions_absent_from_response": total_missing_source_positions,
        "pager_count_mismatches": pager_mismatches,
        "unique_track_ids": len({e["track_id"] for e in unique.values() if e["track_id"]}),
        "entries_with_available_false": unavailable, "entries_without_track_metadata": unresolved,
        "rich_metadata_complete": unresolved == 0,
        "supplemental_metadata_fetch_errors": supplemental_errors,
        "count_mismatches": mismatches, "failed_playlists": failed,
        "limitations": ["Private playlists require authenticated access and are not included.",
                        "Liked playlists and liked tracks are not included.",
                        "Membership uses rich-tracks=false; unavailable IDs remain even when rich metadata is absent.",
                        "Playlist-list summary counts may count fewer playable tracks than the full membership list.",
                        "Pager totals and originalIndex gaps are diagnostics, not proof of missing current tracks; short GET and batch-read membership can agree despite gaps.",
                        "This is a playlist snapshot, not listening-history or play-frequency data.",
                        "Individual requests are not an atomic account snapshot; concurrent edits may cause drift."],
    }
    library = {"schema_version": 1, "generated_at": now(), "source": "Yandex Music public API",
               "account": {"login": args.login}, "coverage": coverage, "playlists": playlists,
               "tracks_by_key": unique, "supplemental_track_provenance": supplemental_provenance}
    write_json(out / "library.json", library)
    write_json(out / "coverage.json", coverage)
    (out / "playlists.txt").write_text("\n".join(text_lines) + "\n", encoding="utf-8")
    files = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.name != "manifest.json" and not path.name.endswith(".tmp"):
            contents = path.read_bytes()
            files.append({"path": str(path.relative_to(out)), "bytes": len(contents),
                          "sha256": hashlib.sha256(contents).hexdigest()})
    write_json(out / "manifest.json", {"generated_at": now(), "hash_algorithm": "sha256", "files": files})
    print(json.dumps(coverage, ensure_ascii=False, indent=2), flush=True)
    return 0 if complete else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", required=True)
    parser.add_argument("--list-file", type=Path, help="Previously fetched playlist-list JSON; omitting fetches a fresh list")
    parser.add_argument("--output", type=Path, default=Path("outputs/public-snapshot"))
    parser.add_argument("--expected-owned", type=int, help="Optional total observed in account UI for explicit coverage")
    parser.add_argument("--workers", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--resume", action="store_true", help="Reuse existing successful detail captures")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    return export(args)


if __name__ == "__main__":
    raise SystemExit(main())
