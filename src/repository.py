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
                CREATE TABLE IF NOT EXISTS bridge_coverage (
                    coverage_key TEXT PRIMARY KEY,
                    bridge_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bridge_stop_checkpoint (
                    bridge_id TEXT PRIMARY KEY,
                    cutoff_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    failure_run_id TEXT NOT NULL,
                    recalled_batch_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL
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

    def confirm_bridge(self, bridge_id, expected_version, patch, actor):
        """Confirm a bridge under a single effective-version guarantee.

        Concurrent confirmations of the same bridge serialize on the
        bridge_coverage row: exactly one wins, losers return the winning
        version without writing a new version or a duplicate audit record.
        """
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, kind, status, version, data FROM entities WHERE id = ?",
                (bridge_id,),
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + bridge_id)
            status = row["status"]
            version = int(row["version"])
            if status == "confirmed":
                connection.commit()
                return self.get_entity(bridge_id), False
            if status != "pending":
                raise ConflictError("bridge in status %s can no longer be confirmed" % status)
            if version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s" % (expected_version, version)
                )
            data = json.loads(row["data"])
            coverage_key = data["coverage_key"]
            existing = connection.execute(
                "SELECT bridge_id FROM bridge_coverage WHERE coverage_key = ?",
                (coverage_key,),
            ).fetchone()
            if existing:
                connection.commit()
                return self.get_entity(existing["bridge_id"]), False
            data.update(patch)
            connection.execute(
                "UPDATE entities SET status = 'confirmed', version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ?",
                (json.dumps(data, ensure_ascii=False, sort_keys=True), now, bridge_id),
            )
            connection.execute(
                "INSERT INTO bridge_coverage(coverage_key, bridge_id) VALUES (?, ?)",
                (coverage_key, bridge_id),
            )
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, 'confirm', 'pending', 'confirmed', ?, ?)",
                (
                    bridge_id,
                    actor.user_id,
                    actor.role,
                    json.dumps({"patch": patch}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            connection.commit()
            won = True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(bridge_id), won

    def stop_bridge(self, bridge_id, cutoff_at, reason, failure_run_id, actor):
        """Suspend a bridge and recall batches after the cutoff.

        Checkpointed so a retry resumes from the same point: it never adds a
        second stop record and never logs a second recall for a batch.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint = connection.execute(
                "SELECT recalled_batch_ids FROM bridge_stop_checkpoint WHERE bridge_id = ?",
                (bridge_id,),
            ).fetchone()
            if checkpoint:
                connection.commit()
                return self.get_entity(bridge_id), json.loads(checkpoint["recalled_batch_ids"])
            row = connection.execute(
                "SELECT id, kind, status, version, data FROM entities WHERE id = ?",
                (bridge_id,),
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + bridge_id)
            status = row["status"]
            if status not in ("confirmed", "suspended"):
                raise ConflictError("bridge in status %s cannot be stopped" % status)
            data = json.loads(row["data"])
            cutoff_at = str(cutoff_at)
            previous_lot_id = data.get("previous_lot_id")
            new_lot_id = data.get("new_lot_id")
            instrument_id = data.get("instrument_id")
            assay_id = data.get("assay_id")
            candidate_batches = connection.execute(
                "SELECT id, status, data FROM entities "
                "WHERE kind = 'result_batch' AND status IN ('waiting', 'released')"
            ).fetchall()
            recalled = []
            now = utcnow()
            for batch in candidate_batches:
                batch_data = json.loads(batch["data"])
                covered_by_bridge = batch_data.get("bridge_id") == bridge_id
                run_lot_id = None
                if not covered_by_bridge:
                    if batch_data.get("instrument_id") != instrument_id:
                        continue
                    if batch_data.get("assay_id") != assay_id:
                        continue
                    run = connection.execute(
                        "SELECT data FROM entities WHERE id = ? AND kind = 'qc_run'",
                        (batch_data.get("qc_run_id"),),
                    ).fetchone()
                    if not run:
                        continue
                    run_lot_id = json.loads(run["data"]).get("qc_lot_id")
                    if run_lot_id not in (previous_lot_id, new_lot_id):
                        continue
                if str(batch_data.get("run_at") or "") <= cutoff_at:
                    continue
                prior_status = batch["status"]
                connection.execute(
                    "UPDATE entities SET status = 'intercepted', version = version + 1, updated_at = ? "
                    "WHERE id = ?",
                    (now, batch["id"]),
                )
                connection.execute(
                    "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'intercepted', ?, ?)",
                    (
                        batch["id"],
                        actor.user_id,
                        actor.role,
                        "recall" if prior_status == "released" else "intercept",
                        prior_status,
                        json.dumps(
                            {"bridge_id": bridge_id, "cutoff_at": cutoff_at, "reason": reason},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                recalled.append(batch["id"])
            data.update(
                {
                    "cutoff_at": cutoff_at,
                    "stop_reason": reason,
                    "failure_run_id": failure_run_id,
                    "recalled_batch_ids": recalled,
                }
            )
            new_status = "suspended"
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? WHERE id = ?",
                (new_status, json.dumps(data, ensure_ascii=False, sort_keys=True), now, bridge_id),
            )
            connection.execute(
                "DELETE FROM bridge_coverage WHERE bridge_id = ?",
                (bridge_id,),
            )
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, 'stop', ?, 'suspended', ?, ?)",
                (
                    bridge_id,
                    actor.user_id,
                    actor.role,
                    status,
                    json.dumps(
                        {
                            "cutoff_at": cutoff_at,
                            "reason": reason,
                            "failure_run_id": failure_run_id,
                            "recalled_batch_ids": recalled,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO bridge_stop_checkpoint(bridge_id, cutoff_at, reason, failure_run_id, recalled_batch_ids, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    bridge_id,
                    cutoff_at,
                    reason,
                    failure_run_id,
                    json.dumps(recalled, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        bridge = self.get_entity(bridge_id)
        return bridge, list(bridge["data"].get("recalled_batch_ids") or [])

    def mark_legacy_pending_bridge(self, bridge_keys, actor_id="migration"):
        """Upgrade legacy result batches missing a bridge stamp.

        Batches released before bridging was tracked lose the old conclusion:
        they become pending_bridge and keep their previous status for audit.
        Returns the upgraded entity ids.
        """
        upgraded = []
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = utcnow()
            rows = connection.execute(
                "SELECT id, status, data FROM entities WHERE kind = 'result_batch'"
            ).fetchall()
            for row in rows:
                data = json.loads(row["data"])
                if data.get("bridge_id") or data.get("bridge_upgrade"):
                    continue
                if not bridge_keys.get(row["id"]):
                    continue
                data["bridge_upgrade"] = True
                data["pre_upgrade_status"] = row["status"]
                connection.execute(
                    "UPDATE entities SET status = 'pending_bridge', version = version + 1, data = ?, updated_at = ? "
                    "WHERE id = ?",
                    (json.dumps(data, ensure_ascii=False, sort_keys=True), now, row["id"]),
                )
                connection.execute(
                    "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                    "VALUES (?, ?, 'system', 'upgrade_pending_bridge', ?, 'pending_bridge', ?, ?)",
                    (
                        row["id"],
                        actor_id,
                        row["status"],
                        json.dumps({"reason": "missing bridge key on legacy result"}, ensure_ascii=False),
                        now,
                    ),
                )
                upgraded.append(row["id"])
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return upgraded

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
