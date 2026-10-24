"""Offline mutation-safety tests. No credentials or network access."""
import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from apply_merge_plan import LiveAPI, MergeExecutor, SafetyError


class FakeAPI:
    def __init__(self, data):
        self.data, self.posts = copy.deepcopy(data), []
        self.timeout_after_apply = False
        self.timeout_before_apply = False
        self.omit_last_insert = False

    def read(self, pid):
        return copy.deepcopy(self.data.get(pid))

    def post(self, pid, operation, data):
        self.posts.append((pid, operation, copy.deepcopy(data)))
        if self.timeout_before_apply:
            raise TimeoutError("unknown write outcome")
        item = self.data[pid]
        if operation == "change":
            assert data["revision"] == item["revision"]
            diff = json.loads(data["diff"])[0]
            rows = copy.deepcopy(diff["tracks"])
            if self.omit_last_insert:
                rows = rows[:-1]
            item["tracks"][diff["at"]:diff["at"]] = rows
            item["trackCount"] = len(item["tracks"])
        elif operation == "visibility":
            item["visibility"] = data["value"]
        elif operation == "name":
            item["title"] = data["value"]
        elif operation == "delete":
            del self.data[pid]
        item["revision"] += 1
        if self.timeout_after_apply:
            raise TimeoutError("response lost after applied write")
        return {"result": "ok"}


def fixture():
    def entry(tid):
        return {"id": str(tid), "albumId": "10"}
    data = {
        "1:10": {"uid": 1, "kind": 10, "title": "target", "revision": 1,
                 "visibility": "public", "trackCount": 2, "tracks": [entry(100), entry(100)]},
        "1:11": {"uid": 1, "kind": 11, "title": "source", "revision": 1,
                 "visibility": "private", "trackCount": 2, "tracks": [entry(200), entry(100)]}}
    library = {"account": {"uid": "1"}, "playlists": []}
    for pid, item in data.items():
        library["playlists"].append({"playlist_id": pid, "uid": "1", "kind": str(item["kind"]),
            "title": item["title"], "visibility": item["visibility"], "collections": ["owned"],
            "tracks": [{"key": f"{e['id']}:10", "track_id": e["id"], "album_id": "10", "album_id_source": "playlist_entry"} for e in item["tracks"]]})
    plan = {"source_snapshot_sha256": "test-only", "groups": [{
        "group_id": "g01", "proposed_title": "merged", "sources": [
            {"playlist_id": "1:10", "revision": 1}, {"playlist_id": "1:11", "revision": 1}],
        "destination": {"playlist_id": "1:10", "required_visibility": "private"},
        "required_unique_keys": ["100:10", "200:10"], "add_keys": ["200:10"],
        "retire_only_after_verification_and_approval": [{"playlist_id": "1:11"}]}]}
    return plan, library, data


class MergeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plan, self.library, self.data = fixture()
        self.api = FakeAPI(self.data)

    def executor(self, **kwargs):
        return MergeExecutor(self.plan, self.library, self.api, self.temp.name, **kwargs)

    def run_quiet(self, executor):
        with redirect_stdout(io.StringIO()):
            return executor.run()

    def test_dry_run_never_posts(self):
        result = self.run_quiet(self.executor())
        self.assertEqual(result[0]["status"], "dry_run_verified")
        self.assertEqual(self.api.posts, [])

    def test_post_transport_has_zero_connection_status_and_read_retries(self):
        token_path = Path(self.temp.name) / "dummy-token"
        token_path.write_text("offline-test-token")
        api = LiveAPI(token_path, 1)
        with patch("requests.Session.post", side_effect=TimeoutError("offline fake")):
            with self.assertRaises(TimeoutError):
                api.post("1:10", "name", {"value": "test"})
        retry = api.local.write_session.get_adapter("https://").max_retries
        self.assertEqual(retry.total, 0)
        self.assertFalse(retry.read)
        self.assertEqual(api.session().get_adapter("https://").max_retries.allowed_methods, frozenset({"GET"}))

    def test_private_before_append_and_existing_duplicates_survive(self):
        result = self.run_quiet(self.executor(apply=True))
        self.assertEqual(result[0]["status"], "verified")
        self.assertEqual([p[1] for p in self.api.posts], ["visibility", "change", "name"])
        self.assertEqual([e["id"] for e in self.api.data["1:10"]["tracks"]], ["100", "100", "200"])
        self.assertIn("1:11", self.api.data)

    def test_retirement_requires_explicit_gate(self):
        with self.assertRaises(SafetyError):
            self.executor(apply=True, phases=("retire",))
        self.assertEqual(self.api.posts, [])

    def test_missing_unavailable_track_blocks_all_retirement(self):
        self.api.omit_last_insert = True
        result = self.run_quiet(self.executor(apply=True, phases=("private", "add", "rename", "retire"), retire_user_approved=True))
        self.assertEqual(result[0]["status"], "blocked")
        self.assertIn("1:11", self.api.data)
        self.assertNotIn("delete", [p[1] for p in self.api.posts])

    def test_applied_timeout_resolves_without_repeating_post(self):
        self.api.timeout_after_apply = True
        result = self.run_quiet(self.executor(apply=True))
        self.assertEqual(result[0]["status"], "verified")
        self.assertEqual(len(self.api.posts), 3)
        self.run_quiet(self.executor(apply=True))
        self.assertEqual(len(self.api.posts), 3)

    def test_unresolved_timeout_stops_and_resume_does_not_retry(self):
        self.api.timeout_before_apply = True
        result = self.run_quiet(self.executor(apply=True))
        self.assertEqual(result[0]["status"], "blocked")
        self.run_quiet(self.executor(apply=True))
        self.assertEqual(len(self.api.posts), 1)
        self.assertIn("1:11", self.api.data)

    def test_changed_source_blocks_retirement_after_successful_append(self):
        self.run_quiet(self.executor(apply=True))
        self.api.data["1:11"]["revision"] += 1
        result = self.run_quiet(self.executor(apply=True, phases=("retire",), retire_user_approved=True))
        self.assertEqual(result[0]["status"], "blocked")
        self.assertIn("1:11", self.api.data)

    def test_backup_corruption_blocks_deletion(self):
        self.run_quiet(self.executor(apply=True))
        path = Path(self.temp.name) / "g01" / "1-11-before.json"
        path.write_text("{}")
        result = self.run_quiet(self.executor(apply=True, phases=("retire",), retire_user_approved=True))
        self.assertEqual(result[0]["status"], "blocked")
        self.assertIn("1:11", self.api.data)

    def test_delete_is_idempotent_after_verified_receipt(self):
        options = dict(apply=True, phases=("private", "add", "rename", "retire"), retire_user_approved=True)
        self.assertEqual(self.run_quiet(self.executor(**options))[0]["status"], "verified")
        self.assertNotIn("1:11", self.api.data)
        before = len(self.api.posts)
        self.assertEqual(self.run_quiet(self.executor(**options))[0]["status"], "verified")
        self.assertEqual(len(self.api.posts), before)

    def test_inferred_source_album_is_retained_with_provenance(self):
        self.api.data["1:11"]["tracks"][0].pop("albumId")
        self.library["playlists"][1]["tracks"][0]["album_id_source"] = "sole_track_album"
        self.assertEqual(self.run_quiet(self.executor(apply=True))[0]["status"], "verified")
        self.assertEqual(self.api.data["1:10"]["tracks"][-1], {"id": "200", "albumId": "10"})

    def test_plan_cannot_drop_identity_or_touch_liked_system(self):
        self.plan["groups"][0]["required_unique_keys"] = ["100:10"]
        with self.assertRaises(SafetyError):
            self.executor()
        self.plan, self.library, self.data = fixture()
        self.library["playlists"][1]["kind"] = "3"
        with self.assertRaises(SafetyError):
            self.executor()


if __name__ == "__main__":
    unittest.main()
