import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


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
                CREATE TABLE IF NOT EXISTS sequence_members (
                    sequence_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY(sequence_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_sequence_members_event
                    ON sequence_members(event_id);
                CREATE TABLE IF NOT EXISTS recompute_batches (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    done INTEGER NOT NULL,
                    failed INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS recompute_batch_items (
                    batch_id TEXT NOT NULL,
                    sequence_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id, sequence_id)
                );
                CREATE INDEX IF NOT EXISTS idx_batch_items_status
                    ON recompute_batch_items(batch_id, status);
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
        return [
            entity
            for entity in self.list_entities(kind=kind)
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

    def list_sequence_members(self, sequence_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id FROM sequence_members WHERE sequence_id = ? ORDER BY event_id",
                (sequence_id,),
            ).fetchall()
        return [row["event_id"] for row in rows]

    def list_member_sequences(self, event_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence_id FROM sequence_members WHERE event_id = ? ORDER BY sequence_id",
                (event_id,),
            ).fetchall()
        return [row["sequence_id"] for row in rows]

    def recompute_sequence(self, sequence_id, expected_version, next_status, new_data,
                           member_ids, notice_entity, audit_entries, bump_version=True):
        """原子地完成一次重算：乐观锁更新序列、对账成员表、写入公告快照、追加审计。

        成员表只做 INSERT OR IGNORE / 多余 DELETE，已经是目标状态的成员不产生写动作，
        因此批次重试不会重复改动已经改过的成员。
        """
        now = utcnow()
        payload = json.dumps(new_data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version, status FROM entities WHERE id = ? AND kind = 'sequence'",
                (sequence_id,),
            ).fetchone()
            if not row:
                raise NotFoundError("sequence not found: " + sequence_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            if bump_version:
                connection.execute(
                    "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                    "WHERE id = ?",
                    (next_status, payload, now, sequence_id),
                )
            else:
                connection.execute(
                    "UPDATE entities SET status = ?, data = ?, updated_at = ? WHERE id = ?",
                    (next_status, payload, now, sequence_id),
                )
            desired = set(member_ids or [])
            current = {
                item["event_id"]
                for item in connection.execute(
                    "SELECT event_id FROM sequence_members WHERE sequence_id = ?",
                    (sequence_id,),
                ).fetchall()
            }
            added = sorted(desired - current)
            removed = sorted(current - desired)
            for event_id in added:
                connection.execute(
                    "INSERT OR IGNORE INTO sequence_members(sequence_id, event_id, added_at) "
                    "VALUES (?, ?, ?)",
                    (sequence_id, event_id, now),
                )
            for event_id in removed:
                connection.execute(
                    "DELETE FROM sequence_members WHERE sequence_id = ? AND event_id = ?",
                    (sequence_id, event_id),
                )
            if notice_entity is not None:
                notice = notice_entity
                notice_payload = json.dumps(notice["data"], ensure_ascii=False, sort_keys=True)
                connection.execute(
                    "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                    "VALUES (?, 'sequence_notice', ?, 1, ?, ?, ?, ?)",
                    (notice["id"], notice["status"], notice_payload,
                     notice["created_by"], now, now),
                )
            for entry in audit_entries:
                connection.execute(
                    "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry["entity_id"], entry["actor_id"], entry["actor_role"],
                        entry["action"], entry["from_status"], entry["to_status"],
                        json.dumps(entry["detail"], ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(sequence_id), {
            "added": added,
            "removed": removed,
            "unchanged": sorted(desired & current),
        }

    # ----- 重算批次 -----
    def create_recompute_batch(self, batch_id, sequence_ids, actor_id):
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO recompute_batches(id, status, total, done, failed, created_by, created_at, updated_at, last_error) "
                "VALUES (?, 'running', ?, 0, 0, ?, ?, ?, NULL)",
                (batch_id, len(sequence_ids), actor_id, now, now),
            )
            for sequence_id in sequence_ids:
                connection.execute(
                    "INSERT INTO recompute_batch_items(batch_id, sequence_id, status, attempts, error, updated_at) "
                    "VALUES (?, ?, 'pending', 0, NULL, ?)",
                    (batch_id, sequence_id, now),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_recompute_batch(self, batch_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recompute_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if not row:
                return None
            items = connection.execute(
                "SELECT sequence_id, status, attempts, error FROM recompute_batch_items "
                "WHERE batch_id = ? ORDER BY sequence_id",
                (batch_id,),
            ).fetchall()
        return {
            "id": row["id"],
            "status": row["status"],
            "total": int(row["total"]),
            "done": int(row["done"]),
            "failed": int(row["failed"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_error": row["last_error"],
            "items": [
                {
                    "sequence_id": item["sequence_id"],
                    "status": item["status"],
                    "attempts": int(item["attempts"]),
                    "error": item["error"],
                }
                for item in items
            ],
        }

    def list_pending_batch_items(self, batch_id, only_pending=True):
        statuses = ("pending",) if only_pending else ("pending", "failed")
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence_id FROM recompute_batch_items WHERE batch_id = ? AND status IN (%s) "
                "ORDER BY sequence_id" % placeholders,
                (batch_id, *statuses),
            ).fetchall()
        return [row["sequence_id"] for row in rows]

    def mark_batch_item(self, batch_id, sequence_id, status, error=None):
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE recompute_batch_items SET status = ?, attempts = attempts + 1, error = ?, updated_at = ? "
                "WHERE batch_id = ? AND sequence_id = ?",
                (status, error, now, batch_id, sequence_id),
            )
            connection.execute(
                "UPDATE recompute_batches SET done = (SELECT COUNT(*) FROM recompute_batch_items "
                "WHERE batch_id = ? AND status = 'done'), failed = (SELECT COUNT(*) FROM recompute_batch_items "
                "WHERE batch_id = ? AND status = 'failed'), last_error = ?, updated_at = ? WHERE id = ?",
                (batch_id, batch_id, error, now, batch_id),
            )
            remaining = connection.execute(
                "SELECT COUNT(*) AS c FROM recompute_batch_items "
                "WHERE batch_id = ? AND status = 'pending'",
                (batch_id,),
            ).fetchone()["c"]
            failed_count = connection.execute(
                "SELECT COUNT(*) AS c FROM recompute_batch_items "
                "WHERE batch_id = ? AND status = 'failed'",
                (batch_id,),
            ).fetchone()["c"]
            if remaining == 0:
                connection.execute(
                    "UPDATE recompute_batches SET status = ?, updated_at = ? WHERE id = ?",
                    ("partial" if failed_count else "completed", now, batch_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
