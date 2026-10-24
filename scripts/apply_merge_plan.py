#!/usr/bin/env python3
"""Review a merge by default; writes require --apply, deletion its own gate.

Only append tracks: retain target duplicates and every distinct track/album pair.
POSTs are never automatically retried. Interrupted writes are reconciled by GET.
Yandex deletion has no revision precondition or multi-playlist transaction: all
sources are checked before retirement, then each is rechecked immediately before
deletion. A subsequent network failure can leave a journaled partial retirement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import requests
from account_export import ReadOnlyAPI
from public_export import API, now, write_json


class SafetyError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class LiveAPI(ReadOnlyAPI):
    def read(self, pid):
        uid, kind = pid.split(":")
        try:
            return self.fetch(f"/users/{uid}/playlists/{kind}", {"rich-tracks": "false"})["result"]
        except RuntimeError as exc:
            if str(exc) == "API HTTP 404":
                return None
            raise

    def post(self, pid, operation, data):
        if operation not in {"change", "visibility", "name", "delete"}:
            raise SafetyError("Unsupported write endpoint")
        uid, kind = pid.split(":")
        # Even connection retries may otherwise apply to POST despite urllib3's
        # allowed_methods setting. A separate persistent session has zero retries.
        if not hasattr(self.local, "write_session"):
            self.local.write_session = requests.Session()
            self.local.write_session.headers.update(self.session().headers)
            self.local.write_session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        try:
            with self.local.write_session.post(API + f"/users/{uid}/playlists/{kind}/{operation}", data=data,
                                     timeout=(12, self.timeout), allow_redirects=False) as response:
                if response.status_code != 200:
                    raise SafetyError(f"POST returned HTTP {response.status_code}")
                result = response.json()
                if not isinstance(result, dict) or "result" not in result or result.get("error"):
                    raise SafetyError("POST response was not a successful result")
                return result
        except requests.RequestException as exc:
            raise SafetyError(f"POST transport outcome unknown: {type(exc).__name__}") from None


class MergeExecutor:
    def __init__(self, plan, library, api, run_dir, apply=False, phases=("private", "add", "rename"),
                 retire_user_approved=False, batch_size=100):
        self.plan, self.library, self.api = plan, library, api
        self.apply, self.phases, self.batch_size = apply, set(phases), batch_size
        if "retire" in self.phases and not retire_user_approved:
            raise SafetyError("Retirement requires --retire-user-approved")
        if not self.phases <= {"private", "add", "rename", "retire"} or not 1 <= batch_size <= 100:
            raise SafetyError("Invalid phases or batch size")
        self.root = Path(run_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "journal.json"
        self.reference = {p["playlist_id"]: p for p in library["playlists"]}
        self.journal = json.loads(self.path.read_text()) if self.path.exists() else {
            "plan_sha256": digest(plan), "source_snapshot_sha256": plan["source_snapshot_sha256"], "groups": {}}
        if self.journal["plan_sha256"] != digest(plan):
            raise SafetyError("Journal belongs to a different immutable plan")
        seen = set()
        for group in plan["groups"]:
            source_ids = {s["playlist_id"] for s in group["sources"]}
            destination = group["destination"]["playlist_id"]
            retirement = {s["playlist_id"] for s in group["retire_only_after_verification_and_approval"]}
            if destination not in source_ids or retirement != source_ids - {destination} or seen & source_ids:
                raise SafetyError("Overlapping groups or invalid destination/retirement scope")
            seen |= source_ids
            for pid in source_ids:
                ref = self.reference[pid]
                if (ref.get("kind") == "3" or "owned" not in ref.get("collections", [])
                        or ref["uid"] != str(library["account"]["uid"])):
                    raise SafetyError("System or non-owned playlist cannot be changed")
            required = {e["key"] for pid in source_ids for e in self.reference[pid]["tracks"]}
            target_keys = [e["key"] for e in self.reference[destination]["tracks"]]
            if (required != set(group["required_unique_keys"]) or len(group["add_keys"]) != len(set(group["add_keys"]))
                    or set(group["add_keys"]) != required - set(target_keys)):
                raise SafetyError("Plan does not preserve the complete source identity union")
        self.save()

    def save(self):
        write_json(self.path, self.journal)
        with self.path.open("rb") as handle:
            os.fsync(handle.fileno())

    def keys(self, detail, pid, group):
        if detail is None or f"{detail.get('uid')}:{detail.get('kind')}" != pid:
            raise SafetyError(f"Missing or wrong playlist {pid}")
        rows = detail.get("tracks") or []
        if detail.get("trackCount") != len(rows):
            raise SafetyError(f"Incomplete current membership {pid}")
        inferred = {}
        for source in group["sources"]:
            for entry in self.reference[source["playlist_id"]]["tracks"]:
                if entry.get("album_id_source") == "sole_track_album":
                    inferred.setdefault(entry["track_id"], set()).add(entry["album_id"])
        keys = []
        for row in rows:
            tid, aid = row.get("id"), row.get("albumId")
            if tid is None:
                raise SafetyError(f"Missing track identity {pid}")
            if aid is None:
                albums = (row.get("track") or {}).get("albums") or []
                candidates = {str(a["id"]) for a in albums if a.get("id") is not None}
                if not candidates:
                    candidates = inferred.get(str(tid), set())
                if len(candidates) != 1:
                    raise SafetyError(f"Unresolved album identity {pid}, track {tid}")
                aid = next(iter(candidates))
            keys.append(f"{tid}:{aid}")
        return keys

    def record(self, group, label, detail):
        file = self.root / group["group_id"] / (label.replace(":", "-") + ".json")
        write_json(file, detail)
        return {"file": str(file.relative_to(self.root)), "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}

    def matches(self, detail, pid, expected, group):
        if expected.get("absent"):
            return detail is None
        return (detail is not None and self.keys(detail, pid, group) == expected["keys"]
                and all(detail.get(k) == expected[k] for k in ("title", "visibility") if k in expected))

    def reconcile(self, action, group):
        current = self.api.read(action["playlist_id"])
        action["readback"] = self.record(group, f"receipt-{action['number']}", current)
        if not self.matches(current, action["playlist_id"], action["expected"], group):
            action["status"] = "unresolved"
            self.save()
            raise SafetyError("Write outcome differs from exact expected state; no automatic POST retry")
        action["status"], action["verified_at"] = "verified", now()
        self.save()
        return current

    def write(self, group, state, pid, operation, data, expected):
        action = {"number": len(state["actions"]) + 1, "playlist_id": pid, "operation": operation,
                  "data": data, "expected": expected, "status": "intent", "started_at": now()}
        state["actions"].append(action)
        self.save()  # Durable intent precedes the only send attempt.
        try:
            response = self.api.post(pid, operation, data)
            action["response"] = self.record(group, f"response-{action['number']}", response)
        except Exception as exc:
            action["error"] = type(exc).__name__  # Never serialize request or credentials.
        return self.reconcile(action, group)

    def check_source(self, group, state, pid):
        detail = self.api.read(pid)
        baseline = state["baseline"][pid]
        if (self.keys(detail, pid, group) != baseline["keys"] or detail.get("revision") != baseline["revision"]
                or any(detail.get(k) != baseline[k] for k in ("title", "visibility"))):
            raise SafetyError(f"Source changed; retain originals in this group: {pid}")
        return detail

    def run_group(self, group):
        gid, target = group["group_id"], group["destination"]["playlist_id"]
        state = self.journal["groups"].setdefault(gid, {"actions": [], "baseline": {}})
        for action in state["actions"]:
            if action["status"] != "verified":
                self.reconcile(action, group)  # Never blindly resend a prior intent.
        retired = {a["playlist_id"] for a in state["actions"] if a["operation"] == "delete" and a["status"] == "verified"}
        if not state["baseline"]:
            baseline = {}
            for source in group["sources"]:
                pid = source["playlist_id"]
                detail = self.api.read(pid)
                keys = self.keys(detail, pid, group)
                ref = self.reference[pid]
                if (keys != [e["key"] for e in ref["tracks"]] or detail.get("revision") != source["revision"]
                        or detail.get("title") != ref["title"] or detail.get("visibility") != ref["visibility"]):
                    raise SafetyError(f"Live source differs from approved snapshot: {pid}")
                baseline[pid] = {"keys": keys, "revision": detail["revision"], "title": detail.get("title"),
                                 "visibility": detail.get("visibility"), "backup": self.record(group, pid + "-before", detail)}
            state["baseline"] = baseline
            self.save()
        for item in state["baseline"].values():
            backup = self.root / item["backup"]["file"]
            if hashlib.sha256(backup.read_bytes()).hexdigest() != item["backup"]["sha256"]:
                raise SafetyError("Source backup integrity failure")
        source_ids = [s["playlist_id"] for s in group["sources"] if s["playlist_id"] != target]
        for pid in source_ids:
            if pid not in retired:
                self.check_source(group, state, pid)
        current = self.api.read(target)
        current_keys = self.keys(current, target, group)
        if current.get("title") not in {state["baseline"][target]["title"], group["proposed_title"]}:
            raise SafetyError("Destination was renamed outside this execution")
        original = state["baseline"][target]["keys"]
        expected = original + group["add_keys"]
        # Exact prefix preserves original order and duplicate multiplicities.
        if current_keys != expected[:len(current_keys)] or not len(original) <= len(current_keys) <= len(expected):
            raise SafetyError("Destination has unexpected additions, removals, or reordering")
        private_required = (group["destination"].get("required_visibility") == "private"
                            or any(b["visibility"] == "private" for b in state["baseline"].values()))
        if not self.apply:
            return {"group_id": gid, "status": "dry_run_verified", "remaining_additions": len(expected) - len(current_keys),
                    "retire_candidates": len(source_ids), "requires_private": private_required}
        if private_required and current.get("visibility") != "private":
            if "private" not in self.phases:
                raise SafetyError("Private destination must be verified before adding private source tracks")
            current = self.write(group, state, target, "visibility", {"value": "private"},
                                 {"keys": current_keys, "visibility": "private"})
        if "add" in self.phases:
            while len(current_keys) < len(expected):
                batch = expected[len(current_keys):len(current_keys) + self.batch_size]
                tracks = [{"id": key.split(":")[0], "albumId": key.split(":")[1]} for key in batch]
                wanted = current_keys + batch
                diff = [{"op": "insert", "at": len(current_keys), "tracks": tracks}]
                current = self.write(group, state, target, "change", {"kind": target.split(":")[1],
                    "revision": current["revision"], "diff": json.dumps(diff, separators=(",", ":"))},
                    {"keys": wanted, **({"visibility": "private"} if private_required else {})})
                current_keys = self.keys(current, target, group)
        if self.phases & {"rename", "retire"}:
            current = self.api.read(target)
            if not self.matches(current, target, {"keys": expected, **({"visibility": "private"} if private_required else {})}, group):
                raise SafetyError("Incomplete destination; retain ALL source playlists")
            for pid in source_ids:
                if pid not in retired:
                    self.check_source(group, state, pid)
        if "rename" in self.phases and current.get("title") != group["proposed_title"]:
            current = self.write(group, state, target, "name", {"value": group["proposed_title"]},
                                 {"keys": expected, "title": group["proposed_title"], **({"visibility": "private"} if private_required else {})})
        if "retire" in self.phases:
            if current.get("title") != group["proposed_title"]:
                raise SafetyError("Destination agreed title must be verified before retirement")
            for pid in source_ids:
                if pid in retired:
                    continue
                self.check_source(group, state, pid)
                if not self.matches(self.api.read(target), target, {"keys": expected, "title": group["proposed_title"],
                                    **({"visibility": "private"} if private_required else {})}, group):
                    raise SafetyError("Destination changed during retirement; remaining sources retained")
                self.write(group, state, pid, "delete", {}, {"absent": True})
                retired.add(pid)
        state["verified_target"] = self.record(group, "target-final", self.api.read(target))
        state["last_completed_at"] = now()
        self.save()
        return {"group_id": gid, "status": "verified", "phases": sorted(self.phases),
                "retired": sorted(retired), "target_entries": len(current_keys),
                "target_complete": current_keys == expected, "remaining_additions": len(expected) - len(current_keys)}

    def run(self, group_ids=None):
        if group_ids and not group_ids <= {g["group_id"] for g in self.plan["groups"]}:
            raise SafetyError("Unknown group ID selection")
        result = []
        for group in self.plan["groups"]:
            if group_ids and group["group_id"] not in group_ids:
                continue
            try:
                result.append(self.run_group(group))
            except Exception as exc:
                result.append({"group_id": group["group_id"], "status": "blocked", "error": str(exc)})
            print(json.dumps(result[-1], ensure_ascii=False), flush=True)
        write_json(self.root / "last-report.json", {"generated_at": now(), "applied": self.apply, "groups": result})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=Path("outputs/analysis/merge-plan.json"))
    parser.add_argument("--snapshot", type=Path, default=Path("outputs/current-snapshot/library.json"))
    parser.add_argument("--run-dir", type=Path, default=Path("outputs/merge-execution"))
    parser.add_argument("--token-file", type=Path, default=Path("~/.yandex-music-mcp/token"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--phases", default="private,add,rename", help="Comma-separated private,add,rename,retire")
    parser.add_argument("--retire-user-approved", action="store_true")
    parser.add_argument("--groups", help="Comma-separated group IDs; default all")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    os.umask(0o077)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    raw = args.snapshot.read_bytes()
    if hashlib.sha256(raw).hexdigest() != plan["source_snapshot_sha256"]:
        raise SafetyError("Snapshot bytes do not match the approved plan")
    library = json.loads(raw)
    api = LiveAPI(args.token_file, 30)
    if str(api.fetch("/account/status")["result"]["account"]["uid"]) != str(library["account"]["uid"]):
        raise SafetyError("Token account differs from the plan owner")
    executor = MergeExecutor(plan, library, api, args.run_dir, args.apply, args.phases.split(","),
                             args.retire_user_approved, args.batch_size)
    results = executor.run(set(args.groups.split(",")) if args.groups else None)
    return 2 if any(r["status"] == "blocked" for r in results) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SafetyError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
