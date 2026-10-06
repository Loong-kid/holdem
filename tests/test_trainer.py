"""HTTP routing, deploy bundle, and multiplayer regression checks."""
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from server import app, manager, APP_VERSION


class TrainerTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_page_and_every_runtime_asset(self):
        redirect = self.client.get("/trainer", follow_redirects=False)
        self.assertEqual(redirect.status_code, 307)
        response = self.client.get("/trainer/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response.headers["cache-control"])
        self.assertIn("레인지 트레이너", response.text)
        files = re.findall(r'(?:src|href)="([^" ]+\.(?:js|css)\?v=[a-f0-9]+)"', response.text)
        self.assertEqual(len(files), 8)
        manifest = json.loads((ROOT / "trainer/manifest.json").read_text())
        for name, expected in manifest["files"].items():
            asset = self.client.get("/trainer/" + name)
            self.assertEqual(asset.status_code, 200, name)
            self.assertEqual(hashlib.sha256(asset.content).hexdigest(), expected, name)
        for url in files:
            self.assertEqual(self.client.get("/trainer/" + url).status_code, 200, url)
        self.assertEqual(self.client.get("/trainer/server.py").status_code, 404)
        self.assertEqual(self.client.get("/trainer/%2e%2e/server.py").status_code, 404)

    def test_entry_links_and_existing_pages(self):
        root = self.client.get("/")
        self.assertEqual(root.status_code, 200)
        self.assertIn('id="join-trainer-link"', root.text)
        self.assertIn('id="trainer-link"', root.text)
        self.assertEqual(root.text.count('href="/trainer/" target="_blank" rel="noopener"'), 2)
        self.assertEqual(self.client.get("/stats").status_code, 200)
        self.assertEqual(self.client.get("/health").json()["version"], APP_VERSION)

    def test_training_does_not_change_room_state(self):
        room_name = "trainer-regression-test"
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "join", "room": room_name, "name": "TrainerSmoke"})
            joined = ws.receive_json()
            self.assertEqual(joined["type"], "joined")
            self.assertEqual(joined["room"], room_name)
            room = manager.rooms[room_name]
            before = room.game.public_state()
            self.assertEqual(self.client.get("/trainer/").status_code, 200)
            self.assertEqual(self.client.get("/trainer/data/ranges.js").status_code, 200)
            self.assertEqual(room.game.public_state(), before)
        manager.rooms.pop(room_name, None)


if __name__ == "__main__":
    unittest.main()
