from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError
from .repository import utcnow
from .rules import RuleEngine


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
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "qc_bridge" and action == "rollback":
            return self._rollback_bridge(actor, entity, data or {}, expected_version)
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
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def _return_batch(self, actor, batch, bridge):
        now = utcnow()
        patch = {
            "returned_by": actor.user_id,
            "returned_at": now,
            "return_reason": "bridge rollback: new lot out of control",
        }
        merged = dict(batch["data"])
        merged.update(patch)
        updated = self.repository.update_entity(batch["id"], int(batch["version"]), "returned", merged)
        self.audit.record(
            batch["id"],
            actor,
            "rollback",
            "released",
            "returned",
            {"bridge_id": bridge["id"], "cutoff_at": bridge["data"].get("cutoff_at")},
        )
        return updated

    def _rollback_bridge(self, actor, bridge, data, expected_version):
        self.rules._ensure_role(actor, ("supervisor", "admin"))
        if bridge["status"] not in ("confirmed", "stopped"):
            raise InvalidTransition("cannot rollback bridge from status %s" % bridge["status"])
        cutoff = str(bridge["data"].get("cutoff_at") or bridge["data"].get("valid_from") or "")
        candidates = [
            batch
            for batch in self.repository.find_entities("result_batch", "bridge_id", bridge["id"])
            if batch["status"] == "released" and str(batch["data"].get("run_at", "")) > cutoff
        ]
        rolled_back = []
        skipped = []
        for batch in candidates:
            try:
                self._return_batch(actor, batch, bridge)
                rolled_back.append(batch["id"])
            except ConflictError:
                fresh = self.repository.get_entity(batch["id"])
                if fresh and fresh["status"] == "released":
                    self._return_batch(actor, fresh, bridge)
                    rolled_back.append(batch["id"])
                else:
                    skipped.append(batch["id"])
        expected = int(expected_version) if expected_version is not None else bridge["version"]
        patch = {
            "stopped_by": actor.user_id,
            "stopped_at": utcnow(),
            "stop_reason": data.get("reason") or "new lot out of control",
        }
        merged = dict(bridge["data"])
        merged.update(patch)
        updated = self.repository.update_entity(bridge["id"], expected, "stopped", merged)
        self.audit.record(
            bridge["id"],
            actor,
            "rollback",
            bridge["status"],
            "stopped",
            {"cutoff_at": cutoff, "rolled_back": rolled_back, "skipped": skipped},
        )
        return updated

    def migrate_legacy_bridges(self, actor):
        self.rules._ensure_role(actor, ("supervisor", "admin"))
        batches = self.repository.list_entities(kind="result_batch", status="released")
        migrated = []
        for batch in batches:
            if batch["data"].get("bridge_id"):
                continue
            patch = {"migration_reason": "legacy result batch without bridge key"}
            merged = dict(batch["data"])
            merged.update(patch)
            self.repository.update_entity(batch["id"], int(batch["version"]), "pending_bridge", merged)
            self.audit.record(
                batch["id"],
                actor,
                "migrate_bridge",
                "released",
                "pending_bridge",
                {"reason": "legacy result batch without bridge key"},
            )
            migrated.append(batch["id"])
        return {"migrated": migrated, "count": len(migrated)}

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
