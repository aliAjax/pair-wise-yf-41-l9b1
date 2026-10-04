import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.sequences import compute_membership, default_window, haversine_km, select_mainshock
from src.service import DomainService


def _event(event_id, origin_time, magnitude, lat, lon, reports=("S-1", "S-2")):
    return {
        "id": event_id,
        "title": event_id,
        "origin_time": origin_time,
        "location": "Region-X",
        "magnitude": magnitude,
        "lat": lat,
        "lon": lon,
        "reports": [{"station": code, "time_offset": 1, "distance_km": 1.0} for code in reports],
    }


class SequenceRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")
        self.events = {}
        for payload in [
            _event("E-M5", "2026-03-01T00:00:00Z", 5.0, 0.0, 0.0),
            _event("E-M4a", "2026-03-01T02:00:00Z", 4.0, 0.1, 0.0),
            _event("E-M4b", "2026-02-28T00:00:00Z", 4.0, 0.0, 0.0),
            _event("E-M3", "2026-03-01T05:00:00Z", 3.0, 0.2, 0.2),
        ]:
            self.events[payload["id"]] = self.service.create(self.admin, "event", payload)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mainshock_is_largest_magnitude_earliest_time(self):
        # 震级并列时取时间最早
        mainshock = select_mainshock(
            [self.events["E-M4a"], self.events["E-M4b"], self.events["E-M3"]]
        )
        self.assertEqual(mainshock, "E-M4b")
        mainshock = select_mainshock([self.events["E-M4a"], self.events["E-M5"]])
        self.assertEqual(mainshock, "E-M5")

    def test_window_and_distance(self):
        days, distance = default_window(5.2)
        self.assertEqual((days, distance), (83.0, 40.0))
        self.assertLess(haversine_km(0, 0, 0, 1), 112.0)
        self.assertGreater(haversine_km(0, 0, 0, 1), 110.0)

    def test_membership_pure_calculation(self):
        mainshock, window, members = compute_membership(
            {"window_days": 10, "window_distance_km": 60.0},
            list(self.events.values()),
            ["E-M4a", "E-M3"],
        )
        self.assertEqual(mainshock, "E-M4a")
        # 主震之前的 E-M4b 不收（余震窗口在主震之后）
        self.assertIn("E-M3", members)
        self.assertNotIn("E-M4b", members)


class SequenceWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")
        self.analyst_a = Actor("cataloger-A", "analyst")
        self.analyst_b = Actor("cataloger-B", "analyst")
        self.events = {}
        for payload in [
            _event("E1", "2026-03-01T00:00:00Z", 4.0, 0.0, 0.0),
            _event("E2", "2026-03-01T06:00:00Z", 3.0, 0.2, 0.0),   # ~22 km
            _event("E3", "2026-03-02T06:00:00Z", 3.0, 0.5, 0.0),   # ~56 km, 窗外
            _event("E4", "2026-02-28T18:00:00Z", 2.0, -0.2, 0.0),  # 主震前，不收
        ]:
            self.events[payload["id"]] = self.service.create(self.admin, "event", payload)

    def tearDown(self):
        self.tmp.cleanup()

    def _create_sequence(self, actor=None):
        actor = actor or self.analyst_a
        return self.service.create(
            actor,
            "sequence",
            {
                "name": "SEQ-1",
                "anchor_event_ids": ["E1", "E2"],
                "window_days": 10,
                "window_distance_km": 30.0,
            },
        )

    def test_create_sequence_picks_mainshock_and_members(self):
        seq = self._create_sequence()
        self.assertEqual(seq["data"]["mainshock_event_id"], "E1")
        self.assertEqual(seq["data"]["member_count"], 2)
        stored = self.service.get(seq["id"])
        self.assertEqual(stored["data"]["member_event_ids"], ["E1", "E2"])
        # E4 在主震之前，E3 在空间窗口外
        self.assertNotIn("E3", stored["data"]["member_event_ids"])
        self.assertNotIn("E4", stored["data"]["member_event_ids"])

    def test_bigger_event_swaps_mainshock_and_recomputes_members_immediately(self):
        seq = self._create_sequence()
        self.assertEqual(seq["version"], 1)
        # 测到更大的一次，主震更换；新事件在 E3 附近，把原本窗外的 E3 拉进窗口
        big = self.service.create(
            self.admin,
            "event",
            _event("E5", "2026-03-02T00:00:00Z", 6.0, 0.45, 0.0),
        )
        updated = self.service.transition(
            self.analyst_a, seq["id"], "attach", {"event_id": big["id"]},
            expected_version=seq["version"],
        )
        self.assertEqual(updated["data"]["mainshock_event_id"], "E5")
        members = self.service.get(seq["id"])["data"]["member_event_ids"]
        self.assertIn("E3", members)
        # E4 在 E5 之前，仍然移出
        self.assertNotIn("E4", members)

    def test_out_of_window_events_removed_when_window_shrinks(self):
        seq = self._create_sequence()
        # 收紧空间窗口，E2 被移出
        sequence = self.service.get(seq["id"])
        updated, changes = self.service.recompute_sequence(
            self.analyst_a, seq["id"],
            expected_version=sequence["version"],
            patch={"window_distance_km": 5.0},
        )
        self.assertIn("E2", changes["removed"])
        self.assertEqual(updated["data"]["member_count"], 1)
        self.assertEqual(updated["data"]["effective_window"]["distance_km"], 5.0)

    def test_published_notice_is_frozen_and_recompute_becomes_new_version(self):
        seq = self._create_sequence()
        published = self.service.transition(
            self.analyst_a, seq["id"], "publish",
            {"communication_id": "ANN-1"}, expected_version=seq["version"],
        )
        self.assertEqual(published["status"], "frozen")
        notices = self.service.list_notices(seq["id"])
        self.assertEqual([n["data"]["version"] for n in notices], [1])
        first_notice = notices[0]

        # 主震更换后重算：旧公告按原版本冻结，结果作为新版本留存
        big = self.service.create(
            self.admin,
            "event",
            _event("E6", "2026-03-02T00:00:00Z", 5.5, 0.05, 0.0),
        )
        self.service.transition(
            self.analyst_a, seq["id"], "attach", {"event_id": big["id"]},
            expected_version=published["version"],
        )
        notices = self.service.list_notices(seq["id"])
        self.assertEqual([n["data"]["version"] for n in notices], [1, 2])
        # 旧公告内容不变
        self.assertEqual(first_notice["data"]["mainshock_event_id"], "E1")
        self.assertEqual(notices[-1]["data"]["mainshock_event_id"], "E6")
        self.assertEqual(notices[-1]["data"]["communication_id"], "ANN-1")

    def test_two_catalogers_submitting_same_sequence_later_one_sees_conflict_and_mainshock(self):
        first = self._create_sequence(self.analyst_a)
        with self.assertRaises(ConflictError) as ctx:
            self._create_sequence(self.analyst_b)
        self.assertEqual(ctx.exception.details["existing_sequence_id"], first["id"])
        self.assertEqual(ctx.exception.details["mainshock_event_id"], "E1")

        # 乐观锁：A 先挂了新主震，B 拿旧版本号提交自己的 attach 必须冲突并看到当前主震
        self.service.create(
            self.admin, "event", _event("E7", "2026-03-03T00:00:00Z", 5.6, 0.05, 0.0)
        )
        self.service.transition(
            self.analyst_a, first["id"], "attach", {"event_id": "E7"},
            expected_version=first["version"],
        )
        self.service.create(
            self.admin, "event", _event("E8", "2026-03-03T02:00:00Z", 3.6, 0.08, 0.0)
        )
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                self.analyst_b, first["id"], "attach", {"event_id": "E8"},
                expected_version=first["version"],
            )
        self.assertEqual(ctx.exception.details["mainshock_event_id"], "E7")
        self.assertGreater(ctx.exception.details["current_version"], first["version"])

    def test_batch_recompute_failure_leaves_unfinished_items_and_retry_idempotent_on_members(self):
        seq_one = self._create_sequence()
        second = self.service.create(
            self.analyst_b,
            "sequence",
            {"name": "SEQ-2", "anchor_event_ids": ["E2", "E4"],
             "window_days": 10, "window_distance_km": 30.0},
        )
        batch = self.service.recompute_batch(self.admin)
        self.assertEqual(batch["status"], "completed")

        # 让其中一个序列在重算时失败（锚点被清空造成无主震）
        broken = self.service.get(seq_one["id"])
        broken["data"]["anchor_event_ids"] = []
        self.repo.update_entity(broken["id"], broken["version"], broken["status"], broken["data"])

        batch = self.service.recompute_batch(self.admin)
        self.assertEqual(batch["status"], "partial")
        failed_items = [item for item in batch["items"] if item["status"] == "failed"]
        self.assertEqual({item["sequence_id"] for item in failed_items}, {seq_one["id"]})
        good_items = [item for item in batch["items"] if item["status"] == "done"]
        self.assertEqual({item["sequence_id"] for item in good_items}, {second["id"]})

        # 修复坏序列后重试：只有未完成项被处理，已完成的成员不重复改动（无成员审计新增）
        fixed = self.service.get(seq_one["id"])
        fixed["data"]["anchor_event_ids"] = ["E2"]
        self.repo.update_entity(fixed["id"], fixed["version"], fixed["status"], fixed["data"])
        audit_before = len(self.repo.list_audit(second["id"]))
        retried = self.service.retry_recompute_batch(self.admin, batch["id"])
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(
            [item for item in retried["items"] if item["status"] == "failed"], []
        )
        audit_after = len(self.repo.list_audit(second["id"]))
        self.assertEqual(audit_before, audit_after)

    def test_backfill_assigns_legacy_events_by_mainshock_window(self):
        # 独立服务：模拟旧库——有事件、没有任何序列归属
        legacy_repo = SQLiteRepository(Path(self.tmp.name) / "legacy.db")
        legacy = DomainService(legacy_repo, RuleEngine())
        for payload in [
            _event("OLD-1", "2025-06-01T00:00:00Z", 5.0, 30.0, 30.0),
            _event("OLD-2", "2025-06-01T05:00:00Z", 3.0, 30.2, 30.0),
            _event("OLD-3", "2025-06-02T00:00:00Z", 3.0, 45.0, 45.0),
        ]:
            legacy.create(self.admin, "event", payload)
        result = legacy.backfill_sequences(self.admin)
        # OLD-1/OLD-2 同簇成一条序列；OLD-3 远离，独立单点不挂列
        self.assertEqual(len(result["created_sequence_ids"]), 1)
        seq = legacy.get(result["created_sequence_ids"][0])
        self.assertEqual(seq["data"]["mainshock_event_id"], "OLD-1")
        self.assertEqual(
            seq["data"]["member_event_ids"], ["OLD-1", "OLD-2"]
        )
        self.assertTrue(seq["data"]["backfilled"])

        # 已有归属的事件回填时不会再被挂进新序列
        second_run = legacy.backfill_sequences(self.admin)
        self.assertEqual(second_run["created_sequence_ids"], [])

    def test_recompute_without_publish_makes_no_notice_but_frozen_always_versions(self):
        seq = self._create_sequence()
        self.service.recompute_sequence(self.analyst_a, seq["id"], expected_version=1)
        self.assertEqual(self.service.list_notices(seq["id"]), [])

        published = self.service.transition(
            self.analyst_a, seq["id"], "publish",
            {"communication_id": "ANN-9"}, expected_version=2,
        )
        # 主震没变，冻结列重算仍然留下新版本
        updated, _ = self.service.recompute_sequence(
            self.analyst_a, seq["id"], expected_version=published["version"]
        )
        versions = [n["data"]["version"] for n in self.service.list_notices(seq["id"])]
        self.assertEqual(versions, [1, 2])
        self.assertEqual(updated["status"], "frozen")

    def test_backfill_requires_admin(self):
        with self.assertRaises(PermissionDenied):
            self.service.backfill_sequences(self.analyst_a)


if __name__ == "__main__":
    unittest.main()
