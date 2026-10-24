"""Synthetic offline timestamp ranking tests; no account or network access."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from rank_playlists import main, rank_library


def track(track_id, timestamp, album="1"):
    return {"track_id": str(track_id), "album_id": album, "timestamp": timestamp}


def playlist(kind, tracks, collections=None):
    return {"playlist_id": f"7:{kind}", "uid": "7", "kind": str(kind), "title": f"List {kind}",
            "collections": ["owned"] if collections is None else collections, "tracks": tracks}


def snapshot(playlists, generated_at="2026-10-07T22:00:00+00:00"):
    return {"schema_version": 1, "generated_at": generated_at, "account": {"uid": "7"}, "playlists": playlists}


class RankingTests(unittest.TestCase):
    def test_distinct_days_beat_single_day_batch_and_keep_album_versions(self):
        frequent = playlist(10, [track(1, "2026-10-01T12:00:00Z"), track(1, "2026-10-02T12:00:00Z", "2")])
        batch = playlist(11, [track(i, "2026-10-07T12:00:00Z") for i in range(30)])
        result = rank_library(snapshot([batch, frequent]), "source")
        self.assertEqual(result["ordered_destination_kinds"], ["10", "11"])
        self.assertEqual(result["targets"][0]["windows"]["90"]["unique_track_album_additions"], 2)
        self.assertEqual(result["targets"][1]["windows"]["90"]["active_addition_days"], 1)
        self.assertEqual(len(result["targets"][1]["windows"]["90"]["possible_bulk_days"]), 1)

    def test_coverage_ignores_missing_malformed_naive_and_future_dates(self):
        data = snapshot([playlist(10, [track(1, None), track(2, "broken"), track(3, "2026-10-01"),
                                         track(4, "2026-10-08T00:00:00Z"), track(5, "2026-10-07T12:00:00Z")])])
        result = rank_library(data, "source")
        cov = result["timestamp_coverage"]
        self.assertEqual((cov["valid_timestamp_entries"], cov["missing_timestamp_entries"],
                          cov["malformed_timestamp_entries"], cov["future_timestamp_entries"]), (1, 1, 2, 1))
        self.assertEqual(cov["coverage_percent"], 20)

    def test_timezone_controls_inclusive_calendar_day_boundary(self):
        data = snapshot([playlist(10, [track(1, "2026-10-07T20:30:00Z"), track(2, "2026-10-07T21:30:00Z")])])
        utc = rank_library(data, "source", days=1)
        moscow = rank_library(data, "source", days=1, timezone_name="Europe/Moscow")
        self.assertEqual(utc["targets"][0]["windows"]["1"]["unique_track_album_additions"], 2)
        self.assertEqual(moscow["targets"][0]["windows"]["1"]["unique_track_album_additions"], 1)
        self.assertTrue(moscow["window_boundaries"]["1"]["start"].startswith("2026-10-08T00:00:00+03:00"))

    def test_merge_uses_original_source_dates_and_excludes_other_collections(self):
        data = snapshot([playlist(10, [track(1, "2026-10-01T12:00:00Z")]),
                         playlist(11, [track(1, "2026-10-01T12:00:00Z"), track(2, "2026-10-02T12:00:00Z")]),
                         playlist(3, [track(99, "2026-10-07T12:00:00Z")]),
                         playlist(12, [track(88, "2026-10-07T12:00:00Z")], ["liked"])])
        plan = {"source_snapshot_sha256": "source", "groups": [{"group_id": "g01", "proposed_title": "Combined",
                "sources": [{"playlist_id": "7:10"}, {"playlist_id": "7:11"}],
                "destination": {"playlist_id": "7:10"}, "generated_at": "2030-01-01T00:00:00Z"}]}
        result = rank_library(data, "source", plan=plan)
        row = result["targets"][0]
        self.assertEqual(result["ordered_destination_kinds"], ["10"])
        self.assertEqual(row["title"], "Combined")
        self.assertEqual(row["latest_addition_at"], "2026-10-02T12:00:00+00:00")
        self.assertEqual(row["windows"]["90"]["retained_entry_additions"], 3)
        self.assertEqual(row["windows"]["90"]["distinct_track_day_additions"], 2)
        self.assertEqual(result["account_actions_performed"], [])
        with self.assertRaisesRegex(ValueError, "hash"):
            rank_library(data, "different", plan=plan)
        bad = copy.deepcopy(plan)
        bad["groups"][0]["sources"].append({"playlist_id": "7:3"})
        with self.assertRaises(ValueError):
            rank_library(data, "source", plan=bad)

    def test_recent_days_then_latest_timestamp_break_activity_ties(self):
        data = snapshot([playlist(10, [track(1, "2026-07-20T12:00:00Z")]),
                         playlist(11, [track(2, "2026-10-01T12:00:00Z")]),
                         playlist(12, [track(3, "2026-10-02T12:00:00Z")])])
        self.assertEqual(rank_library(data, "source")["ordered_destination_kinds"], ["12", "11", "10"])

    def test_cli_writes_account_binding_and_exact_input_hash_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "library.json", Path(directory) / "priority.json"
            source.write_text(json.dumps(snapshot([playlist(10, [track(1, "2026-10-01T12:00:00Z")])])), encoding="utf-8")
            before = source.read_bytes()
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--snapshot", str(source), "--output", str(output)]), 0)
            result = json.loads(output.read_text())
            self.assertEqual(result["account_uid"], "7")
            self.assertEqual(result["source_snapshot_sha256"], hashlib.sha256(before).hexdigest())
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
