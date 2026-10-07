from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, PermissionDenied
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
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "lot_bridge" and action == "confirm":
            return self.confirm_bridge(actor, entity_id, data or {}, expected_version)
        if kind == "lot_bridge" and action == "stop":
            return self.stop_bridge(actor, entity_id, data or {})
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
        if kind == "lot_bridge" and action == "expire":
            self._drop_bridge_coverage(entity_id)
        return updated

    def confirm_bridge(self, actor, entity_id, data, expected_version=None):
        """Authorizer confirmation; concurrent calls yield one effective version."""
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] != "lot_bridge":
            raise InvalidTransition("confirm only applies to lot bridges")
        if entity["status"] == "confirmed":
            # Idempotent retry: return the single effective version, no new record.
            return entity
        next_status, patch = self.rules.validate_transition(
            actor, entity, "confirm", dict(data or {}), self._lookup
        )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        merged = dict(entity["data"])
        merged.update(patch)
        updated, won = self.repository.confirm_bridge(entity_id, expected, merged, actor)
        return updated

    def stop_bridge(self, actor, entity_id, data):
        """Stop release under a failed new lot and recall results after cutoff.

        Retries after a failure resume from the persisted checkpoint and never
        add duplicate interception/recall records.
        """
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] != "lot_bridge":
            raise InvalidTransition("stop only applies to lot bridges")
        next_status, patch = self.rules.validate_transition(
            actor, entity, "stop", dict(data or {}), self._lookup
        )
        updated, recalled = self.repository.stop_bridge(
            entity_id,
            patch["cutoff_at"],
            patch["reason"],
            patch["failure_run_id"],
            actor,
        )
        return updated

    def _drop_bridge_coverage(self, entity_id):
        with self.repository._connect() as connection:
            connection.execute("DELETE FROM bridge_coverage WHERE bridge_id = ?", (entity_id,))

    def upgrade_legacy_batches(self, actor):
        """Mark released result batches that lack bridge keys as pending_bridge."""
        if actor.role not in ("supervisor", "admin"):
            raise PermissionDenied("role %s is not allowed here" % actor.role)
        batches = self.repository.list_entities(kind="result_batch")
        needs_upgrade = {}
        for batch in batches:
            data = batch["data"]
            if data.get("bridge_id") or data.get("bridge_upgrade"):
                continue
            if batch["status"] != "released":
                continue
            run = next(iter(self._lookup("qc_run", "id", data.get("qc_run_id")) or []), None)
            if not run:
                continue
            assay_id = data.get("assay_id")
            run_lot_id = run["data"].get("qc_lot_id")
            superseded = any(
                str(lot["data"].get("replaces_lot_id") or "") == str(run_lot_id)
                for lot in self._lookup("qc_lot", "assay_id", assay_id)
            )
            if superseded:
                needs_upgrade[batch["id"]] = run_lot_id
        upgraded = self.repository.mark_legacy_pending_bridge(needs_upgrade, actor.user_id)
        return {"upgraded": upgraded}

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
