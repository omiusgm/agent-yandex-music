"""Offline public workflow in a temporary project with initially empty outputs.

Only synthetic fixtures are used. These commands do not initialize an API client,
read credentials, or connect to a service. No personal outputs are copied.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


class PublicWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="yandex-music-public-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / "scripts", self.root / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "requirements.lock.txt", self.root / "requirements.lock.txt")
        (self.root / "README.md").write_text("# Synthetic workflow test\n", encoding="utf-8")
        outputs = self.root / "outputs"
        outputs.mkdir()
        self.assertEqual(list(outputs.iterdir()), [])
        self.snapshot = outputs / "current-snapshot"
        self.snapshot.mkdir()
        self.source_bytes = (ROOT / "examples" / "library.example.json").read_bytes()
        self.library = json.loads(self.source_bytes)
        self.assertIs(self.library["synthetic"], True)
        (self.snapshot / "library.json").write_bytes(self.source_bytes)
        (self.snapshot / "coverage.json").write_text(json.dumps(self.library["coverage"]), encoding="utf-8")
        lines = ["Synthetic fixture only"]
        for playlist in self.library["playlists"]:
            lines += [playlist["title"], *[entry["key"] for entry in playlist["tracks"]]]
        (self.snapshot / "playlists.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        analysis = outputs / "analysis"
        analysis.mkdir()
        shutil.copy2(ROOT / "examples" / "grouping-draft.example.json", analysis / "grouping-draft.json")

    def command(self, script, *arguments, successful=True):
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run([sys.executable, str(self.root / "scripts" / script), *arguments],
                                   cwd=self.root, env=env, text=True, capture_output=True, timeout=30)
        if successful:
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        else:
            self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return completed

    def load(self, relative):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def test_analyzer_and_compiler_defaults_preserve_identity_and_protect_favorites(self):
        self.command("analyze_library.py")
        summary = self.load("outputs/analysis/summary.json")
        self.assertEqual(summary["counts"]["playlists"], 2)
        self.assertEqual(summary["counts"]["track_entries"], 7)
        self.assertEqual(summary["counts"]["unique_exact_track_album_pairs"], 5)
        self.assertEqual(summary["counts"]["unavailable_entries_retained"], 1)
        self.command("compile_merge_plan.py")
        plan = self.load("outputs/analysis/merge-plan.json")
        self.assertEqual(plan["source_snapshot_sha256"], hashlib.sha256(self.source_bytes).hexdigest())
        self.assertEqual((plan["owned_before"], plan["owned_after"]), (2, 1))
        group = plan["groups"][0]
        self.assertEqual(group["destination"]["playlist_id"], "1:10")
        self.assertEqual(group["destination"]["required_visibility"], "private")
        self.assertEqual(group["add_keys"], ["1003:2003", "1001:2004", "1004:2005"])
        self.assertEqual(group["expected_unique_tracks"], 5)
        self.assertEqual(group["expected_entries_preserving_destination_duplicates"], 6)
        self.assertEqual({s["playlist_id"] for s in group["sources"]}, {"1:10", "1:11"})
        self.assertEqual([s["playlist_id"] for s in group["retire_only_after_verification_and_approval"]], ["1:11"])
        self.assertNotIn("1005:2006", group["required_unique_keys"])
        self.assertEqual(len(group["origins"]["1001:2001"]), 3)

    def test_handoff_keeps_every_ordered_membership_and_excludes_private_files(self):
        self.command("analyze_library.py")
        self.command("compile_merge_plan.py")
        # This marker is not a credential. It detects accidental broad packaging.
        sentinel = "SYNTHETIC_PRIVATE_FILE_MUST_NOT_BE_PACKAGED"
        (self.root / "private").mkdir()
        (self.root / "private" / "example.txt").write_text(sentinel, encoding="utf-8")
        (self.root / ".env").write_text(sentinel, encoding="utf-8")
        self.command("build_handoff.py")
        compact = self.load("outputs/handoff/music-library.json")
        self.assertEqual(compact["schema_version"], "yandex-music-compact/1")
        self.assertEqual(compact["source_snapshot_sha256"], hashlib.sha256(self.source_bytes).hexdigest())
        self.assertEqual(len(compact["playlists"]), 3)
        self.assertEqual(sum(len(p["entries"]) for p in compact["playlists"]), 8)
        for original, exported in zip(self.library["playlists"], compact["playlists"]):
            self.assertEqual(exported["playlist_id"], original["playlist_id"])
            self.assertEqual(exported["collections"], original["collections"])
            self.assertEqual(exported["entries"], [{key: entry[key] for key in ("key", "position", "source_index", "timestamp")}
                                                    for entry in original["tracks"]])
        self.assertEqual(len(compact["tracks_by_key"]), 6)
        self.assertIs(compact["tracks_by_key"]["1002:2002"]["available"], False)
        self.assertIs(compact["tracks_by_key"]["1004:2005"]["metadata_present"], False)
        self.assertEqual(compact["tracks_by_key"]["1004:2005"]["track_id"], "1004")
        self.assertEqual(compact["tracks_by_key"]["1004:2005"]["album_id"], "2005")
        with zipfile.ZipFile(self.root / "outputs/handoff/yandex-music-agent-kit.zip") as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("README.md", archive.namelist())
            self.assertNotIn("analysis/merge-plan.json", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            for row in manifest["files"]:
                data = archive.read(row["path"])
                self.assertEqual(len(data), row["bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), row["sha256"])
            for name in archive.namelist():
                self.assertFalse(name.startswith("private/"))
                self.assertNotEqual(name, ".env")
                self.assertNotIn(sentinel.encode(), archive.read(name))

    def test_handoff_does_not_discover_unrelated_metadata_from_old_outputs(self):
        sentinel = "UNRELATED_SYNTHETIC_ACCOUNT_METADATA"
        for relative in ("outputs/analysis/music-profile.json", "outputs/current-analysis/music-profile.json",
                         "outputs/handoff/before-merge-music-library.json", "outputs/covers/current-covers.json"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"synthetic_marker": sentinel}), encoding="utf-8")
        self.command("build_handoff.py")
        with zipfile.ZipFile(self.root / "outputs/handoff/yandex-music-agent-kit.zip") as archive:
            for name in archive.namelist():
                self.assertFalse(name.startswith(("analysis/", "history/", "covers/")))
                self.assertNotIn(sentinel.encode(), archive.read(name))

    def test_handoff_includes_only_explicit_optional_artifact_paths(self):
        self.command("analyze_library.py")
        self.command("compile_merge_plan.py")
        selected = self.root / "selected-analysis"
        shutil.copytree(self.root / "outputs/analysis", selected)
        (selected / "not-an-allowed-report.json").write_text("{}", encoding="utf-8")
        historical = self.root / "selected-history.json"
        historical.write_bytes(self.source_bytes)
        covers = self.root / "selected-covers.json"
        covers.write_text(json.dumps({"covers": [{"playlist_id": "1:10", "synthetic": True}]}), encoding="utf-8")
        unrelated = self.root / "outputs/current-analysis/music-profile.json"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text('{"synthetic_marker":"WRONG_ACCOUNT"}', encoding="utf-8")
        self.command("build_handoff.py", "--analysis-dir", str(selected), "--history", str(historical), "--covers", str(covers))
        with zipfile.ZipFile(self.root / "outputs/handoff/yandex-music-agent-kit.zip") as archive:
            self.assertEqual(archive.read("analysis/music-profile.json"), (selected / "music-profile.json").read_bytes())
            self.assertEqual(archive.read("analysis/merge-plan.json"), (selected / "merge-plan.json").read_bytes())
            self.assertEqual(archive.read("history/before-merge-music-library.json"), historical.read_bytes())
            self.assertEqual(archive.read("covers/current-covers.json"), covers.read_bytes())
            self.assertNotIn("analysis/not-an-allowed-report.json", archive.namelist())
            for row in json.loads(archive.read("manifest.json"))["files"]:
                self.assertEqual(hashlib.sha256(archive.read(row["path"])).hexdigest(), row["sha256"])

    def test_incomplete_snapshot_is_rejected_before_plan_or_handoff_creation(self):
        self.library["coverage"]["all_account_playlists_exported"] = False
        (self.snapshot / "library.json").write_text(json.dumps(self.library), encoding="utf-8")
        self.command("compile_merge_plan.py", successful=False)
        self.command("build_handoff.py", successful=False)
        self.assertFalse((self.root / "outputs/analysis/merge-plan.json").exists())
        self.assertFalse((self.root / "outputs/handoff/yandex-music-agent-kit.zip").exists())

    def test_grouping_must_map_every_owned_source_once(self):
        grouping = self.load("outputs/analysis/grouping-draft.json")
        for invalid_sources in (["10"], ["10", "10", "11"]):
            with self.subTest(sources=invalid_sources):
                grouping["groups"][0]["source_kinds"] = invalid_sources
                (self.root / "outputs/analysis/grouping-draft.json").write_text(json.dumps(grouping), encoding="utf-8")
                self.command("compile_merge_plan.py", successful=False)
                self.assertFalse((self.root / "outputs/analysis/merge-plan.json").exists())


if __name__ == "__main__":
    unittest.main()
