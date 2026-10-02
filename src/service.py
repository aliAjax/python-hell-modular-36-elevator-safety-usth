import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .repository import utcnow
from .rules import (
    ALARM_CLEAR_STATUSES,
    RESCUE_DONE_STATUSES,
    priority_reasons,
    RuleEngine,
)


# Kinds whose state changes can move equipment between blocked and unblocked.
_INCIDENT_KINDS = ("alarm", "rescue_job")
# Reinspection statuses that still occupy a daily plan.
ACTIVE_BOOKING_STATUSES = ("scheduled", "queued")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ------------------------------------------------------------------ create
    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        if kind == "reinspection":
            entity = self.book_reinspection(actor, payload)
        else:
            entity = self._plain_create(actor, kind, payload)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity["id"])
        return entity

    def _plain_create(self, actor, kind, payload):
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        return entity

    # ------------------------------------------------------------ daily plans
    def book_reinspection(self, actor, data):
        """Atomically book a reinspection, re-rank the day and claim/queue a slot."""
        with self.repository.transaction() as tx:
            lookup = lambda kind, field, value: tx.find(self.rules.normalize_kind(kind), field, value)
            payload = self.rules.validate_booking(actor, dict(data or {}), lookup)
            plans = lookup("daily_plan", "id", payload["plan_id"])
            plan = plans[0] if plans else None
            entity_id = str(payload.pop("id", "") or uuid4())
            if tx.get(entity_id):
                raise ConflictError("entity already exists: " + entity_id)
            payload["scheduled_slot"] = None
            payload["priority_reasons"] = priority_reasons(
                {"id": entity_id, "data": payload}, lookup
            )
            booking = tx.insert(entity_id, "reinspection", "queued", payload, actor.user_id)
            tx.audit(entity_id, actor, "create", None, "queued", {"kind": "reinspection"})
            changed = self._rank_plan(tx, plan, actor)
        return self.repository.get_entity(entity_id)

    def rerank_plan(self, actor, plan_id):
        self.rules.assert_role(actor, ("admin", "dispatcher"))
        plan = self.repository.get_entity(plan_id)
        if not plan:
            raise NotFoundError("daily plan not found: " + plan_id)
        self._rerank(plan, actor)

    def _rerank(self, plan, actor):
        """Internal re-rank inside a fresh transaction; role was checked by the caller."""
        with self.repository.transaction() as tx:
            live_plan = tx.get(plan["id"])
            self._rank_plan(tx, live_plan, actor)

    def _rank_plan(self, tx, plan, actor):
        """Apply scheduled/queued assignment for one plan. Returns changed booking ids."""
        lookup = lambda kind, field, value: tx.find(self.rules.normalize_kind(kind), field, value)
        bookings = [
            item for item in tx.list("reinspection")
            if item["data"].get("plan_id") == plan["id"]
        ]
        assignment = self.rules.assign_slots(
            bookings, plan["data"].get("reinspection_capacity", 0), lookup
        )
        changed = []
        for booking in bookings:
            target = assignment.get(booking["id"])
            if not target:
                continue
            data = dict(booking["data"])
            data["scheduled_slot"] = target["slot"]
            data["priority_reasons"] = target["reasons"]
            if booking["status"] != target["status"]:
                updated = tx.apply_status(booking, target["status"], data)
                tx.audit(
                    booking["id"], actor, "rank",
                    booking["status"], updated["status"],
                    {"slot": target["slot"], "plan_id": plan["id"], "reasons": target["reasons"]},
                )
                changed.append(booking["id"])
            else:
                tx.apply_status(booking, booking["status"], data)
        return changed

    def daily_board(self, plan_id):
        plan = self.repository.get_entity(plan_id)
        if not plan or plan["kind"] != "daily_plan":
            raise NotFoundError("daily plan not found: " + plan_id)
        bookings = [
            item for item in self.repository.list_entities("reinspection")
            if item["data"].get("plan_id") == plan["id"]
        ]
        equipment_ids = {item["data"].get("equipment_id") for item in bookings}
        equipment = {item["id"]: item for item in self.repository.list_entities("equipment")}

        alarms = [
            alarm for alarm in self.repository.list_entities("alarm")
            if alarm["data"].get("equipment_id") in equipment_ids
        ]
        alarm_by_id = {alarm["id"]: alarm for alarm in alarms}
        jobs = [
            job for job in self.repository.list_entities("rescue_job")
            if job["data"].get("alarm_id") in alarm_by_id
        ]

        def decorate(booking):
            eq = equipment.get(booking["data"].get("equipment_id"))
            related_alarms = [a for a in alarms if a["data"].get("equipment_id") == eq["id"]] if eq else []
            related_jobs = [j for j in jobs if j["data"].get("alarm_id") in {a["id"] for a in related_alarms}]
            blocked = any(a["status"] not in ALARM_CLEAR_STATUSES for a in related_alarms) or any(
                j["status"] not in RESCUE_DONE_STATUSES for j in related_jobs
            )
            return {
                "reinspection": booking,
                "equipment": eq,
                "blocked_by_open_incident": blocked,
                "alarms": related_alarms,
                "rescue_jobs": related_jobs,
            }

        scheduled = sorted(
            (decorate(b) for b in bookings if b["status"] == "scheduled"),
            key=lambda row: row["reinspection"]["data"].get("scheduled_slot") or 0,
        )
        queued = [decorate(b) for b in bookings if b["status"] == "queued"]
        terminal = [decorate(b) for b in bookings if b["status"] not in ACTIVE_BOOKING_STATUSES]
        return {
            "plan": plan,
            "capacity": plan["data"].get("reinspection_capacity"),
            "used_slots": len(scheduled),
            "scheduled": scheduled,
            "queued": queued,
            "finished": terminal,
            "open_alarms": [a for a in alarms if a["status"] not in ALARM_CLEAR_STATUSES],
            "active_rescue_jobs": [j for j in jobs if j["status"] not in RESCUE_DONE_STATUSES],
        }

    # -------------------------------------------------------------- transition
    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        kind = self.rules.normalize_kind(entity["kind"])

        if kind == "equipment" and action in self.rules.TRANSITIONS.get("equipment", {}):
            return self._equipment_transition(actor, entity, action, data or {}, expected)
        if kind == "reinspection":
            return self._reinspection_transition(actor, entity, action, data or {}, expected)

        updated = self._apply_transition(actor, entity, action, data or {}, expected)
        # Alarm/rescue state changes move equipment between blocked and unblocked.
        if kind in _INCIDENT_KINDS:
            self._rerank_open_plans(actor)
        return updated

    def _apply_transition(self, actor, entity, action, data, expected):
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], expected, next_status, merged)
        self.audit.record(
            entity["id"], actor, action, entity["status"], updated["status"], {"patch": patch}
        )
        return updated

    def _equipment_transition(self, actor, equipment, action, data, expected):
        """Equipment state change voids its active bookings and returns their slots."""
        next_status, patch = self.rules.validate_transition(
            actor, equipment, action, dict(data), self._lookup
        )
        reason = "equipment %s -> %s" % (equipment["id"], next_status)
        with self.repository.transaction() as tx:
            live = tx.get(equipment["id"])
            if expected is not None and live["version"] != int(expected):
                raise ConflictError(
                    "version conflict: expected %s, found %s" % (expected, live["version"])
                )
            merged = dict(live["data"])
            merged.update(patch)
            tx.apply_status(live, next_status, merged)
            tx.audit(live["id"], actor, action, live["status"], next_status, {"patch": patch})

            affected_plans = {}
            for booking in tx.list("reinspection"):
                if (
                    booking["data"].get("equipment_id") == equipment["id"]
                    and booking["status"] in ACTIVE_BOOKING_STATUSES
                ):
                    booking_data = dict(booking["data"])
                    booking_data["void_reason"] = reason
                    booking_data["voided_by"] = actor.user_id
                    booking_data["scheduled_slot"] = None
                    tx.apply_status(booking, "voided", booking_data)
                    tx.audit(
                        booking["id"], actor, "void",
                        booking["status"], "voided",
                        {"reason": reason, "auto": True},
                    )
                    plan = tx.get(booking["data"].get("plan_id"))
                    if plan and plan["status"] == "open":
                        affected_plans[plan["id"]] = plan
            for plan in affected_plans.values():
                self._rank_plan(tx, plan, actor)
        return self.repository.get_entity(equipment["id"])

    def _reinspection_transition(self, actor, booking, action, data, expected):
        next_status, patch = self.rules.validate_transition(
            actor, booking, action, dict(data), self._lookup
        )
        merged = dict(booking["data"])
        merged.update(patch)
        if next_status in ("passed", "failed", "voided"):
            # The slot goes back to the plan and can be claimed by the queue.
            merged["scheduled_slot"] = None
        updated = self.repository.update_entity(booking["id"], expected, next_status, merged)
        self.audit.record(
            booking["id"], actor, action, booking["status"], updated["status"], {"patch": patch}
        )
        self._rerank_open_plans(actor)
        return updated

    def _rerank_open_plans(self, actor):
        """Re-rank every open plan after incidents close or bookings end."""
        for plan in self.repository.list_entities("daily_plan", status="open"):
            self._rerank(plan, actor)

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
