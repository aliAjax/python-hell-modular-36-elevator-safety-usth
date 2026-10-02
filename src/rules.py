from datetime import date, datetime, timedelta

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError

RISK_LEVELS = ("low", "medium", "high")
RISK_RANK = {"high": 0, "medium": 1, "low": 2}
SIGN_OFF_ROLES = ("admin", "inspector", "senior_inspector")
# Statuses that mean the situation is over and no longer block a reinspection slot.
ALARM_CLEAR_STATUSES = ("closed", "false_alarm")
RESCUE_DONE_STATUSES = ("completed", "aborted")


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _positive(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")
    if number <= 0:
        raise ValidationError(field + " must be positive")
    return number


def _risk_of(equipment):
    risk = equipment["data"].get("risk_level", "low")
    return risk if risk in RISK_LEVELS else "low"


def _parse_day(value):
    text = str(value).strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        raise ValidationError("date must be ISO-8601 (YYYY-MM-DD)")


def _inspection_due(equipment, lookup):
    """Return the due date of the current inspection cycle, or None when never inspected."""
    passed = [
        item
        for item in _all(lookup, "inspection")
        if item["data"].get("equipment_id") == equipment["id"] and item["status"] == "passed"
    ]
    if not passed:
        return None
    latest = max(
        passed,
        key=lambda item: str(item["data"].get("scheduled_at") or item["data"].get("passed_at") or ""),
    )
    base_text = str(latest["data"].get("scheduled_at") or latest["data"].get("passed_at"))
    try:
        base = datetime.fromisoformat(base_text.replace("Z", "+00:00")).date()
    except ValueError:
        return None
    try:
        cycle = int(float(latest["data"].get("cycle_days") or equipment["data"].get("inspection_interval_days") or 365))
    except (TypeError, ValueError):
        cycle = 365
    return base + timedelta(days=cycle)


def _has_open_incident(equipment_id, lookup):
    """True while an alarm is not cleared or a rescue job is not finished."""
    alarm_ids = set()
    blocked = False
    for alarm in _all(lookup, "alarm"):
        if alarm["data"].get("equipment_id") == equipment_id:
            if alarm["status"] not in ALARM_CLEAR_STATUSES:
                blocked = True
            alarm_ids.add(alarm["id"])
    for job in _all(lookup, "rescue_job"):
        if job["data"].get("alarm_id") in alarm_ids and job["status"] not in RESCUE_DONE_STATUSES:
            blocked = True
    return blocked


def reinspection_priority(reinspection, lookup):
    """
    Sort key (lower value = earlier slot):
    1. pending alarm/rescue goes after everything else;
    2. high risk before medium before low;
    3. inspection overdue before due-soon before not-due before never inspected;
    4. earlier due date first;
    5. earlier booking first (created_at, id).
    """
    equipment = _find_one(lookup, "equipment", "id", reinspection["data"].get("equipment_id"))
    risk = _risk_of(equipment) if equipment else "low"
    today = date.fromisoformat(str(reinspection["data"].get("plan_date")))
    due = _inspection_due(equipment, lookup) if equipment else None
    if due is None:
        due_rank, due_key = 3, date.max
    elif due < today:
        due_rank, due_key = 0, due
    elif due == today:
        due_rank, due_key = 1, due
    else:
        due_rank, due_key = 2, due
    blocked = 1 if _has_open_incident(reinspection["data"].get("equipment_id"), lookup) else 0
    return (
        blocked,
        RISK_RANK[risk],
        due_rank,
        due_key.isoformat(),
        str(reinspection.get("created_at") or ""),
        str(reinspection.get("id") or ""),
    )


def priority_reasons(reinspection, lookup):
    """Snapshot explaining why the booking got its slot/queue position."""
    equipment = _find_one(lookup, "equipment", "id", reinspection["data"].get("equipment_id"))
    reasons = []
    if equipment:
        risk = _risk_of(equipment)
        if risk == "high":
            reasons.append("high_risk_equipment")
        due = _inspection_due(equipment, lookup)
        if due is None:
            reasons.append("never_passed_inspection")
        else:
            today = date.fromisoformat(str(reinspection["data"].get("plan_date")))
            if due < today:
                reasons.append("inspection_overdue")
            elif due == today:
                reasons.append("inspection_due_today")
    if _has_open_incident(reinspection["data"].get("equipment_id"), lookup):
        reasons.append("open_alarm_or_rescue")
    return reasons


def _validate_equipment(data, lookup):
    asset_no = str(data.get("asset_no", "")).strip()
    if not asset_no:
        raise ValidationError("asset_no is required")
    if _find_one(lookup, "equipment", "asset_no", asset_no):
        raise ConflictError("equipment asset_no already exists: " + asset_no)
    _positive(data.get("inspection_interval_days"), "inspection_interval_days")
    risk = data.get("risk_level", "low")
    if risk not in RISK_LEVELS:
        raise ValidationError("risk_level must be one of: " + ", ".join(RISK_LEVELS))


def _validate_inspection(data, lookup):
    equipment = _find_one(lookup, "equipment", "id", data.get("equipment_id"))
    if not equipment:
        raise ValidationError("inspection requires equipment")
    try:
        datetime.fromisoformat(str(data.get("scheduled_at")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("scheduled_at must be ISO-8601")
    _positive(data.get("cycle_days"), "cycle_days")


def _validate_maintenance(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("maintenance requires equipment")
    if data.get("work_type") not in ("routine", "repair", "component_replacement", "modernization"):
        raise ValidationError("invalid work_type")
    if data.get("work_type") == "component_replacement" and not data.get("part_serial"):
        raise ValidationError("part_serial is required for component replacement")


def _validate_alarm(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("alarm requires equipment")
    for alarm in _all(lookup, "alarm"):
        if (
            alarm["data"].get("equipment_id") == data.get("equipment_id")
            and alarm["data"].get("code") == data.get("code")
            and alarm["status"] not in ("closed", "false_alarm")
        ):
            raise ConflictError("active alarm already exists for equipment and code")


def _validate_rescue(data, lookup):
    alarm = _find_one(lookup, "alarm", "id", data.get("alarm_id"))
    if not alarm or alarm["status"] == "closed":
        raise ValidationError("rescue_job requires an active alarm")
    key = data.get("dedupe_key")
    for job in _all(lookup, "rescue_job"):
        if job["data"].get("dedupe_key") == key and job["status"] not in ("completed", "aborted"):
            raise ConflictError("active rescue job already exists for dedupe_key")


def _validate_remediation(data, lookup):
    if not data.get("equipment_id") and not data.get("alarm_id"):
        raise ValidationError("remediation requires equipment_id or alarm_id")
    issue = str(data.get("issue", "")).strip()
    for item in _all(lookup, "remediation"):
        if item["data"].get("equipment_id") == data.get("equipment_id") and item["data"].get("issue") == issue and item["status"] not in ("closed",):
            raise ConflictError("open remediation already exists for issue")


def _validate_permit(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("permit requires equipment")
    if data.get("purpose") not in ("return_to_service", "special_inspection", "temporary_operation"):
        raise ValidationError("invalid permit purpose")


def _pass_inspection(actor, entity, data, lookup):
    return {"passed_by": actor.user_id, "passed_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def _validate_daily_plan(data, lookup):
    plan_day = _parse_day(data.get("plan_date"))
    data["plan_date"] = plan_day.isoformat()
    try:
        capacity = int(data.get("reinspection_capacity"))
    except (TypeError, ValueError):
        raise ValidationError("reinspection_capacity must be an integer")
    if capacity <= 0:
        raise ValidationError("reinspection_capacity must be positive")
    data["reinspection_capacity"] = capacity
    if _find_one(lookup, "daily_plan", "plan_date", data["plan_date"]):
        raise ConflictError("daily plan already exists for " + data["plan_date"])


def validate_booking(actor, data, lookup):
    """Validate a reinspection booking request; the service runs this inside the slot transaction."""
    _ensure_role(actor, ("admin", "dispatcher"))
    plan = _find_one(lookup, "daily_plan", "id", data.get("plan_id"))
    if not plan:
        raise ValidationError("reinspection requires an existing daily plan")
    if plan["status"] != "open":
        raise ConflictError("daily plan %s is %s" % (plan["id"], plan["status"]))
    equipment = _find_one(lookup, "equipment", "id", data.get("equipment_id"))
    if not equipment:
        raise ValidationError("reinspection requires equipment")
    for existing in _all(lookup, "reinspection"):
        if (
            existing["data"].get("equipment_id") == equipment["id"]
            and existing["data"].get("plan_date") == plan["data"]["plan_date"]
            and existing["status"] in ("scheduled", "queued")
        ):
            raise ConflictError(
                "equipment %s already has an active reinspection %s on %s (status: %s)"
                % (equipment["id"], existing["id"], plan["data"]["plan_date"], existing["status"])
            )
    cleaned = dict(data)
    cleaned["plan_date"] = plan["data"]["plan_date"]
    cleaned["risk_level"] = _risk_of(equipment)
    return cleaned


def _sign_off_reinspection(actor, entity, data, lookup):
    """High-risk elevators can only be signed by a senior inspector (or admin)."""
    if entity["data"].get("risk_level") == "high" and actor.role not in ("admin", "senior_inspector"):
        raise PermissionDenied("high-risk reinspection requires a senior_inspector")
    return {"signed_off_by": actor.user_id, "signed_off_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def _void_reinspection(actor, entity, data, lookup):
    return {"voided_by": actor.user_id, "void_reason": data.get("reason", "voided")}


def _permit_reinspection_block(equipment_id, lookup):
    """Permits stay frozen while a reinspection is pending or the latest one failed."""
    bookings = [
        item
        for item in _all(lookup, "reinspection")
        if item["data"].get("equipment_id") == equipment_id
    ]
    pending = [item for item in bookings if item["status"] in ("scheduled", "queued")]
    if pending:
        raise ConflictError("permit frozen: reinspection pending for equipment")
    terminal = [item for item in bookings if item["status"] in ("passed", "failed", "voided")]
    if terminal:
        latest = max(terminal, key=lambda item: str(item.get("updated_at") or ""))
        if latest["status"] == "failed":
            raise ConflictError("permit frozen: latest reinspection failed")


def _request_permit_review(actor, entity, data, lookup):
    _permit_reinspection_block(entity["data"].get("equipment_id"), lookup)
    return {}


def _grant_permit(actor, entity, data, lookup):
    equipment = _find_one(lookup, "equipment", "id", entity["data"].get("equipment_id"))
    if not equipment or equipment["status"] not in ("in_service", "suspended"):
        raise ConflictError("permit can only be granted for a serviceable equipment")
    _permit_reinspection_block(equipment["id"], lookup)
    passed = [
        item
        for item in _all(lookup, "inspection") + _all(lookup, "reinspection")
        if item["data"].get("equipment_id") == equipment["id"] and item["status"] == "passed"
    ]
    if not passed:
        raise ConflictError("permit requires a passed inspection")
    if [r for r in _all(lookup, "remediation") if r["data"].get("equipment_id") == equipment["id"] and r["status"] != "closed"]:
        raise ConflictError("permit blocked by open remediation")
    return {"granted_by": actor.user_id, "granted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def _verify_remediation(actor, entity, data, lookup):
    if not entity["data"].get("evidence"):
        raise ValidationError("remediation evidence is required before verification")
    return {"verified_by": actor.user_id}


def _complete_rescue(actor, entity, data, lookup):
    jobs = [j for j in _all(lookup, "rescue_job") if j["data"].get("alarm_id") == entity["id"]]
    if not jobs or any(job["status"] not in ("completed", "aborted") for job in jobs):
        raise ConflictError("alarm cannot close before rescue jobs are complete")
    return {"resolved_by": actor.user_id}


class RuleEngine:
    ALIASES = {
        "equipments": "equipment", "inspections": "inspection", "maintenances": "maintenance",
        "alarms": "alarm", "rescue_jobs": "rescue_job", "remediations": "remediation",
        "permits": "permit", "daily_plans": "daily_plan", "reinspections": "reinspection",
    }
    INITIAL_STATUS = {
        "equipment": "in_service", "inspection": "scheduled", "maintenance": "planned",
        "alarm": "received", "rescue_job": "dispatched", "remediation": "open",
        "permit": "blocked", "daily_plan": "open", "reinspection": "queued",
    }
    TRANSITIONS = {
        "equipment": {
            "suspend": (("in_service",), "suspended"),
            "out_of_service": (("in_service", "suspended"), "out_of_service"),
            "return_to_service": (("suspended",), "in_service"),
        },
        "inspection": {
            "pass": (("scheduled",), "passed"),
            "fail": (("scheduled",), "failed"),
            "reschedule": (("failed",), "scheduled"),
        },
        "maintenance": {
            "start": (("planned",), "in_progress"),
            "complete": (("in_progress",), "completed"),
        },
        "alarm": {
            "dispatch": (("received",), "dispatched"),
            "mark_false": (("received", "dispatched"), "false_alarm"),
            "resolve": (("dispatched",), "resolved"),
            "close": (("resolved",), "closed"),
        },
        "rescue_job": {
            "arrive": (("dispatched",), "on_site"),
            "complete": (("on_site",), "completed"),
            "abort": (("dispatched", "on_site"), "aborted"),
        },
        "remediation": {
            "submit_evidence": (("open",), "evidence_submitted"),
            "verify": (("evidence_submitted",), "verified"),
            "reject": (("evidence_submitted",), "open"),
            "close": (("verified",), "closed"),
        },
        "permit": {
            "request_review": (("blocked",), "pending_review"),
            "grant": (("pending_review",), "granted"),
            "revoke": (("granted", "pending_review"), "revoked"),
            "expire": (("granted",), "expired"),
        },
        "daily_plan": {
            "close": (("open",), "closed"),
            "reopen": (("closed",), "open"),
        },
        "reinspection": {
            "pass": (("scheduled",), "passed"),
            "fail": (("scheduled",), "failed"),
            "void": (("scheduled", "queued"), "voided"),
        },
    }
    CREATE_REQUIRED = {
        "equipment": ("asset_no", "equipment_type", "location", "inspection_interval_days"),
        "inspection": ("equipment_id", "scheduled_at", "cycle_days"),
        "maintenance": ("equipment_id", "work_type", "planned_at"),
        "alarm": ("equipment_id", "code", "occurred_at"),
        "rescue_job": ("alarm_id", "dedupe_key", "team"),
        "remediation": ("issue", "owner", "due_at"),
        "permit": ("equipment_id", "purpose", "requested_by"),
        "daily_plan": ("plan_date", "reinspection_capacity"),
        "reinspection": ("equipment_id", "plan_id"),
    }
    ACTION_REQUIRED = {
        ("inspection", "pass"): ("findings",),
        ("inspection", "fail"): ("findings",),
        ("maintenance", "complete"): ("completed_at",),
        ("rescue_job", "complete"): ("outcome",),
        ("remediation", "submit_evidence"): ("evidence",),
        ("alarm", "resolve"): ("resolution",),
        ("permit", "revoke"): ("reason",),
        ("reinspection", "pass"): ("findings",),
        ("reinspection", "fail"): ("findings",),
        ("reinspection", "void"): ("reason",),
    }
    CREATE_ROLES = {
        "equipment": ("admin", "inspector"),
        "inspection": ("admin", "inspector"),
        "maintenance": ("admin", "maintenance"),
        "alarm": ("admin", "dispatcher", "inspector"),
        "rescue_job": ("admin", "dispatcher"),
        "remediation": ("admin", "inspector", "maintenance"),
        "permit": ("admin", "inspector"),
        "daily_plan": ("admin", "dispatcher"),
        "reinspection": ("admin", "dispatcher"),
    }
    ROLE_ACTIONS = {
        "suspend": ("admin", "inspector"),
        "out_of_service": ("admin", "inspector"),
        "return_to_service": ("admin", "inspector"),
        "pass": ("admin", "inspector", "senior_inspector"),
        "fail": ("admin", "inspector", "senior_inspector"),
        "reschedule": ("admin", "inspector"),
        "start": ("admin", "maintenance"),
        "complete": ("admin", "maintenance", "dispatcher"),
        "dispatch": ("admin", "dispatcher"),
        "mark_false": ("admin", "dispatcher", "inspector"),
        "resolve": ("admin", "dispatcher"),
        "close": ("admin", "dispatcher", "inspector"),
        "arrive": ("admin", "dispatcher"),
        "abort": ("admin", "dispatcher"),
        "submit_evidence": ("admin", "maintenance", "inspector"),
        "verify": ("admin", "inspector"),
        "reject": ("admin", "inspector"),
        "request_review": ("admin", "inspector"),
        "grant": ("admin", "inspector"),
        "revoke": ("admin", "inspector"),
        "expire": ("admin", "inspector"),
        ("daily_plan", "close"): ("admin", "dispatcher"),
        ("daily_plan", "reopen"): ("admin", "dispatcher"),
        ("reinspection", "pass"): SIGN_OFF_ROLES,
        ("reinspection", "fail"): SIGN_OFF_ROLES,
        ("reinspection", "void"): ("admin", "dispatcher"),
    }
    CUSTOM_CREATE = {
        "equipment": lambda a, d, l: _validate_equipment(d, l),
        "inspection": lambda a, d, l: _validate_inspection(d, l),
        "maintenance": lambda a, d, l: _validate_maintenance(d, l),
        "alarm": lambda a, d, l: _validate_alarm(d, l),
        "rescue_job": lambda a, d, l: _validate_rescue(d, l),
        "remediation": lambda a, d, l: _validate_remediation(d, l),
        "permit": lambda a, d, l: _validate_permit(d, l),
        "daily_plan": lambda a, d, l: _validate_daily_plan(d, l),
    }
    CUSTOM_TRANSITIONS = {
        ("permit", "grant"): _grant_permit,
        ("permit", "request_review"): _request_permit_review,
        ("remediation", "verify"): _verify_remediation,
        ("alarm", "close"): _complete_rescue,
        ("inspection", "pass"): _pass_inspection,
        ("reinspection", "pass"): _sign_off_reinspection,
        ("reinspection", "fail"): _sign_off_reinspection,
        ("reinspection", "void"): _void_reinspection,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def assert_role(self, actor, allowed):
        _ensure_role(actor, allowed)

    def validate_booking(self, actor, data, lookup):
        return validate_booking(actor, data, lookup)

    def assign_slots(self, bookings, capacity, lookup):
        """
        Rank all active bookings for one daily plan and assign slots.

        Returns {booking_id: {"status", "slot", "reasons"}}; the service applies
        the changes inside a single database transaction.
        """
        active = [b for b in bookings if b["status"] in ("scheduled", "queued")]
        ordered = sorted(active, key=lambda booking: reinspection_priority(booking, lookup))
        assignment = {}
        for position, booking in enumerate(ordered, start=1):
            new_status = "scheduled" if position <= int(capacity) else "queued"
            assignment[booking["id"]] = {
                "status": new_status,
                "slot": position if new_status == "scheduled" else None,
                "reasons": priority_reasons(booking, lookup),
            }
        return assignment
