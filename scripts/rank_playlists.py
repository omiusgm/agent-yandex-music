#!/usr/bin/env python3
"""Rank ordinary playlists offline using retained entries' original add timestamps.

Use a pre-merge snapshot plus --plan to aggregate historical activity into future
destinations. The script never substitutes file modification, playlist revision,
or current execution times for track-add timestamps. Removed entries are absent
from a snapshot, so this is not a complete event history or listening statistics.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Timestamp must be a nonempty ISO8601 string")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed


def track_key(track):
    track_id, album_id = track.get("track_id"), track.get("album_id")
    if track_id is None or album_id is None or str(track_id) == "" or str(album_id) == "":
        return None
    return f"{track_id}:{album_id}"


def window_stats(events):
    daily = defaultdict(set)
    for event in events:
        # Unknown identities can show an active day but are not unique-ID evidence.
        daily[event["date"]]
        if event["key"] is not None:
            daily[event["date"]].add(event["key"])
    return {
        "retained_entry_additions": len(events),
        "active_addition_days": len(daily),
        "unique_track_album_additions": len({e["key"] for e in events if e["key"] is not None}),
        "distinct_track_day_additions": len({(e["key"], e["date"]) for e in events if e["key"] is not None}),
        "unresolved_identity_entries": sum(e["key"] is None for e in events),
        "max_unique_tracks_in_one_day": max((len(keys) for keys in daily.values()), default=0),
        "possible_bulk_days": [
            {"date": day.isoformat(), "unique_track_album_additions": len(keys)}
            for day, keys in sorted(daily.items()) if len(keys) >= 20
        ],
    }


def rank_library(library, source_sha256, plan=None, days=90, timezone_name="UTC"):
    """Pure offline calculation; source_sha256 identifies the exact input bytes."""
    if days < 1:
        raise ValueError("days must be positive")
    if library.get("schema_version") != 1:
        raise ValueError("Expected normalized library schema_version=1")
    zone = ZoneInfo(timezone_name)
    as_of = parse_timestamp(library.get("generated_at")).astimezone(zone)
    uid_value = (library.get("account") or {}).get("uid")
    if uid_value is None or str(uid_value) == "":
        raise ValueError("Snapshot account.uid is required")
    uid = str(uid_value)
    owned = {}
    kinds = set()
    for playlist in library.get("playlists", []):
        if ("owned" not in playlist.get("collections", []) or playlist.get("system_playlist")
                or str(playlist.get("kind")) == "3"):
            continue
        pid, kind = str(playlist.get("playlist_id", "")), str(playlist.get("kind", ""))
        if not kind or pid != f"{uid}:{kind}" or str(playlist.get("uid", uid)) != uid:
            raise ValueError("An ordinary playlist belongs to a different or invalid account")
        if pid in owned or kind in kinds:
            raise ValueError("Duplicate ordinary playlist identity")
        owned[pid], kinds = playlist, kinds | {kind}

    specs = []
    if plan is None:
        specs = [{"group_id": f"playlist-{p['kind']}", "title": p.get("title") or "",
                  "destination_playlist_id": pid, "destination_kind": str(p["kind"]),
                  "source_playlist_ids": [pid]} for pid, p in owned.items()]
    else:
        if plan.get("source_snapshot_sha256") != source_sha256:
            raise ValueError("Plan source hash does not match the supplied pre-merge snapshot")
        used, destinations, group_ids = set(), set(), set()
        for index, group in enumerate(plan.get("groups", []), 1):
            source_ids = [str(p["playlist_id"]) for p in group["sources"]]
            destination = str(group["destination"]["playlist_id"])
            group_id = str(group.get("group_id", f"group-{index}"))
            if (not source_ids or len(set(source_ids)) != len(source_ids) or used.intersection(source_ids)
                    or not set(source_ids) <= set(owned) or destination not in source_ids
                    or destination in destinations or group_id in group_ids):
                raise ValueError("Plan has overlapping, non-owned, system, or invalid destinations/sources")
            used.update(source_ids)
            destinations.add(destination)
            group_ids.add(group_id)
            specs.append({"group_id": group_id, "title": group.get("proposed_title") or owned[destination].get("title") or "",
                          "destination_playlist_id": destination, "destination_kind": str(owned[destination]["kind"]),
                          "source_playlist_ids": source_ids})
        if used != set(owned):
            raise ValueError("Plan must map every ordinary owned playlist exactly once")

    recent_days = min(30, days)
    window_days = sorted({days, recent_days, 180})
    starts = {str(n): as_of.date() - timedelta(days=n - 1) for n in window_days}
    coverage = {"retained_owned_entries": 0, "valid_timestamp_entries": 0,
                "missing_timestamp_entries": 0, "malformed_timestamp_entries": 0,
                "future_timestamp_entries": 0}
    targets = []
    for spec in specs:
        events = []
        local_coverage = {k: 0 for k in coverage}
        for pid in spec["source_playlist_ids"]:
            for track in owned[pid].get("tracks", []):
                local_coverage["retained_owned_entries"] += 1
                value = track.get("timestamp")
                if value is None or value == "":
                    local_coverage["missing_timestamp_entries"] += 1
                    continue
                try:
                    timestamp = parse_timestamp(value).astimezone(zone)
                except (ValueError, TypeError, OverflowError):
                    local_coverage["malformed_timestamp_entries"] += 1
                    continue
                if timestamp > as_of:
                    local_coverage["future_timestamp_entries"] += 1
                    continue
                local_coverage["valid_timestamp_entries"] += 1
                events.append({"timestamp": timestamp, "date": timestamp.date(), "key": track_key(track),
                               "source_playlist_id": pid, "position": track.get("position"),
                               "original_timestamp": value})
        for name, value in local_coverage.items():
            coverage[name] += value
        windows = {window: window_stats([e for e in events if e["date"] >= start])
                   for window, start in starts.items()}
        windows["all_time"] = window_stats(events)
        latest = max((e["timestamp"] for e in events), default=None)
        samples = sorted(events, key=lambda e: e["timestamp"], reverse=True)[:2]
        row = {**spec, "windows": windows, "timestamp_coverage": local_coverage,
               "latest_addition_at": latest.isoformat() if latest else None,
               "timestamp_samples": [{"source_playlist_id": e["source_playlist_id"], "position": e["position"],
                                       "track_key": e["key"], "original_timestamp": e["original_timestamp"]} for e in samples]}
        row["reason"] = (f"{windows[str(days)]['active_addition_days']} active adding days in {days} days; "
                         f"{windows[str(recent_days)]['active_addition_days']} in {recent_days} days; "
                         f"{windows[str(days)]['unique_track_album_additions']} unique track/album additions; "
                         f"latest {row['latest_addition_at'] or 'unavailable'}.")
        targets.append(row)

    def sort_key(row):
        latest = parse_timestamp(row["latest_addition_at"]).timestamp() if row["latest_addition_at"] else float("-inf")
        return (-row["windows"][str(days)]["active_addition_days"],
                -row["windows"][str(recent_days)]["active_addition_days"], -latest,
                -row["windows"][str(days)]["unique_track_album_additions"], row["destination_kind"])

    targets.sort(key=sort_key)
    for rank, row in enumerate(targets, 1):
        row["rank"] = rank
    total = coverage["retained_owned_entries"]
    coverage["coverage_percent"] = round(100 * coverage["valid_timestamp_entries"] / total, 3) if total else None
    return {
        "schema_version": 1, "status": "offline_recommended_order", "account_uid": uid,
        "source_snapshot_sha256": source_sha256, "source_snapshot_generated_at": library["generated_at"],
        "as_of": as_of.isoformat(), "timezone": timezone_name, "default_window_days": days,
        "recent_window_days": recent_days, "plan_source_hash_verified": plan is not None,
        "window_boundaries": {window: {"start": datetime.combine(start, time.min, zone).isoformat(),
                                       "end": as_of.isoformat()} for window, start in starts.items()},
        "ranking_method": "Descending distinct active addition days in the main window, then recent-window active days, latest original timestamp, unique exact track/album additions; destination kind breaks remaining ties.",
        "timestamp_coverage": coverage,
        "limitations": [
            "Only timestamps of retained source entries are counted; deleted entries and complete event history are unavailable.",
            "These are additions, not listening frequency or play counts.",
            "For historical ranking after a merge, supply the original pre-merge snapshot and its matching plan. Current snapshots may contain migration-created timestamps that cannot be distinguished automatically.",
            "A batch of many tracks on one day contributes one active day. A day with 20+ unique IDs is only a possible batch indicator, not proof of an import.",
            "Unique additions use exact track_id:album_id; metadata titles, remixes and alternate album IDs are never conflated.",
        ],
        "system_liked_tracks": {"kind": "3", "ranked": False},
        "ordered_group_ids": [r["group_id"] for r in targets],
        "ordered_destination_kinds": [r["destination_kind"] for r in targets],
        "targets": targets, "account_actions_performed": [],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=Path("outputs/current-snapshot/library.json"))
    parser.add_argument("--plan", type=Path, help="Merge plan matching the exact original pre-merge snapshot")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--timezone", default="UTC", help="IANA timezone, for example Europe/Moscow")
    parser.add_argument("--output", type=Path, default=Path("outputs/analysis/playlist-priority.json"))
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be positive")
    if args.output.resolve() in {p.resolve() for p in (args.snapshot, args.plan) if p is not None}:
        parser.error("Output must not overwrite the source snapshot or plan")
    try:
        data = args.snapshot.read_bytes()
        library = json.loads(data)
        plan = json.loads(args.plan.read_text(encoding="utf-8")) if args.plan else None
        result = rank_library(library, hashlib.sha256(data).hexdigest(), plan, args.days, args.timezone)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, args.output)
    except (OSError, ValueError, KeyError, TypeError, ZoneInfoNotFoundError, OverflowError) as exc:
        parser.error(str(exc))
    print(json.dumps({"ranked_targets": len(result["targets"]), "output": str(args.output.resolve()),
                      "timestamp_coverage": result["timestamp_coverage"], "account_changed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
