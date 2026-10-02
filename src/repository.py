import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError
from .rules import REINSPECTION_DAILY_CAPACITY


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reinspection_active_equipment
                    ON entities(json_extract(data, '$.equipment_id'))
                    WHERE kind = 'reinspection' AND status IN ('scheduled', 'queued');
                CREATE INDEX IF NOT EXISTS idx_reinspection_date
                    ON entities(json_extract(data, '$.scheduled_date'))
                    WHERE kind = 'reinspection';
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def count_reinspections(self, scheduled_date, status):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS c FROM entities "
                "WHERE kind = 'reinspection' AND status = ? "
                "AND json_extract(data, '$.scheduled_date') = ?",
                (status, scheduled_date),
            ).fetchone()
        return int(row["c"]) if row else 0

    def schedule_reinspection(self, entity_id, payload, actor_id, capacity):
        """Atomically assign a slot, queue when full, and bump lowest priority when needed."""
        scheduled_date = payload["scheduled_date"]
        priority = int(payload.get("priority", 1))
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, data, version FROM entities "
                "WHERE kind = 'reinspection' AND status = 'scheduled' "
                "AND json_extract(data, '$.scheduled_date') = ? "
                "ORDER BY json_extract(data, '$.priority') DESC, created_at",
                (scheduled_date,),
            ).fetchall()
            status = "scheduled"
            bump = None
            if priority == 2:
                status = "queued"
            elif len(rows) >= int(capacity):
                target = rows[0]
                target_priority = int(json.loads(target["data"]).get("priority", 1))
                if priority == 0 and target_priority > priority:
                    bump = target
                else:
                    status = "queued"
            queued_count = int(connection.execute(
                "SELECT COUNT(*) AS c FROM entities "
                "WHERE kind = 'reinspection' AND status = 'queued' "
                "AND json_extract(data, '$.scheduled_date') = ?",
                (scheduled_date,),
            ).fetchone()["c"])
            if bump is not None:
                bump_data = json.loads(bump["data"])
                bump_data["queue_position"] = queued_count + 1
                connection.execute(
                    "UPDATE entities SET status = 'queued', version = version + 1, data = ?, updated_at = ? "
                    "WHERE id = ? AND version = ?",
                    (json.dumps(bump_data, ensure_ascii=False, sort_keys=True), now, bump["id"], bump["version"]),
                )
            elif status == "queued":
                payload["queue_position"] = queued_count + 1
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, 'reinspection', ?, 1, ?, ?, ?, ?)",
                (entity_id, status, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, now),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if "reinspection_active_equipment" in str(exc):
                raise ConflictError("equipment already has an active reinspection")
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def promote_queued_reinspections(self, scheduled_date, capacity):
        """Promote highest-priority queued reinspections into freed slots."""
        now = utcnow()
        promoted = []
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            scheduled_count = int(connection.execute(
                "SELECT COUNT(*) AS c FROM entities "
                "WHERE kind = 'reinspection' AND status = 'scheduled' "
                "AND json_extract(data, '$.scheduled_date') = ?",
                (scheduled_date,),
            ).fetchone()["c"])
            while scheduled_count < int(capacity):
                row = connection.execute(
                    "SELECT id, data, version FROM entities "
                    "WHERE kind = 'reinspection' AND status = 'queued' "
                    "AND json_extract(data, '$.scheduled_date') = ? "
                    "ORDER BY json_extract(data, '$.priority') ASC, "
                    "json_extract(data, '$.queue_position') ASC, created_at LIMIT 1",
                    (scheduled_date,),
                ).fetchone()
                if not row:
                    break
                data = json.loads(row["data"])
                data.pop("queue_position", None)
                connection.execute(
                    "UPDATE entities SET status = 'scheduled', version = version + 1, data = ?, updated_at = ? "
                    "WHERE id = ? AND version = ?",
                    (json.dumps(data, ensure_ascii=False, sort_keys=True), now, row["id"], row["version"]),
                )
                promoted.append(row["id"])
                scheduled_count += 1
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return promoted

    def void_reinspections_for_equipment(self, equipment_id):
        """Void all active reinspections for an equipment and free their slots."""
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, data, version FROM entities "
                "WHERE kind = 'reinspection' AND status IN ('scheduled', 'queued') "
                "AND json_extract(data, '$.equipment_id') = ?",
                (equipment_id,),
            ).fetchall()
            affected_dates = set()
            for row in rows:
                data = json.loads(row["data"])
                affected_dates.add(data.get("scheduled_date"))
                data["voided_reason"] = "equipment_status_changed"
                connection.execute(
                    "UPDATE entities SET status = 'voided', version = version + 1, data = ?, updated_at = ? "
                    "WHERE id = ? AND version = ?",
                    (json.dumps(data, ensure_ascii=False, sort_keys=True), now, row["id"], row["version"]),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        for scheduled_date in affected_dates:
            if scheduled_date:
                self.promote_queued_reinspections(scheduled_date, REINSPECTION_DAILY_CAPACITY)
        return [row["id"] for row in rows]

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        entities = self.list_entities(kind=kind)
        if field == "*":
            return entities
        return [
            entity
            for entity in entities
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
