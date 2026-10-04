import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _make_event(service, actor, label, magnitude, origin_time, lat, lon):
    return service.create(
        actor,
        "event",
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


class FailingRepository(SQLiteRepository):
    """Repository that injects a failure on the Nth sequence batch step."""

    def __init__(self, path, fail_on_step=2):
        super().__init__(path)
        self.fail_on_step = fail_on_step
        self.step_count = 0
        self.applied = []

    def apply_sequence_step(self, sequence_id, expected_version, sequence_data, event_id, event_patch):
        self.step_count += 1
        self.applied.append(event_id)
        if self.fail_on_step is not None and self.step_count == self.fail_on_step:
            raise RuntimeError("injected batch failure")
        return super().apply_sequence_step(
            sequence_id, expected_version, sequence_data, event_id, event_patch
        )


class SequenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_sequence(self, name, mainshock_id, members, max_days=10, max_distance_km=100.0):
        return self.service.create(
            self.actor,
            "sequence",
            {
                "name": name,
                "mainshock_id": mainshock_id,
                "member_ids": members,
                "window": {"max_days": max_days, "max_distance_km": max_distance_km},
            },
        )

    def test_mainshock_is_largest_then_earliest(self):
        m1 = _make_event(self.service, self.actor, "M1", 5.0, "2026-01-01T00:00:00Z", 0.0, 0.0)
        m2 = _make_event(self.service, self.actor, "M2", 5.0, "2026-01-02T00:00:00Z", 0.1, 0.1)
        a = _make_event(self.service, self.actor, "A", 4.0, "2026-01-03T00:00:00Z", 0.2, 0.2)
        seq = self._make_sequence("SEQ-1", m1["id"], [m1["id"], m2["id"], a["id"]])

        updated = self.service.recalculate_sequence(self.actor, seq["id"])
        self.assertEqual(updated["data"]["mainshock_id"], m1["id"])
        self.assertEqual(set(updated["data"]["member_ids"]), {m1["id"], m2["id"], a["id"]})

    def test_mainshock_change_recomputes_members(self):
        m = _make_event(self.service, self.actor, "M", 5.0, "2026-01-01T00:00:00Z", 0.0, 0.0)
        a = _make_event(self.service, self.actor, "A", 4.0, "2026-01-02T00:00:00Z", 0.1, 0.1)
        b = _make_event(self.service, self.actor, "B", 3.0, "2026-01-03T00:00:00Z", 0.2, 0.2)
        f = _make_event(self.service, self.actor, "F", 2.0, "2026-01-02T00:00:00Z", 10.0, 10.0)
        seq = self._make_sequence("SEQ-2", m["id"], [m["id"], a["id"], b["id"], f["id"]])

        updated = self.service.recalculate_sequence(self.actor, seq["id"])
        self.assertEqual(updated["data"]["mainshock_id"], m["id"])
        self.assertNotIn(f["id"], updated["data"]["member_ids"])
        for member in (m["id"], a["id"], b["id"]):
            self.assertIn(member, updated["data"]["member_ids"])

        # A bigger event inside the window becomes the new mainshock and
        # re-centers the window; the far event stays out.
        g = _make_event(self.service, self.actor, "G", 6.0, "2026-01-04T00:00:00Z", 0.05, 0.05)
        updated = self.service.get(seq["id"])
        self.assertEqual(updated["data"]["mainshock_id"], g["id"])
        self.assertIn(g["id"], updated["data"]["member_ids"])
        self.assertNotIn(f["id"], updated["data"]["member_ids"])

    def test_published_announcements_are_frozen(self):
        m = _make_event(self.service, self.actor, "M", 5.0, "2026-01-01T00:00:00Z", 0.0, 0.0)
        a = _make_event(self.service, self.actor, "A", 4.0, "2026-01-02T00:00:00Z", 0.1, 0.1)
        seq = self._make_sequence("SEQ-3", m["id"], [m["id"], a["id"]])
        self.service.recalculate_sequence(self.actor, seq["id"])

        announced = self.service.publish_sequence_announcement(self.actor, seq["id"], "first")
        v1 = announced["data"]["announcements"][0]
        self.assertEqual(v1["version"], 1)
        self.assertEqual(v1["mainshock_id"], m["id"])
        self.assertEqual(set(v1["member_ids"]), {m["id"], a["id"]})

        # Recalculate after a bigger event; the working state changes but the
        # published announcement stays frozen at the original version.
        g = _make_event(self.service, self.actor, "G", 6.0, "2026-01-03T00:00:00Z", 0.05, 0.05)
        updated = self.service.get(seq["id"])
        self.assertEqual(updated["data"]["mainshock_id"], g["id"])
        frozen = updated["data"]["announcements"][0]
        self.assertEqual(frozen["mainshock_id"], m["id"])
        self.assertEqual(set(frozen["member_ids"]), {m["id"], a["id"]})

        # A new announcement is a new version, not a rewrite of the old one.
        announced2 = self.service.publish_sequence_announcement(self.actor, seq["id"], "second")
        self.assertEqual(len(announced2["data"]["announcements"]), 2)
        self.assertEqual(announced2["data"]["announcements"][0]["mainshock_id"], m["id"])
        self.assertEqual(announced2["data"]["announcements"][1]["mainshock_id"], g["id"])

    def test_concurrent_submitter_sees_conflict_and_current_mainshock(self):
        m = _make_event(self.service, self.actor, "M", 5.0, "2026-01-01T00:00:00Z", 0.0, 0.0)
        a = _make_event(self.service, self.actor, "A", 4.0, "2026-01-02T00:00:00Z", 0.1, 0.1)
        seq = self._make_sequence("SEQ-4", m["id"], [m["id"], a["id"]])
        self.service.recalculate_sequence(self.actor, seq["id"])

        s1 = self.service.get(seq["id"])
        s2 = self.service.get(seq["id"])
        self.service.recalculate_sequence(self.actor, s1["id"], expected_version=s1["version"])

        with self.assertRaises(ConflictError) as ctx:
            self.service.recalculate_sequence(self.actor, s2["id"], expected_version=s2["version"])
        message = str(ctx.exception)
        self.assertIn("version conflict", message)
        self.assertIn("current mainshock: %s" % m["id"], message)

    def test_batch_failure_leaves_unfinished_batch_for_retry(self):
        m = _make_event(self.service, self.actor, "M", 5.0, "2026-01-01T00:00:00Z", 0.0, 0.0)
        a = _make_event(self.service, self.actor, "A", 4.0, "2026-01-02T00:00:00Z", 0.1, 0.1)
        b = _make_event(self.service, self.actor, "B", 3.0, "2026-01-03T00:00:00Z", 0.2, 0.2)
        c = _make_event(self.service, self.actor, "C", 2.0, "2026-01-04T00:00:00Z", 0.3, 0.3)

        failing = FailingRepository(Path(self.tmp.name) / "test.db", fail_on_step=2)
        service = DomainService(failing, RuleEngine())
        seq = service.create(
            self.actor,
            "sequence",
            {
                "name": "SEQ-5",
                "mainshock_id": m["id"],
                "member_ids": [],
                "window": {"max_days": 10, "max_distance_km": 100.0},
            },
        )

        with self.assertRaises(RuntimeError):
            service.recalculate_sequence(self.actor, seq["id"])

        after_fail = service.get(seq["id"])
        batch = after_fail["data"]["batch"]
        self.assertEqual(batch["status"], "in_progress")
        self.assertEqual(len(batch["processed"]), 1)

        # Retry: the already-processed member is not modified again.
        failing.fail_on_step = None
        updated = service.recalculate_sequence(self.actor, seq["id"])
        final_batch = updated["data"]["batch"]
        self.assertEqual(final_batch["status"], "done")
        self.assertEqual(len(final_batch["processed"]), 4)
        self.assertEqual(failing.applied.count(failing.applied[0]), 1)
        for member in (m["id"], a["id"], b["id"], c["id"]):
            self.assertIn(member, updated["data"]["member_ids"])
            self.assertEqual(service.get(member)["data"]["sequence_id"], seq["id"])

    def test_backfill_assigns_unattributed_events(self):
        m = _make_event(self.service, self.actor, "M", 5.0, "2026-01-01T00:00:00Z", 0.0, 0.0)
        a = _make_event(self.service, self.actor, "A", 4.0, "2026-01-02T00:00:00Z", 0.1, 0.1)
        b = _make_event(self.service, self.actor, "B", 3.0, "2026-01-03T00:00:00Z", 0.2, 0.2)
        f = _make_event(self.service, self.actor, "F", 2.0, "2026-01-02T00:00:00Z", 10.0, 10.0)
        seq = self._make_sequence("SEQ-6", m["id"], [])

        result = self.service.backfill_sequences(self.actor)
        self.assertEqual(result["assigned"], 3)
        for member in (m["id"], a["id"], b["id"]):
            self.assertEqual(self.service.get(member)["data"]["sequence_id"], seq["id"])
        self.assertIsNone(self.service.get(f["id"])["data"].get("sequence_id"))

        # Re-running backfill is a no-op: already-attributed events are untouched.
        result2 = self.service.backfill_sequences(self.actor)
        self.assertEqual(result2["assigned"], 0)


if __name__ == "__main__":
    unittest.main()
