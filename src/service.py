import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import REINSPECTION_DAILY_CAPACITY, RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind == "reinspection":
            return self.schedule_reinspection(actor, data, idempotency_key)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def schedule_reinspection(self, actor, data, idempotency_key=None):
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, "reinspection", payload, self._lookup)
        equipment = self._lookup("equipment", "id", payload["equipment_id"])[0]
        scheduled_date = self.rules.parse_date(payload["scheduled_date"])
        factors = self.rules.priority_factors(equipment, scheduled_date, self._lookup)
        payload["priority"] = factors["priority"]
        payload["risk_level"] = equipment["data"].get("risk_level", "low")
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        entity = self.repository.schedule_reinspection(
            entity_id, payload, actor.user_id, REINSPECTION_DAILY_CAPACITY
        )
        self.audit.record(entity_id, actor, "schedule", None, entity["status"], {
            "kind": "reinspection",
            "scheduled_date": payload["scheduled_date"],
            "priority": factors["priority"],
            "factors": factors,
        })
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def daily_plan(self, date):
        scheduled_date = self.rules.parse_date(date)
        if scheduled_date is None:
            raise ValidationError("date must be ISO-8601 date")
        scheduled_date = scheduled_date.isoformat()
        reinspections = self.repository.list_entities(kind="reinspection")
        day_items = [r for r in reinspections if r["data"].get("scheduled_date") == scheduled_date]
        scheduled = [r for r in day_items if r["status"] == "scheduled"]
        queued = [r for r in day_items if r["status"] == "queued"]
        equipment_plan = []
        for equipment in self.repository.list_entities(kind="equipment"):
            factors = self.rules.priority_factors(equipment, scheduled_date, self._lookup)
            equipment_plan.append({
                "equipment_id": equipment["id"],
                "asset_no": equipment["data"].get("asset_no"),
                "location": equipment["data"].get("location"),
                "risk_level": equipment["data"].get("risk_level", "low"),
                "status": equipment["status"],
                "factors": factors,
            })
        equipment_plan.sort(key=lambda item: (item["factors"]["priority"], str(item["asset_no"])))
        return {
            "date": scheduled_date,
            "capacity": REINSPECTION_DAILY_CAPACITY,
            "available_slots": max(0, REINSPECTION_DAILY_CAPACITY - len(scheduled)),
            "scheduled": scheduled,
            "queued": queued,
            "equipment": equipment_plan,
        }

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if self.rules.normalize_kind(entity["kind"]) == "equipment":
            voided = self.repository.void_reinspections_for_equipment(entity_id)
            if voided:
                self.audit.record(
                    entity_id,
                    actor,
                    "void_reinspections",
                    entity["status"],
                    updated["status"],
                    {"voided_reinspections": voided},
                )
        elif self.rules.normalize_kind(entity["kind"]) == "alarm":
            if action in ("resolve", "close", "mark_false"):
                self._refresh_reinspection_for_equipment(entity["data"].get("equipment_id"))
        elif self.rules.normalize_kind(entity["kind"]) == "rescue_job":
            if action in ("complete", "abort"):
                alarm = self._lookup("alarm", "id", entity["data"].get("alarm_id"))
                if alarm:
                    self._refresh_reinspection_for_equipment(alarm[0]["data"].get("equipment_id"))
        return updated

    def _refresh_reinspection_for_equipment(self, equipment_id):
        if not equipment_id:
            return
        equipment = self._lookup("equipment", "id", equipment_id)
        if not equipment:
            return
        queued = [
            r for r in self.repository.list_entities(kind="reinspection")
            if r["data"].get("equipment_id") == equipment_id and r["status"] == "queued"
        ]
        affected_dates = set()
        for item in queued:
            scheduled_date = self.rules.parse_date(item["data"].get("scheduled_date"))
            factors = self.rules.priority_factors(equipment[0], scheduled_date, self._lookup)
            merged = dict(item["data"])
            merged["priority"] = factors["priority"]
            self.repository.update_entity(item["id"], item["version"], "queued", merged)
            affected_dates.add(item["data"].get("scheduled_date"))
        for scheduled_date in affected_dates:
            if scheduled_date:
                self.repository.promote_queued_reinspections(scheduled_date, REINSPECTION_DAILY_CAPACITY)

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
