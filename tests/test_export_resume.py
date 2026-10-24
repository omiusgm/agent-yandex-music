import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from account_export import fetch_playlist


class ExportResumeTests(unittest.TestCase):
    def test_cover_and_privacy_refresh_without_revision_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / 'captures').mkdir()
            old = {'detail': {'uid': 1, 'kind': 10, 'revision': 2, 'cover': {'uri': 'old'}, 'visibility': 'public'},
                   'entries': [{'id': '5', 'albumId': 6, 'track': {'id': '5', 'title': 'Synthetic'}}],
                   'audit': {'source_track_list_complete': True}, 'warnings': [],
                   'provenance': {'requests': [], 'fetched_at': 'old', 'reused': False}}
            (out / 'captures/playlist-1-10.json').write_text(json.dumps(old))
            fresh = {'uid': 1, 'kind': 10, 'revision': 2, 'trackCount': 1,
                     'cover': {'uri': 'new'}, 'visibility': 'private', 'description': 'Updated',
                     'tracks': [{'id': '5', 'albumId': 6, 'timestamp': '2026-01-01T00:00:00Z'}]}

            class FakeAPI:
                def __init__(self): self.calls = []
                def fetch(self, path, params):
                    self.calls.append((path, params))
                    return {'result': copy.deepcopy(fresh)}

            api = FakeAPI()
            pid, result = fetch_playlist(api, out, {'uid': 1, 'kind': 10, 'revision': 2}, True)
            self.assertEqual(pid, '1:10')
            self.assertEqual(result['detail']['cover']['uri'], 'new')
            self.assertEqual(result['detail']['visibility'], 'private')
            self.assertEqual(result['entries'][0]['track']['title'], 'Synthetic')
            self.assertEqual(result['entries'][0]['timestamp'], '2026-01-01T00:00:00Z')
            self.assertEqual(len(api.calls), 1)
            self.assertTrue(result['provenance']['reused'])
            self.assertTrue((out / 'raw/playlist-1-10-resume-short.json').exists())


if __name__ == '__main__':
    unittest.main()
