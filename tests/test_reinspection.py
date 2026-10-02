import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import REINSPECTION_DAILY_CAPACITY, RuleEngine
from src.service import DomainService


class ReinspectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.dispatcher = Actor("dispatcher", "dispatcher")
        self.inspector = Actor("inspector", "inspector")
        self.senior = Actor("senior", "senior_inspector")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no, risk="low"):
        return self.service.create(self.admin, "equipment", {
            "asset_no": asset_no, "equipment_type": "elevator", "location": "A",
            "inspection_interval_days": 365, "risk_level": risk,
        })

    def schedule(self, equipment, date="2026-10-02"):
        return self.service.schedule_reinspection(
            self.dispatcher, {"equipment_id": equipment["id"], "scheduled_date": date}
        )

    def test_daily_capacity_queues_overflow(self):
        for i in range(REINSPECTION_DAILY_CAPACITY):
            r = self.schedule(self.equipment("E-%d" % i))
            self.assertEqual(r["status"], "scheduled")
        overflow = self.schedule(self.equipment("E-overflow"))
        self.assertEqual(overflow["status"], "queued")
        self.assertEqual(overflow["data"]["queue_position"], 1)

    def test_high_risk_due_scheduled_first(self):
        high = self.equipment("E-high", "high")
        r = self.schedule(high)
        self.assertEqual(r["status"], "scheduled")
        self.assertEqual(r["data"]["priority"], 0)

    def test_alarm_or_rescue_deprioritized(self):
        high = self.equipment("E-alarm", "high")
        self.service.create(self.admin, "alarm", {
            "equipment_id": high["id"], "code": "DOOR", "occurred_at": "2026-10-01T10:00:00Z",
        })
        r = self.schedule(high)
        self.assertEqual(r["status"], "queued")
        self.assertEqual(r["data"]["priority"], 2)

    def test_unfinished_rescue_deprioritized(self):
        high = self.equipment("E-rescue", "high")
        alarm = self.service.create(self.admin, "alarm", {
            "equipment_id": high["id"], "code": "DOOR", "occurred_at": "2026-10-01T10:00:00Z",
        })
        self.service.create(self.admin, "rescue_job", {
            "alarm_id": alarm["id"], "dedupe_key": "job-1", "team": "Alpha",
        })
        r = self.schedule(high)
        self.assertEqual(r["status"], "queued")
        self.assertEqual(r["data"]["priority"], 2)

    def test_high_risk_bumps_low_risk_when_full(self):
        for i in range(REINSPECTION_DAILY_CAPACITY):
            self.schedule(self.equipment("E-fill-%d" % i))
        high = self.equipment("E-high", "high")
        r = self.schedule(high)
        self.assertEqual(r["status"], "scheduled")
        plan = self.service.daily_plan("2026-10-02")
        self.assertEqual(len(plan["scheduled"]), REINSPECTION_DAILY_CAPACITY)
        self.assertEqual(len(plan["queued"]), 1)

    def test_duplicate_schedule_conflicts_with_occupied_slot(self):
        high = self.equipment("E-high", "high")
        self.schedule(high)
        with self.assertRaises(ConflictError) as ctx:
            self.schedule(high)
        self.assertIn("slot", str(ctx.exception))

    def test_equipment_status_change_voids_and_frees_slot(self):
        high = self.equipment("E-high", "high")
        self.schedule(high)
        for i in range(REINSPECTION_DAILY_CAPACITY - 1):
            self.schedule(self.equipment("E-fill-%d" % i))
        overflow = self.schedule(self.equipment("E-overflow"))
        self.assertEqual(overflow["status"], "queued")
        self.service.transition(self.admin, high["id"], "suspend", {})
        plan = self.service.daily_plan("2026-10-02")
        self.assertEqual(len(plan["scheduled"]), REINSPECTION_DAILY_CAPACITY)
        voided = self.service.repository.list_entities(kind="reinspection", status="voided")
        self.assertTrue(any(v["data"]["equipment_id"] == high["id"] for v in voided))

    def test_permit_frozen_until_reinspection_passes(self):
        equipment = self.equipment("E-perm")
        inspection = self.service.create(self.admin, "inspection", {
            "equipment_id": equipment["id"], "scheduled_at": "2026-09-01T09:00:00Z", "cycle_days": 365,
        })
        self.service.transition(self.admin, inspection["id"], "pass", {"findings": "ok"})
        reinspection = self.schedule(equipment, date="2026-10-05")
        permit = self.service.create(self.admin, "permit", {
            "equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops",
        })
        self.service.transition(self.admin, permit["id"], "request_review", {})
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, permit["id"], "grant", {})
        self.service.transition(self.inspector, reinspection["id"], "pass", {"findings": "ok"})
        permit = self.service.transition(self.admin, permit["id"], "grant", {})
        self.assertEqual(permit["status"], "granted")

    def test_inspector_cannot_pass_high_risk_reinspection(self):
        high = self.equipment("E-high", "high")
        r = self.schedule(high, date="2026-10-03")
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.inspector, r["id"], "pass", {"findings": "ok"})
        r = self.service.transition(self.senior, r["id"], "pass", {"findings": "ok"})
        self.assertEqual(r["status"], "passed")

    def test_alarm_resolve_promotes_reinspection(self):
        high = self.equipment("E-alarm", "high")
        alarm = self.service.create(self.admin, "alarm", {
            "equipment_id": high["id"], "code": "DOOR", "occurred_at": "2026-10-01T10:00:00Z",
        })
        r = self.schedule(high, date="2026-10-08")
        self.assertEqual(r["status"], "queued")
        self.service.transition(self.admin, alarm["id"], "dispatch", {"team": "Alpha"})
        self.service.transition(self.admin, alarm["id"], "resolve", {"resolution": "ok"})
        r = self.service.repository.get_entity(r["id"])
        self.assertEqual(r["status"], "scheduled")
        self.assertEqual(r["data"]["priority"], 0)

    def test_rescue_complete_promotes_reinspection(self):
        high = self.equipment("E-rescue", "high")
        alarm = self.service.create(self.admin, "alarm", {
            "equipment_id": high["id"], "code": "DOOR", "occurred_at": "2026-10-01T10:00:00Z",
        })
        self.service.transition(self.admin, alarm["id"], "dispatch", {"team": "Alpha"})
        job = self.service.create(self.admin, "rescue_job", {
            "alarm_id": alarm["id"], "dedupe_key": "job-1", "team": "Alpha",
        })
        r = self.schedule(high, date="2026-10-09")
        self.assertEqual(r["status"], "queued")
        self.service.transition(self.admin, job["id"], "arrive", {})
        self.service.transition(self.admin, job["id"], "complete", {"outcome": "passenger freed"})
        r = self.service.repository.get_entity(r["id"])
        self.assertEqual(r["status"], "scheduled")

    def test_daily_plan_unified_view(self):
        high = self.equipment("E-high", "high")
        self.schedule(high)
        plan = self.service.daily_plan("2026-10-02")
        self.assertEqual(plan["date"], "2026-10-02")
        self.assertEqual(plan["capacity"], REINSPECTION_DAILY_CAPACITY)
        self.assertEqual(len(plan["scheduled"]), 1)
        self.assertEqual(plan["available_slots"], REINSPECTION_DAILY_CAPACITY - 1)
        self.assertTrue(any(item["equipment_id"] == high["id"] for item in plan["equipment"]))


if __name__ == "__main__":
    unittest.main()
