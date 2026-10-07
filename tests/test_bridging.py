import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine, bridge_is_effective
from src.service import DomainService


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "bridge.db"),
            RuleEngine(),
        )
        self.supervisor = Actor("qc-supervisor", "supervisor")
        self.operator = Actor("qc-operator", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _world(self):
        assay = self.service.create(
            self.supervisor,
            "assay",
            {"name": "Glucose", "unit": "mmol/L", "allowed_low": 3.9, "allowed_high": 6.1},
        )
        old_lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "OLD", "target": 5.0, "sd": 0.1, "expires_at": "2099-01-01"},
        )
        old_lot = self.service.transition(self.supervisor, old_lot["id"], "activate", {"activated_by": "s"})
        new_lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "NEW", "target": 5.1, "sd": 0.11, "expires_at": "2099-06-01"},
        )
        new_lot = self.service.transition(
            self.supervisor,
            new_lot["id"],
            "switch_in",
            {"previous_lot_id": old_lot["id"], "switched_at": "2026-10-01T00:00:00Z"},
        )
        instrument = self.service.create(
            self.supervisor,
            "instrument",
            {"name": "Analyzer A", "serial": "A-100", "calibration_due": "2099-01-01"},
        )
        return assay, old_lot, new_lot, instrument

    def _run_and_batch(self, assay, lot, instrument, at, value=5.01, count=3):
        run = self.service.create(
            self.operator,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": value,
                "run_at": at,
            },
        )
        run = self.service.transition(self.operator, run["id"], "evaluate", {"evaluated_by": "op"})
        batch = self.service.create(
            self.operator,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": at,
                "patient_count": count,
            },
        )
        return run, batch

    def _bridge(self, assay, old_lot, new_lot, instrument, expires_at="2099-12-31T00:00:00Z", confirm=True):
        bridge = self.service.create(
            self.supervisor,
            "lot_bridge",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "new_lot_id": new_lot["id"],
                "previous_lot_id": old_lot["id"],
                "expires_at": expires_at,
            },
        )
        if confirm:
            bridge = self.service.transition(
                self.supervisor, bridge["id"], "confirm", {"authorizer_id": "auth-1"}
            )
        return bridge

    def test_bridge_must_be_confirmed_before_borrowed_release(self):
        assay, old_lot, new_lot, instrument = self._world()
        _, batch = self._run_and_batch(assay, old_lot, instrument, "2026-10-02T08:00:00Z")
        # No bridge yet: old-lot results cannot ride on the old conclusion.
        with self.assertRaises(ConflictError):
            self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "r"})
        bridge = self._bridge(assay, old_lot, new_lot, instrument, confirm=False)
        self.assertEqual(bridge["status"], "pending")
        with self.assertRaises(ConflictError):
            self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "r"})
        bridge = self.service.transition(
            self.supervisor, bridge["id"], "confirm", {"authorizer_id": "auth-1"}
        )
        self.assertEqual(bridge["status"], "confirmed")
        released = self.service.transition(
            self.supervisor, batch["id"], "release", {"reviewer_id": "r"}
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["release_mode"], "bridge")
        self.assertEqual(released["data"]["bridge_id"], bridge["id"])
        self.assertEqual(
            released["data"]["bridge_key"],
            "%s:%s:%s" % (instrument["id"], assay["id"], new_lot["id"]),
        )

    def test_operator_cannot_authorize_bridge(self):
        assay, old_lot, new_lot, instrument = self._world()
        bridge = self.service.create(
            self.supervisor,
            "lot_bridge",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "new_lot_id": new_lot["id"],
                "previous_lot_id": old_lot["id"],
                "expires_at": "2099-12-31",
            },
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.operator, bridge["id"], "confirm", {"authorizer_id": "auth-1"}
            )

    def test_expired_bridge_blocks_release_until_reconfirmed(self):
        assay, old_lot, new_lot, instrument = self._world()
        bridge = self._bridge(
            assay, old_lot, new_lot, instrument, expires_at="2026-10-05T00:00:00Z"
        )
        self.assertFalse(bridge_is_effective(bridge, "2026-10-05T01:00:00Z"))
        _, batch = self._run_and_batch(assay, old_lot, instrument, "2026-10-06T08:00:00Z")
        bridge = self.service.transition(
            self.supervisor, bridge["id"], "expire", {"reason": "window elapsed"}
        )
        self.assertEqual(bridge["status"], "expired")
        with self.assertRaises(ConflictError):
            self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "r"})
        # Renew coverage, then an authorizer confirms the new window again.
        bridge = self.service.transition(
            self.supervisor,
            bridge["id"],
            "reconfirm",
            {"expires_at": "2099-12-31T00:00:00Z", "authorizer_id": "auth-2"},
        )
        self.assertEqual(bridge["status"], "pending")
        bridge = self.service.transition(
            self.supervisor, bridge["id"], "confirm", {"authorizer_id": "auth-2"}
        )
        self.assertTrue(bridge_is_effective(bridge, "2026-10-06T08:00:00Z"))
        released = self.service.transition(
            self.supervisor, batch["id"], "release", {"reviewer_id": "r"}
        )
        self.assertEqual(released["status"], "released")
        # Renewal reuses the same coverage row: still one effective version.
        self.assertEqual(bridge["data"]["coverage_key"], released["data"]["bridge_key"])

    def test_stop_after_failure_only_recalls_after_cutoff_and_is_resumable(self):
        assay, old_lot, new_lot, instrument = self._world()
        bridge = self._bridge(assay, old_lot, new_lot, instrument)
        _, before = self._run_and_batch(assay, old_lot, instrument, "2026-10-02T07:59:00Z")
        before = self.service.transition(
            self.supervisor, before["id"], "release", {"reviewer_id": "r"}
        )
        _, after_waiting = self._run_and_batch(assay, old_lot, instrument, "2026-10-02T09:00:00Z")
        _, after_released = self._run_and_batch(assay, old_lot, instrument, "2026-10-02T10:00:00Z")
        after_released = self.service.transition(
            self.supervisor, after_released["id"], "release", {"reviewer_id": "r"}
        )
        failure_run = self.service.create(
            self.operator,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": new_lot["id"],
                "instrument_id": instrument["id"],
                "value": 5.9,
                "run_at": "2026-10-02T08:00:00Z",
            },
        )
        failure_run = self.service.transition(
            self.operator, failure_run["id"], "evaluate", {"evaluated_by": "op"}
        )
        self.assertEqual(failure_run["status"], "rejected")
        stopped = self.service.transition(
            self.supervisor,
            bridge["id"],
            "stop",
            {
                "cutoff_at": "2026-10-02T08:00:00Z",
                "reason": "new lot 1_3s failure",
                "failure_run_id": failure_run["id"],
            },
        )
        self.assertEqual(stopped["status"], "suspended")
        self.assertEqual(
            sorted(stopped["data"]["recalled_batch_ids"]),
            sorted([after_waiting["id"], after_released["id"]]),
        )
        # Results at/after cutoff only; the pre-cutoff release stays released.
        self.assertEqual(self.service.get(before["id"])["status"], "released")
        self.assertEqual(self.service.get(after_waiting["id"])["status"], "intercepted")
        self.assertEqual(self.service.get(after_released["id"])["status"], "intercepted")
        # Suspended bridge stops further releases under it.
        _, new_waiting = self._run_and_batch(assay, old_lot, instrument, "2026-10-02T11:00:00Z")
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.supervisor, new_waiting["id"], "release", {"reviewer_id": "r"}
            )
        # Retrying the stop resumes from the checkpoint: same bridge, no new recalls.
        retry = self.service.transition(
            self.supervisor,
            bridge["id"],
            "stop",
            {
                "cutoff_at": "2026-10-02T08:00:00Z",
                "reason": "new lot 1_3s failure",
                "failure_run_id": failure_run["id"],
            },
        )
        self.assertEqual(retry["version"], stopped["version"])
        interceptions = [
            row for row in self.service.audit_log()
            if row["entity_id"] in (after_waiting["id"], after_released["id"])
            and row["action"] in ("intercept", "recall")
        ]
        self.assertEqual(len(interceptions), 2)

    def test_concurrent_confirmations_create_one_effective_version(self):
        assay, old_lot, new_lot, instrument = self._world()
        bridge = self._bridge(assay, old_lot, new_lot, instrument, confirm=False)
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def confirm():
            try:
                barrier.wait()
                results.append(
                    self.service.transition(
                        self.supervisor, bridge["id"], "confirm", {"authorizer_id": "auth-x"}
                    )
                )
            except Exception as exc:  # pragma: no cover - diagnostic path
                errors.append(exc)

        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 2)
        self.assertEqual({row["id"] for row in results}, {bridge["id"]})
        self.assertEqual({row["version"] for row in results}, {2})
        stored = self.service.get(bridge["id"])
        self.assertEqual(stored["status"], "confirmed")
        confirmations = [
            row for row in self.service.audit_log(bridge["id"]) if row["action"] == "confirm"
        ]
        self.assertEqual(len(confirmations), 1)

    def test_legacy_released_batch_upgraded_and_cannot_reuse_old_conclusion(self):
        instrument = self.service.create(
            self.supervisor,
            "instrument",
            {"name": "I", "serial": "S", "calibration_due": "2099-01-01"},
        )
        assay = self.service.create(
            self.supervisor,
            "assay",
            {"name": "A", "unit": "u", "allowed_low": 0, "allowed_high": 10},
        )
        old_lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "OLD", "target": 5.0, "sd": 0.1, "expires_at": "2099-01-01"},
        )
        old_lot = self.service.transition(self.supervisor, old_lot["id"], "activate", {"activated_by": "s"})
        # Simulate legacy data: released batch stamped by the pre-bridge version.
        run, batch = self._run_and_batch(assay, old_lot, instrument, "2026-09-20T08:00:00Z")
        batch = self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "r"})
        self.assertEqual(batch["data"]["release_mode"], "normal")
        new_lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "NEW", "target": 5.1, "sd": 0.11, "expires_at": "2099-06-01"},
        )
        self.service.transition(
            self.supervisor,
            new_lot["id"],
            "switch_in",
            {"previous_lot_id": old_lot["id"], "switched_at": "2026-10-01T00:00:00Z"},
        )
        outcome = self.service.upgrade_legacy_batches(self.supervisor)
        self.assertEqual(outcome["upgraded"], [batch["id"]])
        upgraded = self.service.get(batch["id"])
        self.assertEqual(upgraded["status"], "pending_bridge")
        self.assertTrue(upgraded["data"]["bridge_upgrade"])
        self.assertEqual(upgraded["data"]["pre_upgrade_status"], "released")
        # The old released conclusion cannot be used; release needs coverage.
        with self.assertRaises(ConflictError):
            self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "r2"})
        bridge = self._bridge(assay, old_lot, new_lot, instrument)
        re_released = self.service.transition(
            self.supervisor, batch["id"], "release", {"reviewer_id": "r2"}
        )
        self.assertEqual(re_released["status"], "released")
        self.assertEqual(re_released["data"]["release_mode"], "bridge")
        self.assertFalse(re_released["data"]["bridge_upgrade"])
        # Upgrade is idempotent.
        again = self.service.upgrade_legacy_batches(self.supervisor)
        self.assertEqual(again["upgraded"], [])


if __name__ == "__main__":
    unittest.main()
