import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HttpSequenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), "static")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port)
        headers = {"X-User-Id": "admin", "X-Role": "admin", "Content-Type": "application/json"}
        conn.request(method, path, body=json.dumps(body) if body else None, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def _make_event(self, label, magnitude, origin_time, lat, lon):
        status, data = self._request(
            "POST",
            "/api/event",
            {
                "title": "Event-%s" % label,
                "origin_time": origin_time,
                "location": "Region",
                "lat": lat,
                "lon": lon,
                "magnitude": magnitude,
                "reports": [
                    {"station": "STA-1", "time_offset": 1, "distance_km": 1.0},
                    {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
                ],
            },
        )
        self.assertEqual(status, 201)
        return data["id"]

    def test_sequence_http_flow(self):
        m = self._make_event("M", 5.0, "2026-01-01T00:00:00Z", 0.0, 0.0)
        a = self._make_event("A", 4.0, "2026-01-02T00:00:00Z", 0.1, 0.1)

        status, seq = self._request(
            "POST",
            "/api/sequence",
            {
                "name": "SEQ-HTTP",
                "mainshock_id": m,
                "member_ids": [m],
                "window": {"max_days": 10, "max_distance_km": 100.0},
            },
        )
        self.assertEqual(status, 201)
        seq_id = seq["id"]

        # Recalculate via the actions endpoint.
        status, data = self._request(
            "POST", "/api/sequence/%s/actions" % seq_id, {"action": "recalculate"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["mainshock_id"], m)
        self.assertIn(a, data["data"]["member_ids"])

        # Publish an announcement.
        status, data = self._request(
            "POST",
            "/api/sequence/%s/actions" % seq_id,
            {"action": "publish_announcement", "data": {"note": "v1"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(data["data"]["announcements"]), 1)

        # A bigger event triggers auto-recalc; the announcement stays frozen.
        g = self._make_event("G", 6.0, "2026-01-03T00:00:00Z", 0.05, 0.05)
        status, data = self._request("GET", "/api/sequence/%s" % seq_id)
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["mainshock_id"], g)
        self.assertEqual(data["data"]["announcements"][0]["mainshock_id"], m)

        # A stale version yields a 409 with the current mainshock.
        status, data = self._request(
            "POST",
            "/api/sequence/%s/actions" % seq_id,
            {"action": "recalculate", "expected_version": 1},
        )
        self.assertEqual(status, 409)
        self.assertIn("current mainshock", data["error"])

        # Backfill endpoint.
        status, data = self._request("POST", "/api/sequences/backfill")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["assigned"], 0)


if __name__ == "__main__":
    unittest.main()
