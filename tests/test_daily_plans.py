import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


PLAN_DAY = "2026-10-02"


class DailyPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.dispatcher_a = Actor("dispatch-a", "dispatcher")
        self.dispatcher_b = Actor("dispatch-b", "dispatcher")
        self.inspector = Actor("insp-1", "inspector")
        self.senior = Actor("senior-1", "senior_inspector")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no, risk="low", actor=None):
        return self.service.create(
            actor or self.admin,
            "equipment",
            {
                "asset_no": asset_no,
                "equipment_type": "elevator",
                "location": "Tower",
                "inspection_interval_days": 365,
                "risk_level": risk,
            },
        )

    def plan(self, capacity=2, day=PLAN_DAY, actor=None):
        return self.service.create(
            actor or self.admin,
            "daily_plan",
            {"plan_date": day, "reinspection_capacity": capacity},
        )

    def book(self, plan_id, equipment_id, actor=None):
        return self.service.create(
            actor or self.dispatcher_a,
            "reinspection",
            {"plan_id": plan_id, "equipment_id": equipment_id},
        )

    def statuses(self, plan_id):
        board = self.service.daily_board(plan_id)
        scheduled = [row["reinspection"]["data"]["equipment_id"] for row in board["scheduled"]]
        queued = [row["reinspection"]["data"]["equipment_id"] for row in board["queued"]]
        return scheduled, queued, board

    def test_capacity_fills_and_low_risk_waits_in_queue(self):
        plan = self.plan(capacity=2)
        low1 = self.equipment("L-1", "low")
        low2 = self.equipment("L-2", "low")
        low3 = self.equipment("L-3", "low")
        self.book(plan["id"], low1["id"])
        self.book(plan["id"], low2["id"])
        third = self.book(plan["id"], low3["id"])
        self.assertEqual(third["status"], "queued")
        scheduled, queued, board = self.statuses(plan["id"])
        self.assertEqual(len(scheduled), 2)
        self.assertEqual(queued, [low3["id"]])
        self.assertEqual(board["used_slots"], 2)

    def test_high_risk_and_overdue_jump_ahead(self):
        plan = self.plan(capacity=1)
        # Book a plain low-risk elevator first, it takes the only slot.
        low = self.equipment("LOW", "low")
        self.book(plan["id"], low["id"])
        # A high-risk elevator arrives later and must claim the slot.
        high = self.equipment("HIGH", "high")
        self.book(plan["id"], high["id"])
        scheduled, queued, board = self.statuses(plan["id"])
        self.assertEqual(scheduled, [high["id"]])
        self.assertEqual(queued, [low["id"]])
        slot = board["scheduled"][0]["reinspection"]["data"]
        self.assertIn("high_risk_equipment", slot["priority_reasons"])

    def test_open_alarm_and_rescue_are_deferred(self):
        plan = self.plan(capacity=1)
        free_equipment = self.equipment("FREE", "low")
        trapped_equipment = self.equipment("TRAP", "high")  # higher risk, but blocked
        alarm = self.service.create(
            self.dispatcher_a,
            "alarm",
            {"equipment_id": trapped_equipment["id"], "code": "ENTRAP", "occurred_at": "2026-10-02T08:00:00Z"},
        )
        self.book(plan["id"], free_equipment["id"])
        blocked_booking = self.book(plan["id"], trapped_equipment["id"])
        # Even though high risk, unresolved alarm pushes it behind the free elevator.
        scheduled, queued, board = self.statuses(plan["id"])
        self.assertEqual(scheduled, [free_equipment["id"]])
        self.assertEqual(queued, [trapped_equipment["id"]])
        self.assertTrue(board["queued"][0]["blocked_by_open_incident"])
        self.assertIn("open_alarm_or_rescue", blocked_booking["data"]["priority_reasons"])

        # Dispatch a rescue; still blocked until the rescue is finished.
        self.service.transition(self.dispatcher_a, alarm["id"], "dispatch", {"team": "Alpha"})
        job = self.service.create(
            self.dispatcher_a,
            "rescue_job",
            {"alarm_id": alarm["id"], "dedupe_key": "job-1", "team": "Alpha"},
        )
        scheduled, queued, _ = self.statuses(plan["id"])
        self.assertEqual(scheduled, [free_equipment["id"]])

        # Complete the rescue and resolve/close the alarm: high-risk now takes the slot.
        self.service.transition(self.dispatcher_a, job["id"], "arrive", {})
        self.service.transition(self.dispatcher_a, job["id"], "complete", {"outcome": "freed"})
        self.service.transition(self.dispatcher_a, alarm["id"], "resolve", {"resolution": "safe"})
        self.service.transition(self.dispatcher_a, alarm["id"], "close", {})
        scheduled, queued, board = self.statuses(plan["id"])
        self.assertEqual(scheduled, [trapped_equipment["id"]])
        self.assertEqual(queued, [free_equipment["id"]])

    def test_double_submit_same_equipment_conflicts_and_shows_taken_slot(self):
        plan = self.plan(capacity=5)
        equipment = self.equipment("DUP", "medium")
        first = self.book(plan["id"], equipment["id"], self.dispatcher_a)
        self.assertEqual(first["status"], "scheduled")
        with self.assertRaises(ConflictError) as context:
            self.book(plan["id"], equipment["id"], self.dispatcher_b)
        message = str(context.exception)
        self.assertIn(first["id"], message)
        self.assertIn("scheduled", message)

    def test_concurrent_dispatchers_one_wins_one_sees_conflict(self):
        plan = self.plan(capacity=5)
        equipment = self.equipment("RACE", "medium")
        errors = []

        def submit(actor):
            try:
                self.book(plan["id"], equipment["id"], actor)
            except ConflictError as exc:
                errors.append(str(exc))
            except Exception as exc:  # pragma: no cover - surface unexpected failures
                errors.append("UNEXPECTED:" + str(exc))

        threads = [
            threading.Thread(target=submit, args=(self.dispatcher_a,)),
            threading.Thread(target=submit, args=(self.dispatcher_b,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(errors), 1)
        board = self.service.daily_board(plan["id"])
        self.assertEqual(board["used_slots"], 1)

    def test_concurrent_capacity_is_not_oversold(self):
        plan = self.plan(capacity=3)
        equipments = [self.equipment("C-%02d" % index, "low") for index in range(10)]
        barriers = {"start": threading.Event()}

        def submit(equipment, actor):
            barriers["start"].wait()
            self.book(plan["id"], equipment["id"], actor)

        threads = [
            threading.Thread(target=submit, args=(equipments[index], Actor("d-%d" % index, "dispatcher")))
            for index in range(10)
        ]
        for thread in threads:
            thread.start()
        barriers["start"].set()
        for thread in threads:
            thread.join()

        scheduled, queued, board = self.statuses(plan["id"])
        self.assertEqual(board["used_slots"], 3)
        self.assertEqual(len(scheduled), 3)
        self.assertEqual(len(queued), 7)

    def test_equipment_status_change_voids_booking_and_returns_slot(self):
        plan = self.plan(capacity=1)
        first = self.equipment("VOID", "high")
        second = self.equipment("WAIT", "low")
        booked = self.book(plan["id"], first["id"])
        waiting = self.book(plan["id"], second["id"])
        self.assertEqual(booked["status"], "scheduled")
        self.assertEqual(waiting["status"], "queued")

        # Equipment status changes (suspension): booking is voided, queue moves up.
        self.service.transition(self.admin, first["id"], "suspend", {})
        board = self.service.daily_board(plan["id"])
        voided = [row for row in board["finished"] if row["reinspection"]["id"] == booked["id"]][0]
        self.assertEqual(voided["reinspection"]["status"], "voided")
        void_record = [
            entry for entry in self.service.audit_log(booked["id"]) if entry["action"] == "void"
        ][-1]
        self.assertTrue(void_record["detail"].get("auto"))
        scheduled, queued, _ = self.statuses(plan["id"])
        self.assertEqual(scheduled, [second["id"]])
        self.assertEqual(queued, [])

    def test_finished_booking_frees_slot_for_queue(self):
        plan = self.plan(capacity=1)
        first = self.equipment("PASS", "low")
        second = self.equipment("NEXT", "low")
        booked = self.book(plan["id"], first["id"])
        self.book(plan["id"], second["id"])
        booked = self.service.get(booked["id"])
        self.assertEqual(booked["status"], "scheduled")
        passed = self.service.transition(self.inspector, booked["id"], "pass", {"findings": "ok"})
        self.assertEqual(passed["status"], "passed")
        scheduled, queued, _ = self.statuses(plan["id"])
        self.assertEqual(scheduled, [second["id"]])

    def test_high_risk_requires_senior_inspector(self):
        plan = self.plan(capacity=2)
        high = self.equipment("RISK", "high")
        low = self.equipment("SAFE", "low")
        high_booking = self.book(plan["id"], high["id"])
        low_booking = self.book(plan["id"], low["id"])
        high_booking = self.service.get(high_booking["id"])
        low_booking = self.service.get(low_booking["id"])

        with self.assertRaises(PermissionDenied):
            self.service.transition(self.inspector, high_booking["id"], "pass", {"findings": "ok"})
        # A normal inspector can still sign off low-risk elevators.
        low_passed = self.service.transition(self.inspector, low_booking["id"], "pass", {"findings": "ok"})
        self.assertEqual(low_passed["status"], "passed")
        # The senior inspector can sign off the high-risk one.
        high_passed = self.service.transition(self.senior, high_booking["id"], "pass", {"findings": "ok"})
        self.assertEqual(high_passed["status"], "passed")

    def test_permit_stays_frozen_until_reinspection_passes(self):
        plan = self.plan(capacity=2)
        equipment = self.equipment("FRZ", "high")
        booking = self.book(plan["id"], equipment["id"])
        permit = self.service.create(
            self.admin,
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        # Pending reinspection keeps the permit frozen at request time.
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, permit["id"], "request_review", {})

        # A failed reinspection keeps it frozen as well.
        self.service.transition(self.senior, booking["id"], "fail", {"findings": "bad"})
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, permit["id"], "request_review", {})

        # Re-book and pass; only then can the permit proceed.
        second = self.book(plan["id"], equipment["id"])
        self.service.transition(self.senior, second["id"], "pass", {"findings": "fixed"})
        reviewed = self.service.transition(self.admin, permit["id"], "request_review", {})
        granted = self.service.transition(self.admin, reviewed["id"], "grant", {})
        self.assertEqual(granted["status"], "granted")

    def test_inspector_cannot_create_daily_plan_or_booking(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.inspector,
                "daily_plan",
                {"plan_date": PLAN_DAY, "reinspection_capacity": 1},
            )

    def test_overdue_inspection_outranks_future_due(self):
        plan = self.plan(capacity=1)
        overdue = self.equipment("OVD", "low")
        fresh = self.equipment("FRS", "low")
        # overdue: inspection passed long ago, cycle already expired
        old = self.service.create(
            self.admin, "inspection",
            {"equipment_id": overdue["id"], "scheduled_at": "2024-09-01T09:00:00Z", "cycle_days": 365},
        )
        self.service.transition(self.admin, old["id"], "pass", {"findings": "ok"})
        # fresh: inspection is still within cycle
        new = self.service.create(
            self.admin, "inspection",
            {"equipment_id": fresh["id"], "scheduled_at": "2026-09-15T09:00:00Z", "cycle_days": 365},
        )
        self.service.transition(self.admin, new["id"], "pass", {"findings": "ok"})

        self.book(plan["id"], fresh["id"])
        self.book(plan["id"], overdue["id"])
        scheduled, queued, board = self.statuses(plan["id"])
        self.assertEqual(scheduled, [overdue["id"]])
        self.assertIn("inspection_overdue", board["scheduled"][0]["reinspection"]["data"]["priority_reasons"])


if __name__ == "__main__":
    unittest.main()
