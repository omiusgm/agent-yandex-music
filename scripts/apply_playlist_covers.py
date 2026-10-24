#!/usr/bin/env python3
"""Validate playlist covers by default. Account changes require --apply.

Manifest: {"covers": [{"playlist_id": "uid:kind", "title": "...",
                       "image_path": "absolute-or-manifest-relative.png",
                       "image_sha256": "optional expected SHA256"}]}.
Only ordinary owned playlists are accepted. Cover POSTs have no automatic retry
or redirects. Original metadata and existing custom artwork are backed up first.
An uncertain POST is journaled and stops the run instead of silently retrying.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import time
from urllib.parse import urlparse
import zlib

import requests

from account_export import ReadOnlyAPI
from public_export import API, now, write_json

MAX_BYTES = 20_000_000
AVATAR_HOSTS = frozenset({"avatars.yandex.net", "avatars.mds.yandex.net",
                          "avatars.yandex.ru", "avatars.mds.yandex.ru"})


class CoverError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def image_info(data):
    """Inspect actual image bytes, never trusting the filename or MIME suffix."""
    if not 0 < len(data) <= MAX_BYTES:
        raise CoverError("Image must be between 1 and 20,000,000 bytes")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        offset, dimensions, ended, has_pixels = 8, None, False, False
        while offset + 12 <= len(data):
            length = struct.unpack(">I", data[offset:offset + 4])[0]
            kind = data[offset + 4:offset + 8]
            stop = offset + 8 + length
            if stop + 4 > len(data):
                raise CoverError("Truncated PNG chunk")
            body = data[offset + 8:stop]
            expected_crc = struct.unpack(">I", data[stop:stop + 4])[0]
            if zlib.crc32(kind + body) & 0xFFFFFFFF != expected_crc:
                raise CoverError("PNG checksum mismatch")
            if kind == b"IHDR":
                if dimensions is not None or length != 13 or offset != 8:
                    raise CoverError("Invalid PNG header")
                dimensions = struct.unpack(">II", body[:8])
            if kind == b"IDAT":
                has_pixels = True
            offset = stop + 4
            if kind == b"IEND":
                ended = True
                break
        if not dimensions or not ended or not has_pixels or offset != len(data):
            raise CoverError("Incomplete PNG")
        width, height = dimensions
        mime, suffix = "image/png", ".png"
    elif data.startswith(b"\xff\xd8"):
        offset, dimensions = 2, None
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                raise CoverError("Invalid JPEG marker")
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in (0xD9, 0xDA):
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                continue
            size = struct.unpack(">H", data[offset:offset + 2])[0]
            if size < 2 or offset + size > len(data):
                raise CoverError("Truncated JPEG segment")
            if marker in sof_markers:
                if size < 8:
                    raise CoverError("Invalid JPEG dimensions")
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                dimensions = width, height
            offset += size
        if not dimensions or not data.endswith(b"\xff\xd9"):
            raise CoverError("Incomplete JPEG")
        width, height = dimensions
        mime, suffix = "image/jpeg", ".jpg"
    else:
        raise CoverError("Only actual PNG/JPEG images are supported")
    if not 0 < width < 2500 or not 0 < height < 2500:
        raise CoverError("Image dimensions must be positive and less than 2500 pixels per side")
    return {"mime_type": mime, "suffix": suffix, "width": width, "height": height,
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def track_pairs(detail):
    entries = detail.get("tracks")
    if not isinstance(entries, list) or detail.get("trackCount") != len(entries):
        raise CoverError("Current short playlist response did not prove complete membership")
    pairs = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("id") is None:
            raise CoverError("Playlist entry has no ID")
        pairs.append([str(entry["id"]), str(entry["albumId"]) if entry.get("albumId") is not None else None])
    return pairs


def stable_content(detail):
    return {"uid": str(detail.get("uid")), "kind": str(detail.get("kind")),
            "title": detail.get("title"), "description": detail.get("description"),
            "visibility": detail.get("visibility"), "collective": detail.get("collective"),
            "track_count": detail.get("trackCount"), "ordered_track_album_pairs": track_pairs(detail)}


def cover_uri(detail):
    return (detail.get("cover") or {}).get("uri")


def avatar_url(uri, size="1000x1000"):
    if not isinstance(uri, str) or not uri:
        raise CoverError("Cover has no image URI")
    value = uri.replace("%%", size)
    if value.startswith("//"):
        value = "https:" + value
    elif not value.startswith("https://"):
        value = "https://" + value
    parsed = urlparse(value)
    if (parsed.scheme != "https" or parsed.hostname not in AVATAR_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise CoverError("Cover URI is not on an allowed official Yandex avatar host")
    return value


def download_avatar(uri, prefix):
    # Deliberately separate from the OAuth session. Credentials never reach CDN.
    url = avatar_url(uri)
    with requests.Session() as session:
        session.trust_env = False
        with session.get(url, timeout=(12, 30), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise CoverError(f"Anonymous cover verification returned HTTP {response.status_code}")
            parts, total = [], 0
            for part in response.iter_content(65536):
                total += len(part)
                if total > MAX_BYTES:
                    raise CoverError("CDN image exceeded the size limit")
                parts.append(part)
    data = b"".join(parts)
    info = image_info(data)
    path = prefix.with_suffix(info["suffix"])
    path.write_bytes(data)
    return {"url": url, "path": str(path), **info}


class CoverAPI(ReadOnlyAPI):
    def read_playlist(self, pid):
        uid, kind = pid.split(":")
        detail = self.fetch(f"/users/{uid}/playlists/{kind}", {"rich-tracks": "false"})["result"]
        if (str(detail.get("uid")), str(detail.get("kind"))) != (uid, kind):
            raise CoverError("Playlist identity mismatch")
        track_pairs(detail)
        return detail

    def upload(self, pid, data, info):
        uid, kind = pid.split(":")
        # Zero retries, including connection retries, and no redirect forwarding.
        if not hasattr(self.local, "cover_write_session"):
            session = requests.Session()
            session.headers["Authorization"] = "OAuth " + self._token
            session.headers["User-Agent"] = "YandexPlaylistCoverUpload/1.0"
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            self.local.cover_write_session = session
        try:
            with self.local.cover_write_session.post(
                API + f"/users/{uid}/playlists/{kind}/cover/upload",
                files={"image": ("cover" + info["suffix"], data, info["mime_type"])},
                timeout=(12, self.timeout), allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise CoverError(f"Cover POST returned HTTP {response.status_code}; no retry performed")
                payload = response.json()
        except requests.RequestException as exc:
            raise CoverError(f"Cover POST transport outcome unknown: {type(exc).__name__}") from None
        if not isinstance(payload, dict) or payload.get("error") or not isinstance(payload.get("result"), dict):
            raise CoverError("Cover POST returned no successful result; no retry performed")
        result = payload["result"]
        if not cover_uri(result):
            raise CoverError("Cover POST returned no cover URI; no retry performed")
        return result


def prepare(args):
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = manifest.get("covers")
    if not isinstance(rows, list) or not rows:
        raise CoverError("Manifest must contain a non-empty covers array")
    library = json.loads(args.snapshot.read_text(encoding="utf-8"))
    uid = str(library["account"]["uid"])
    owned = {p["playlist_id"]: p for p in library["playlists"]
             if "owned" in p.get("collections", []) and not p.get("system_playlist") and str(p["kind"]) != "3"}
    selected, seen = [], set()
    for row in rows:
        pid = str(row.get("playlist_id", ""))
        if not re.fullmatch(r"\d+:\d+", pid) or pid in seen or pid not in owned or not pid.startswith(uid + ":"):
            raise CoverError("Manifest contains a duplicate, system, unknown, or non-owned playlist")
        seen.add(pid)
        if args.only and pid not in args.only:
            continue
        path = Path(row["image_path"])
        path = path.resolve() if path.is_absolute() else (manifest_path.parent / path).resolve()
        info = image_info(path.read_bytes())
        if row.get("image_sha256") and row["image_sha256"] != info["sha256"]:
            raise CoverError("Image content does not match its declared SHA256")
        title = row.get("title", owned[pid]["title"])
        if title != owned[pid]["title"]:
            raise CoverError("Manifest title does not match snapshot playlist identity")
        selected.append({"playlist_id": pid, "title": title, "image_path": str(path), "image": info})
    if args.only and set(args.only) - seen:
        raise CoverError("--only includes a playlist absent from the manifest")
    return manifest, library, selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, default=Path("outputs/current-snapshot/library.json"))
    parser.add_argument("--output", type=Path, default=Path("outputs/covers/apply"))
    parser.add_argument("--token-file", type=Path, default=Path("~/.yandex-music-mcp/token"))
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--only", action="append", help="One manifest playlist ID; repeat for a reviewed subset")
    parser.add_argument("--apply", action="store_true", help="Actually upload covers; omitted means read-only preflight")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    os.umask(0o077)
    try:
        manifest, library, selected = prepare(args)
        root = args.output.resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        api = CoverAPI(args.token_file, args.timeout)
        account_uid = str((api.fetch("/account/status")["result"].get("account") or {}).get("uid"))
        if account_uid != str(library["account"]["uid"]):
            raise CoverError("Authenticated account differs from the cover manifest snapshot")
        live_owned = api.fetch(f"/users/{account_uid}/playlists/list")["result"]
        live_owned_ids = {f"{p['uid']}:{p['kind']}" for p in live_owned if str(p["kind"]) != "3"}
        if any(row["playlist_id"] not in live_owned_ids for row in selected):
            raise CoverError("A target is no longer an ordinary owned playlist")
        plan = {"checked_at": now(), "mode": "apply" if args.apply else "dry_run",
                "manifest_sha256": digest(manifest), "account_uid": account_uid, "covers": []}
        before_by_id = {}
        for row in selected:
            pid = row["playlist_id"]
            before = api.read_playlist(pid)
            if before.get("title") != row["title"]:
                raise CoverError("Live playlist title changed; refresh its reviewed cover mapping")
            before_by_id[pid] = before
            plan["covers"].append({**row, "current_revision": before.get("revision"),
                "current_track_count": before["trackCount"], "before_cover": before.get("cover"),
                "before_og_image": before.get("ogImage"), "content_sha256": digest(stable_content(before))})
        write_json(root / ("apply-preflight.json" if args.apply else "dry-run-plan.json"), plan)
        if not args.apply:
            print(json.dumps({"mode": "dry_run", "validated_covers": len(selected),
                              "account_changed": False, "plan": str(root / "dry-run-plan.json")}))
            return 0
        journal_path = root / "journal.json"
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else {
            "manifest_sha256": digest(manifest), "created_at": now(), "covers": {}}
        if journal["manifest_sha256"] != digest(manifest):
            raise CoverError("Journal belongs to a different cover manifest")
        for row in selected:
            pid = row["playlist_id"]
            folder = root / pid.replace(":", "-")
            folder.mkdir(exist_ok=True, mode=0o700)
            previous = journal["covers"].get(pid)
            if previous:
                current = api.read_playlist(pid)
                if (previous.get("state") == "verified" and previous.get("image_sha256") == row["image"]["sha256"]
                        and cover_uri(current) == previous.get("verified_cover_uri") and (current.get("cover") or {}).get("custom") is True):
                    print(f"Already verified {pid}", flush=True)
                    continue
                raise CoverError(f"Existing unfinished or changed cover journal for {pid}; inspect evidence before another upload")
            before = api.read_playlist(pid)
            if stable_content(before) != stable_content(before_by_id[pid]) or before.get("cover") != before_by_id[pid].get("cover"):
                raise CoverError("Playlist changed after preflight; no cover was uploaded to this playlist")
            write_json(folder / "before.json", before)
            if (before.get("cover") or {}).get("custom") is True:
                backup = download_avatar(cover_uri(before), folder / "before-custom-cover")
                write_json(folder / "before-custom-cover-image.json", backup)
                restore_path = root / "restore-custom-covers-manifest.json"
                restore = json.loads(restore_path.read_text()) if restore_path.exists() else {"covers": []}
                restore["covers"] = [v for v in restore["covers"] if v["playlist_id"] != pid]
                restore["covers"].append({"playlist_id": pid, "title": before.get("title"),
                    "image_path": backup["path"], "image_sha256": backup["sha256"]})
                write_json(restore_path, restore)
            # Generated mosaics retain raw constituent URIs. No undocumented
            # reset-to-generated-mosaic endpoint is claimed or invoked.
            data = Path(row["image_path"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != row["image"]["sha256"]:
                raise CoverError("Image changed after preflight")
            record = {"state": "attempting", "started_at": now(), "image_sha256": row["image"]["sha256"],
                      "before_file": str(folder / "before.json"), "before_cover_uri": cover_uri(before)}
            journal["covers"][pid] = record
            write_json(journal_path, journal)
            try:
                response = api.upload(pid, data, row["image"])
                expected_uri = cover_uri(response)
                record["response_cover_uri"] = expected_uri
                write_json(folder / "upload-response.json", response)
                write_json(journal_path, journal)
                after = None
                for attempt in range(4):
                    after = api.read_playlist(pid)
                    if cover_uri(after) == expected_uri and (after.get("cover") or {}).get("custom") is True:
                        break
                    if attempt < 3:
                        time.sleep(1)
                write_json(folder / "after.json", after)
                if stable_content(after) != stable_content(before):
                    raise CoverError("Playlist content changed across cover upload; stop for inspection")
                if cover_uri(after) != expected_uri or (after.get("cover") or {}).get("custom") is not True:
                    raise CoverError("Live playlist did not confirm the returned custom cover")
                if cover_uri(after) == cover_uri(before):
                    raise CoverError("Cover URI did not change; replacement is unverified")
                preview = download_avatar(expected_uri, folder / "after-received-cover")
                write_json(folder / "after-received-cover-image.json", preview)
                record.update({"state": "verified", "verified_at": now(), "verified_cover_uri": expected_uri,
                               "track_membership_and_metadata_unchanged": True, "received_image": preview})
                write_json(journal_path, journal)
                print(f"Verified custom cover {pid}; {after['trackCount']} tracks unchanged", flush=True)
            except Exception as exc:
                record.update({"state": "uncertain", "error": str(exc), "stopped_at": now()})
                try:
                    observed = api.read_playlist(pid)
                    write_json(folder / "after-error-read.json", observed)
                    record["observed_cover_uri"] = cover_uri(observed)
                    record["observed_content_unchanged"] = stable_content(observed) == stable_content(before)
                except Exception as read_exc:
                    record["reconciliation_read_error"] = str(read_exc)
                write_json(journal_path, journal)
                raise CoverError(f"Upload {pid} requires inspection; no automatic retry: {exc}") from None
        print(json.dumps({"mode": "apply", "verified_covers": sum(r.get("state") == "verified" for r in journal["covers"].values()),
                          "journal": str(journal_path)}))
        return 0
    except Exception as exc:
        print(f"Cover operation stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
