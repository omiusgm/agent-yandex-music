#!/usr/bin/env python3
"""Authenticated Yandex Music metadata snapshot, with explicit loss checks.

Reads ~/.yandex-music-mcp/token locally; never writes credentials to the snapshot.
Only GET and the read-only POST /tracks lookup endpoint are used. No audio or
account changes. Raw playlist responses are retained, including unavailable IDs.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from public_export import API, normalize_entry, now, write_json


class ReadOnlyAPI:
    def __init__(self, token_path, timeout):
        self._token = token_path.expanduser().read_text(encoding="utf-8").strip()
        if not self._token or any(c.isspace() for c in self._token):
            raise ValueError("Token file is empty or malformed")
        self.timeout = timeout
        self.local = threading.local()

    def session(self):
        if not hasattr(self.local, "session"):
            session = requests.Session()
            session.headers.update({"Authorization": "OAuth " + self._token,
                                    "User-Agent": "YandexPlaylistMetadataExport/1.0"})
            retry = Retry(total=2, connect=2, read=2, status=2, backoff_factor=0.5,
                          status_forcelist=(429, 500, 502, 503, 504),
                          allowed_methods=frozenset(("GET",)), respect_retry_after_header=False)
            session.mount("https://", HTTPAdapter(max_retries=retry))
            self.local.session = session
        return self.local.session

    def fetch(self, path, params=None, track_ids=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("API path must be local to the fixed Yandex API host")
        if track_ids is not None and path != "/tracks":
            raise ValueError("Only the read-only track metadata POST is allowed")
        method = "POST" if track_ids is not None else "GET"
        try:
            with self.session().request(
                method, API + path, params=params,
                data={"track-ids": ",".join(track_ids), "with-positions": "true"} if track_ids is not None else None,
                timeout=(min(self.timeout, 12), self.timeout), allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    # Response bodies may include private account information.
                    raise RuntimeError(f"API HTTP {response.status_code}")
                data = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"API transport error: {type(exc).__name__}") from None
        if not isinstance(data, dict) or "result" not in data or data.get("error"):
            raise RuntimeError("API returned no successful result")
        return data


def identity(summary):
    owner = summary.get("owner") or {}
    uid = summary.get("uid", owner.get("uid"))
    kind = summary.get("kind")
    if uid is None or kind is None:
        raise ValueError("Playlist identity is missing")
    return str(uid), str(kind)


def track_id(entry):
    if not isinstance(entry, dict):
        return None
    value = entry.get("id", (entry.get("track") or {}).get("id"))
    return str(value) if value is not None else None


def audit_entries(detail, entries):
    pager = detail.get("pager") or {}
    total = pager.get("total")
    declared = detail.get("trackCount")
    indices = [e.get("originalIndex") if isinstance(e, dict) else None for e in entries]
    indexed = [i for i in indices if isinstance(i, int)]
    all_indexed = len(indexed) == len(entries)
    missing = sorted(set(range(total)) - set(indexed)) if isinstance(total, int) and all_indexed else []
    duplicate = len(indexed) != len(set(indexed))
    out_of_bounds = [i for i in indexed if i < 0 or (isinstance(total, int) and i >= total)]
    complete = (not duplicate and not out_of_bounds
                and isinstance(declared, int) and len(entries) == declared)
    return {"pager_total": total, "raw_entry_count": len(entries),
            "missing_source_indices": missing, "all_entries_have_source_index": all_indexed,
            "duplicate_source_indices": duplicate, "out_of_bounds_source_indices": out_of_bounds,
            "source_track_list_complete": complete,
            "pager_holes_are_not_proof_of_missing_current_entries": True}


def merge_pages(first, later):
    """Join overlapping API pages by source position, never by track ID."""
    combined = copy.deepcopy(first)
    if any(not isinstance(e, dict) or not isinstance(e.get("originalIndex"), int) for e in first + later):
        raise ValueError("Cannot safely combine pages without source indices")
    by_index = {e["originalIndex"]: e for e in combined}
    if len(by_index) != len(combined):
        raise ValueError("Duplicate source indices in playlist response")
    new_count = 0
    for entry in later:
        index = entry["originalIndex"]
        if index in by_index:
            if track_id(entry) != track_id(by_index[index]):
                raise ValueError("Track identity changed at the same source position")
            # Original entries are retained in the raw response files.
            if not by_index[index].get("track") and entry.get("track"):
                by_index[index]["track"] = entry["track"]
        else:
            by_index[index] = copy.deepcopy(entry)
            new_count += 1
    return [by_index[i] for i in sorted(by_index)], new_count


def fetch_playlist(api, out, summary, resume):
    uid, kind = identity(summary)
    pid = uid + ":" + kind
    path = f"/users/{quote(uid, safe='')}/playlists/{quote(kind, safe='')}"
    cache = out / "captures" / f"playlist-{uid}-{kind}.json"
    if resume and cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if (saved["detail"].get("revision") == summary.get("revision")
                and saved.get("audit", {}).get("source_track_list_complete")):
            # Covers and other metadata can change without a revision increment.
            # Reuse enriched track metadata only after checking fresh membership.
            response = api.fetch(path, {"rich-tracks": "false"})
            fresh = response["result"]
            if identity(fresh) != (uid, kind):
                raise ValueError("Playlist response identity mismatch")
            rows = fresh.get("tracks") or []
            pairs = lambda values: [(track_id(e), e.get("albumId")) for e in values]
            audit = audit_entries(fresh, rows)
            if (fresh.get("revision") == summary.get("revision") and audit["source_track_list_complete"]
                    and pairs(rows) == pairs(saved["entries"])):
                raw_path = out / "raw" / f"playlist-{uid}-{kind}-resume-short.json"
                write_json(raw_path, response)
                updated = copy.deepcopy(rows)
                for current, cached in zip(updated, saved["entries"]):
                    if not current.get("track") and cached.get("track"):
                        current["track"] = cached["track"]
                captured_at = now()
                saved.update({"detail": {k: v for k, v in fresh.items() if k != "tracks"},
                              "entries": updated, "audit": audit, "warnings": []})
                saved["provenance"].update({"reused": True, "fetched_at": captured_at})
                saved["provenance"]["requests"].append({"url": API + path, "params": {"rich-tracks": "false"},
                    "raw_file": str(raw_path.relative_to(out)), "fetched_at": captured_at,
                    "revision": fresh.get("revision"), "track_metadata_reused": True})
                write_json(cache, saved)
                return pid, saved
    records, warnings = [], []

    def request(label, params):
        response = api.fetch(path, params)
        detail = response["result"]
        if not isinstance(detail, dict) or identity(detail) != (uid, kind):
            raise ValueError("Playlist response identity mismatch")
        raw_path = out / "raw" / f"playlist-{uid}-{kind}-{label}.json"
        write_json(raw_path, response)
        records.append({"url": API + path, "params": params, "raw_file": str(raw_path.relative_to(out)),
                        "fetched_at": now(), "revision": detail.get("revision")})
        return detail

    rich = request("rich", {"rich-tracks": "true"})
    detail, entries = rich, copy.deepcopy(rich.get("tracks") or [])
    # richTracks responses can filter unavailable tracks. A short response may
    # retain their IDs; enrich only after selecting the complete membership list.
    # Always retrieve current membership and exact album IDs. pager.total can
    # include historical/deleted source positions: current trackCount is checked
    # separately, and all source-index holes remain visible in the audit.
    try:
        short = request("short", {"rich-tracks": "false"})
        short_entries = short.get("tracks") or []
        if short.get("revision") != rich.get("revision"):
            warnings.append("revision_changed_between_rich_and_short")
        detail, entries = short, copy.deepcopy(short_entries)
    except Exception as exc:
        warnings.append("short_response_failed: " + str(exc))
    pager = detail.get("pager") or {}
    total, per_page = pager.get("total"), pager.get("perPage")
    if (not audit_entries(detail, entries)["source_track_list_complete"]
            and isinstance(total, int) and isinstance(per_page, int) and per_page > 0 and total > per_page):
        for page in range(1, (total + per_page - 1) // per_page):
            try:
                extra = request(f"page-{page}", {"rich-tracks": "false", "page": page, "page-size": per_page})
                if extra.get("revision") != detail.get("revision"):
                    warnings.append("revision_changed_during_pagination")
                    break
                entries, added = merge_pages(entries, extra.get("tracks") or [])
                if not added:
                    warnings.append("pagination_did_not_add_source_positions")
                    break
            except Exception as exc:
                warnings.append("pagination_failed: " + str(exc))
                break
    if detail.get("revision") == rich.get("revision"):
        rich_by_id = {track_id(e): e.get("track") for e in rich.get("tracks") or []
                      if isinstance(e, dict) and e.get("track")}
        for entry in entries:
            if isinstance(entry, dict) and not entry.get("track") and track_id(entry) in rich_by_id:
                entry["track"] = rich_by_id[track_id(entry)]
    result = {"detail": {k: v for k, v in detail.items() if k != "tracks"}, "entries": entries,
              "audit": audit_entries(detail, entries), "warnings": warnings,
              "provenance": {"requests": records, "fetched_at": records[-1]["fetched_at"], "reused": False}}
    write_json(cache, result)
    return pid, result


def inventory_items(result, liked=False):
    if not isinstance(result, list):
        raise ValueError("Playlist inventory result must be an array")
    output = []
    for entry in result:
        summary = entry.get("playlist") if liked else entry
        if not isinstance(summary, dict):
            raise ValueError("Inventory entry has no playlist object")
        identity(summary)
        output.append({"summary": summary, "like_metadata": {k: v for k, v in entry.items() if k != "playlist"} if liked else None})
    return output


def enrich_missing(api, out, results, errors):
    missing = sorted({track_id(e) for result in results.values() for e in result["entries"]
                      if isinstance(e, dict) and not e.get("track") and track_id(e)})
    enriched = {}
    for offset in range(0, len(missing), 100):
        batch = missing[offset:offset + 100]
        try:
            response = api.fetch("/tracks", track_ids=batch)
            write_json(out / "raw" / f"tracks-enrichment-{offset // 100:04d}.json", response)
            if not isinstance(response["result"], list):
                raise ValueError("Track metadata lookup result must be an array")
            for track in response["result"]:
                if isinstance(track, dict) and track.get("id") is not None:
                    enriched[str(track["id"])] = track
        except Exception as exc:
            errors.append({"operation": "metadata_enrichment", "batch_offset": offset, "error": str(exc)})
    for result in results.values():
        for entry in result["entries"]:
            if isinstance(entry, dict) and not entry.get("track") and track_id(entry) in enriched:
                entry["track"] = enriched[track_id(entry)]
                entry["metadataEnrichedFrom"] = "read-only POST /tracks"


def export(args):
    os.umask(0o077)
    started = now()
    api = ReadOnlyAPI(args.token_file, args.timeout)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    status = api.fetch("/account/status")["result"]
    account = status.get("account") or {}
    if account.get("uid") is None:
        raise ValueError("Authenticated account UID is absent")
    uid = str(account["uid"])
    # Do not persist status, subscription, contact, or payment fields.
    safe_account = {"uid": uid, "login": account.get("login"), "display_name": account.get("displayName")}
    own_path, liked_path = f"/users/{uid}/playlists/list", f"/users/{uid}/likes/playlists"
    owned_raw = api.fetch(own_path)
    write_json(out / "raw" / "playlists-list.json", owned_raw)
    owned = inventory_items(owned_raw["result"])
    failures = []
    system = []
    kinds_raw = api.fetch(f"/users/{uid}/playlists/list/kinds", {"add-playlist-with-likes": "true"})
    write_json(out / "raw" / "owned-kinds-with-likes.json", kinds_raw)
    if not isinstance(kinds_raw["result"], list):
        raise ValueError("Playlist kinds inventory must be an array")
    ui_owned_kinds = {str(kind) for kind in kinds_raw["result"]}
    ordinary_kinds = {identity(item["summary"])[1] for item in owned}
    for kind in sorted(ui_owned_kinds - ordinary_kinds):
        raw_system = api.fetch(f"/users/{uid}/playlists/{quote(kind, safe='')}", {"rich-tracks": "false"})
        write_json(out / "raw" / f"system-playlist-{kind}-inventory.json", raw_system)
        system.append({"summary": {k: v for k, v in raw_system["result"].items() if k != "tracks"}, "like_metadata": None})
    try:
        liked_raw = api.fetch(liked_path)
        write_json(out / "raw" / "liked-playlists-list.json", liked_raw)
        liked = inventory_items(liked_raw["result"], liked=True)
    except Exception as exc:
        failures.append({"operation": "liked_inventory", "error": str(exc)})
        liked = []
    inventory = {}
    for collection, items in (("owned", owned), ("liked", liked), ("liked_tracks", system)):
        for item in items:
            pid = ":".join(identity(item["summary"]))
            if pid not in inventory:
                inventory[pid] = {"summary": item["summary"], "collections": []}
            inventory[pid]["collections"].append(collection)
            if item["like_metadata"] is not None:
                inventory[pid]["like_metadata"] = item["like_metadata"]
    write_json(out / "inventory.json", {"generated_at": started, "account": safe_account, "playlists": inventory})
    print(f"Inventory: {len(owned)} editable owned, {len(system)} system, {len(liked)} liked; {len(inventory)} distinct playlists", flush=True)
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_playlist, api, out, item["summary"], args.resume): pid
                   for pid, item in inventory.items()}
        for future in concurrent.futures.as_completed(futures):
            pid = futures[future]
            try:
                pid, result = future.result()
                results[pid] = result
                print(f"Fetched {len(results)}/{len(inventory)} {pid}: {len(result['entries'])} entries"
                      f"; complete={result['audit']['source_track_list_complete']}", flush=True)
            except Exception as exc:
                failures.append({"playlist_id": pid, "error": str(exc)})
                print(f"Failed {pid}: {exc}", file=sys.stderr, flush=True)
    verification = {"verified_at": now(), "owned_stable": False, "liked_stable": False}
    for name, path, original, is_liked in (("owned", own_path, owned, False), ("liked", liked_path, liked, True)):
        try:
            final_raw = api.fetch(path)
            write_json(out / "raw" / f"{name}-playlists-list-final.json", final_raw)
            final_items = inventory_items(final_raw["result"], liked=is_liked)
            def fingerprints(items):
                return {":".join(identity(i["summary"])): {
                    "revision": i["summary"].get("revision"), "track_count": i["summary"].get("trackCount")}
                    for i in items}
            before, after = fingerprints(original), fingerprints(final_items)
            changes = [{"playlist_id": pid, "before": before.get(pid), "after": after.get(pid)}
                       for pid in sorted(set(before) | set(after)) if before.get(pid) != after.get(pid)]
            verification[name + "_stable"] = not changes
            verification[name + "_final_count"] = len(final_items)
            verification[name + "_changes"] = changes
        except Exception as exc:
            verification[name + "_error"] = str(exc)
    try:
        final_kinds = api.fetch(f"/users/{uid}/playlists/list/kinds", {"add-playlist-with-likes": "true"})
        write_json(out / "raw" / "owned-kinds-with-likes-final.json", final_kinds)
        verification["ui_owned_kinds_stable"] = {str(kind) for kind in final_kinds["result"]} == ui_owned_kinds
        verification["ui_owned_final_count"] = len(final_kinds["result"])
        verification["system_stable"] = True
        for item in system:
            system_uid, kind = identity(item["summary"])
            final_system = api.fetch(f"/users/{system_uid}/playlists/{kind}", {"rich-tracks": "false"})
            write_json(out / "raw" / f"system-playlist-{kind}-final.json", final_system)
            saved = results.get(system_uid + ":" + kind, {}).get("detail", {})
            if any(saved.get(k) != final_system["result"].get(k) for k in ("revision", "trackCount")):
                verification["system_stable"] = False
    except Exception as exc:
        verification["system_stable"] = False
        verification["system_error"] = str(exc)
    write_json(out / "inventory-verification.json", verification)
    enrich_missing(api, out, results, failures)
    playlists, unique, count_mismatches, incomplete, revision_drift = [], {}, [], [], []
    text_lines = ["Яндекс Музыка — плейлисты и метаданные", f"Снимок UTC: {started}",
                  "Аудиофайлы не загружались. Аккаунт не изменялся.", ""]
    for pid, item in inventory.items():
        if pid not in results:
            continue
        summary, capture = item["summary"], results[pid]
        detail, audit = capture["detail"], capture["audit"]
        entries = [normalize_entry(entry, n, pid) for n, entry in enumerate(capture["entries"], 1)]
        if not audit["source_track_list_complete"]:
            incomplete.append({"playlist_id": pid, **audit})
        if summary.get("revision") is not None and detail.get("revision") != summary.get("revision"):
            revision_drift.append({"playlist_id": pid, "list_revision": summary.get("revision"), "detail_revision": detail.get("revision")})
        if detail.get("trackCount") != len(entries):
            count_mismatches.append({"playlist_id": pid, "declared": detail.get("trackCount"), "exported": len(entries),
                                     "pager_total": audit["pager_total"]})
        owner_uid, kind = identity(summary)
        owner_login = (detail.get("owner") or {}).get("login") or owner_uid
        playlist = {"playlist_id": pid, "uid": owner_uid, "kind": kind, "collections": item["collections"],
                    "system_playlist": "liked_tracks" in item["collections"],
                    "merge_eligible": "owned" in item["collections"] and "liked_tracks" not in item["collections"],
                    "title": detail.get("title"), "description": detail.get("description"), "visibility": detail.get("visibility"),
                    "revision": detail.get("revision"), "url": f"https://music.yandex.ru/users/{quote(str(owner_login), safe='')}/playlists/{kind}",
                    "declared_track_count": detail.get("trackCount"), "exported_track_count": len(entries),
                    **audit, "warnings": capture["warnings"], "provenance": capture["provenance"],
                    "playlist_metadata": detail, "tracks": entries}
        if "like_metadata" in item:
            playlist["like_metadata"] = item["like_metadata"]
        playlists.append(playlist)
        text_lines.extend([f"=== {playlist['title']} ===", playlist["url"],
                           f"ID {pid}; коллекции: {', '.join(item['collections'])}; треков {len(entries)}; revision {playlist['revision']}"])
        if not audit["source_track_list_complete"]:
            text_lines.append("ВНИМАНИЕ: API не подтвердил полноту исходных позиций; объединение без потерь пока не доказано.")
        for entry in entries:
            artists = ", ".join(a.get("name", "") for a in entry.get("artists", []))
            text_lines.append(f"{entry['position']}. {artists} — {entry.get('title') or '[нет метаданных]'} | {entry['key']} | {entry['url'] or ''}"
                              + (" [недоступен]" if entry.get("available") is False else ""))
            if entry["key"] not in unique:
                unique[entry["key"]] = {k: v for k, v in entry.items() if k not in ("position", "timestamp")}
                unique[entry["key"]]["occurrences"] = []
            unique[entry["key"]]["occurrences"].append({"playlist_id": pid, "playlist_title": playlist["title"],
                                                       "position": entry["position"], "collections": item["collections"]})
        text_lines.append("")
    own_ids = {pid for pid, item in inventory.items() if "owned" in item["collections"]}
    liked_ids = {pid for pid, item in inventory.items() if "liked" in item["collections"]}
    system_ids = {pid for pid, item in inventory.items() if "liked_tracks" in item["collections"]}
    complete_ids = {p["playlist_id"] for p in playlists if p["source_track_list_complete"] and not p["warnings"]}
    owned_inventory_matches = args.expected_owned is None or len(ui_owned_kinds) == args.expected_owned
    liked_inventory_matches = args.expected_liked is None or len(liked) == args.expected_liked
    owned_api_complete = verification["owned_stable"] and own_ids <= complete_ids and not any(d["playlist_id"] in own_ids for d in revision_drift)
    liked_api_complete = verification["liked_stable"] and liked_ids <= complete_ids and not any(f.get("operation") == "liked_inventory" for f in failures)
    system_api_complete = (verification.get("system_stable", False) and verification.get("ui_owned_kinds_stable", False)
                           and system_ids <= complete_ids)
    owned_complete = owned_inventory_matches and owned_api_complete and system_api_complete
    liked_complete = liked_inventory_matches and liked_api_complete
    coverage = {
        "scope": "authenticated_owned_system_and_liked_playlists", "all_account_playlists_exported": owned_complete,
        "authenticated_api_scope_complete": owned_api_complete and liked_api_complete and system_api_complete,
        "final_inventory_verification": verification,
        "account_scope_complete": owned_complete and liked_complete, "observed_total_owned_in_ui": args.expected_owned,
        "observed_total_liked_in_ui": args.expected_liked, "owned_list_count": len(owned), "liked_list_count": len(liked),
        "system_playlist_count": len(system), "ui_owned_kinds_count": len(ui_owned_kinds),
        "owned_ui_count_explanation": "The UI includes the system Liked Tracks playlist kind=3; ordinary playlist list excludes it.",
        "owned_inventory_matches_ui": owned_inventory_matches, "liked_inventory_matches_ui": liked_inventory_matches,
        "exported_owned_playlist_count": len(own_ids & set(results)), "exported_liked_playlist_count": len(liked_ids & set(results)),
        "exported_distinct_playlist_count": len(playlists), "exported_track_entries": sum(len(p["tracks"]) for p in playlists),
        "exported_owned_track_entries": sum(len(p["tracks"]) for p in playlists if "owned" in p["collections"]),
        "exported_liked_track_entries": sum(len(p["tracks"]) for p in playlists if "liked_tracks" in p["collections"]),
        "unique_track_album_keys": len(unique), "unique_track_ids": len({e["track_id"] for e in unique.values() if e["track_id"]}),
        "entries_with_available_false": sum(e.get("available") is False for p in playlists for e in p["tracks"]),
        "entries_without_track_metadata": sum(not e["metadata_present"] for p in playlists for e in p["tracks"]),
        "source_positions_absent_from_response": sum(len(p["missing_source_indices"]) for p in playlists),
        "source_index_gaps_are_historical_or_filtered_not_necessarily_current_membership": True,
        "incomplete_playlists": incomplete, "count_mismatches": count_mismatches, "revision_drift": revision_drift,
        "failed_playlists": failures,
        "limitations": ["This is a playlist snapshot, not listening history or play frequency.",
                        "Individual API requests are not an atomic account snapshot; concurrent edits can cause drift.",
                        "Missing track metadata does not remove the original entry or ID.",
                        "Unavailable tracks can be retained by ID but are not guaranteed to be addable to a new playlist."],
    }
    library = {"schema_version": 1, "generated_at": now(), "source": "Yandex Music authenticated API",
               "account": safe_account, "coverage": coverage, "playlists": playlists, "tracks_by_key": unique}
    write_json(out / "library.json", library)
    write_json(out / "coverage.json", coverage)
    (out / "playlists.txt").write_text("\n".join(text_lines) + "\n", encoding="utf-8")
    files = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.name != "manifest.json" and not path.name.endswith(".tmp"):
            contents = path.read_bytes()
            files.append({"path": str(path.relative_to(out)), "bytes": len(contents), "sha256": hashlib.sha256(contents).hexdigest()})
    write_json(out / "manifest.json", {"generated_at": now(), "hash_algorithm": "sha256", "files": files})
    print(json.dumps({k: v for k, v in coverage.items() if not isinstance(v, list)}, ensure_ascii=False, indent=2), flush=True)
    return 0 if coverage["account_scope_complete"] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, default=Path("~/.yandex-music-mcp/token"))
    parser.add_argument("--output", type=Path, default=Path("outputs/current-snapshot"))
    parser.add_argument("--expected-owned", type=int)
    parser.add_argument("--expected-liked", type=int)
    parser.add_argument("--workers", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--resume", action="store_true", help="Reuse complete captures whose revision still matches the fresh inventory")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    try:
        return export(args)
    except FileNotFoundError:
        print("Token file is not available yet; finish device login before authenticated export.", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"Export failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
