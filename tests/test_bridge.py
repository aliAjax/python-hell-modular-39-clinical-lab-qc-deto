import json
import tempfile
import threading
import unittest
import http.client
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
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

    def _base(self):
        assay = self.service.create(
            self.supervisor,
            "assay",
            {"name": "Glucose", "unit": "mmol/L", "allowed_low": 3.9, "allowed_high": 6.1},
        )
        old_lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-1", "target": 5.0, "sd": 0.1, "expires_at": "2099-01-01"},
        )
        old_lot = self.service.transition(self.supervisor, old_lot["id"], "activate", {"activated_by": "qc-1"})
        new_lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-2", "target": 5.1, "sd": 0.1, "expires_at": "2099-06-01"},
        )
        instrument = self.service.create(
            self.supervisor,
            "instrument",
            {"name": "Analyzer A", "serial": "A-100", "calibration_due": "2099-01-01"},
        )
        return assay, old_lot, new_lot, instrument

    def _make_bridge(self, assay, old_lot, new_lot, instrument, cutoff=None, valid_until="2099-12-31"):
        return self.service.create(
            self.supervisor,
            "qc_bridge",
            {
                "instrument_id": instrument["id"],
                "assay_id": assay["id"],
                "qc_lot_id": new_lot["id"],
                "borrowed_lot_id": old_lot["id"],
                "valid_until": valid_until,
                "cutoff_at": cutoff,
            },
        )

    def _accepted_run(self, assay, lot, instrument, value, run_at):
        run = self.service.create(
            self.supervisor,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": value,
                "run_at": run_at,
            },
        )
        return self.service.transition(self.supervisor, run["id"], "evaluate", {"evaluated_by": "qc-1"})

    def _batch(self, assay, instrument, run, run_at, count=1):
        return self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": run_at,
                "patient_count": count,
            },
        )

    def test_bridge_confirm_and_release_under_bridge(self):
        assay, old_lot, new_lot, instrument = self._base()
        bridge = self._make_bridge(assay, old_lot, new_lot, instrument)
        self.assertEqual(bridge["status"], "pending")
        bridge = self.service.transition(self.supervisor, bridge["id"], "confirm", {"confirmed_by": "qc-2"})
        self.assertEqual(bridge["status"], "confirmed")
        self.assertEqual(bridge["data"]["borrowed_lot_id"], old_lot["id"])
        run = self._accepted_run(assay, new_lot, instrument, 5.12, "2026-09-27T08:00:00Z")
        batch = self._batch(assay, instrument, run, "2026-09-27T08:05:00Z", 12)
        batch = self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        self.assertEqual(batch["status"], "released")
        self.assertEqual(batch["data"]["bridge_id"], bridge["id"])
        self.assertTrue(batch["data"]["released_under_bridge"])

    def test_bridge_requires_authorization(self):
        assay, old_lot, new_lot, instrument = self._base()
        bridge = self._make_bridge(assay, old_lot, new_lot, instrument)
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.operator, bridge["id"], "confirm", {"confirmed_by": "op"})
        bridge = self.service.transition(self.supervisor, bridge["id"], "confirm", {"confirmed_by": "qc-2"})
        self.assertEqual(bridge["status"], "confirmed")

    def test_bridge_expire_and_reconfirm(self):
        assay, old_lot, new_lot, instrument = self._base()
        bridge = self._make_bridge(assay, old_lot, new_lot, instrument)
        bridge = self.service.transition(self.supervisor, bridge["id"], "confirm", {"confirmed_by": "qc-2"})
        bridge = self.service.transition(self.supervisor, bridge["id"], "expire", {"reason": "lot coverage expired"})
        self.assertEqual(bridge["status"], "expired")
        run = self._accepted_run(assay, new_lot, instrument, 5.12, "2026-09-27T08:00:00Z")
        batch = self._batch(assay, instrument, run, "2026-09-27T08:05:00Z", 12)
        with self.assertRaises(ConflictError):
            self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        bridge = self.service.transition(
            self.supervisor, bridge["id"], "reconfirm", {"valid_until": "2099-12-31", "reconfirmed_by": "qc-2"}
        )
        self.assertEqual(bridge["status"], "confirmed")
        batch = self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        self.assertEqual(batch["status"], "released")

    def test_duplicate_bridge_conflict(self):
        assay, old_lot, new_lot, instrument = self._base()
        self._make_bridge(assay, old_lot, new_lot, instrument)
        with self.assertRaises(ConflictError):
            self._make_bridge(assay, old_lot, new_lot, instrument)

    def test_bridge_creation_validation(self):
        assay, old_lot, new_lot, instrument = self._base()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.supervisor,
                "qc_bridge",
                {
                    "instrument_id": instrument["id"],
                    "assay_id": assay["id"],
                    "qc_lot_id": new_lot["id"],
                    "borrowed_lot_id": new_lot["id"],
                    "valid_until": "2099-12-31",
                },
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.supervisor,
                "qc_bridge",
                {
                    "instrument_id": instrument["id"],
                    "assay_id": assay["id"],
                    "qc_lot_id": new_lot["id"],
                    "borrowed_lot_id": old_lot["id"],
                },
            )

    def test_oos_rolls_back_only_after_cutoff(self):
        assay, old_lot, new_lot, instrument = self._base()
        bridge = self._make_bridge(
            assay, old_lot, new_lot, instrument, cutoff="2026-09-27T08:00:00Z"
        )
        bridge = self.service.transition(self.supervisor, bridge["id"], "confirm", {"confirmed_by": "qc-2"})
        run_a = self._accepted_run(assay, new_lot, instrument, 5.12, "2026-09-27T07:59:00Z")
        batch_a = self._batch(assay, instrument, run_a, "2026-09-27T07:59:00Z", 5)
        batch_a = self.service.transition(self.supervisor, batch_a["id"], "release", {"reviewer_id": "qc-2"})
        run_b = self._accepted_run(assay, new_lot, instrument, 5.12, "2026-09-27T08:01:00Z")
        batch_b = self._batch(assay, instrument, run_b, "2026-09-27T08:01:00Z", 7)
        batch_b = self.service.transition(self.supervisor, batch_b["id"], "release", {"reviewer_id": "qc-2"})
        bridge = self.service.transition(
            self.supervisor, bridge["id"], "rollback", {"reason": "new lot out of control"}
        )
        self.assertEqual(bridge["status"], "stopped")
        batch_a = self.service.get(batch_a["id"])
        batch_b = self.service.get(batch_b["id"])
        self.assertEqual(batch_a["status"], "released")
        self.assertEqual(batch_b["status"], "returned")
        run_c = self._accepted_run(assay, new_lot, instrument, 5.12, "2026-09-27T08:05:00Z")
        batch_c = self._batch(assay, instrument, run_c, "2026-09-27T08:05:00Z", 3)
        with self.assertRaises(ConflictError):
            self.service.transition(self.supervisor, batch_c["id"], "release", {"reviewer_id": "qc-2"})

    def test_concurrent_confirm_one_effective_version(self):
        assay, old_lot, new_lot, instrument = self._base()
        bridge = self._make_bridge(assay, old_lot, new_lot, instrument)
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def do_confirm():
            barrier.wait()
            try:
                results.append(
                    self.service.transition(
                        self.supervisor, bridge["id"], "confirm", {"confirmed_by": "qc-2"}, expected_version=1
                    )
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_confirm) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], (ConflictError, InvalidTransition))
        bridge = self.service.get(bridge["id"])
        self.assertEqual(bridge["status"], "confirmed")
        self.assertEqual(bridge["version"], 2)

    def test_rollback_resumes_from_breakpoint_without_duplicate_returns(self):
        assay, old_lot, new_lot, instrument = self._base()
        bridge = self._make_bridge(
            assay, old_lot, new_lot, instrument, cutoff="2026-09-27T08:00:00Z"
        )
        bridge = self.service.transition(self.supervisor, bridge["id"], "confirm", {"confirmed_by": "qc-2"})
        run_a = self._accepted_run(assay, new_lot, instrument, 5.12, "2026-09-27T08:01:00Z")
        batch_a = self._batch(assay, instrument, run_a, "2026-09-27T08:01:00Z", 5)
        batch_a = self.service.transition(self.supervisor, batch_a["id"], "release", {"reviewer_id": "qc-2"})
        run_b = self._accepted_run(assay, new_lot, instrument, 5.12, "2026-09-27T08:02:00Z")
        batch_b = self._batch(assay, instrument, run_b, "2026-09-27T08:02:00Z", 7)
        batch_b = self.service.transition(self.supervisor, batch_b["id"], "release", {"reviewer_id": "qc-2"})
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.supervisor, bridge["id"], "rollback", {"reason": "new lot out of control"}, expected_version=1
            )
        self.assertEqual(self.service.get(batch_a["id"])["status"], "returned")
        self.assertEqual(self.service.get(batch_b["id"])["status"], "returned")
        bridge = self.service.transition(
            self.supervisor, bridge["id"], "rollback", {"reason": "new lot out of control"}
        )
        self.assertEqual(bridge["status"], "stopped")
        self.assertEqual(self.service.get(batch_a["id"])["status"], "returned")
        self.assertEqual(self.service.get(batch_b["id"])["status"], "returned")
        audits_a = [a for a in self.service.audit_log(batch_a["id"]) if a["action"] == "rollback"]
        audits_b = [a for a in self.service.audit_log(batch_b["id"]) if a["action"] == "rollback"]
        audits_bridge = [a for a in self.service.audit_log(bridge["id"]) if a["action"] == "rollback"]
        self.assertEqual(len(audits_a), 1)
        self.assertEqual(len(audits_b), 1)
        self.assertEqual(len(audits_bridge), 1)
        self.assertEqual(audits_bridge[0]["detail"]["rolled_back"], [])

    def test_migration_marks_legacy_pending_bridge(self):
        assay, lot, _, instrument = self._base()
        run = self._accepted_run(assay, lot, instrument, 5.02, "2026-09-27T08:00:00Z")
        legacy = self._batch(assay, instrument, run, "2026-09-27T08:05:00Z", 9)
        legacy = self.service.transition(self.supervisor, legacy["id"], "release", {"reviewer_id": "qc-2"})
        self.assertEqual(legacy["status"], "released")
        self.assertNotIn("bridge_id", legacy["data"])
        result = self.service.migrate_legacy_bridges(self.supervisor)
        self.assertEqual(result["count"], 1)
        legacy = self.service.get(legacy["id"])
        self.assertEqual(legacy["status"], "pending_bridge")
        result = self.service.migrate_legacy_bridges(self.supervisor)
        self.assertEqual(result["count"], 0)

    def test_migration_requires_authorization(self):
        with self.assertRaises(PermissionDenied):
            self.service.migrate_legacy_bridges(self.operator)


class BridgeHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "bridge-http.db"),
            RuleEngine(),
        )
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), str(Path(self.tmp.name)))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _request(self, method, path, body=None, actor="qc-supervisor", role="supervisor"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-User-Id": actor, "X-Role": role}
        if body is not None:
            headers["Content-Type"] = "application/json"
            connection.request(method, path, json.dumps(body), headers)
        else:
            connection.request(method, path, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, json.loads(payload.decode("utf-8"))

    def _setup(self):
        _, assay = self._request("POST", "/api/assay", {"name": "Glucose", "unit": "mmol/L", "allowed_low": 3.9, "allowed_high": 6.1})
        _, old_lot = self._request("POST", "/api/qc_lot", {"assay_id": assay["id"], "lot_no": "LOT-1", "target": 5.0, "sd": 0.1, "expires_at": "2099-01-01"})
        self._request("POST", "/api/entities/%s/actions" % old_lot["id"], {"action": "activate", "data": {"activated_by": "qc-1"}})
        _, new_lot = self._request("POST", "/api/qc_lot", {"assay_id": assay["id"], "lot_no": "LOT-2", "target": 5.1, "sd": 0.1, "expires_at": "2099-06-01"})
        _, instrument = self._request("POST", "/api/instrument", {"name": "A", "serial": "S", "calibration_due": "2099-01-01"})
        return assay, old_lot, new_lot, instrument

    def test_bridge_lifecycle_over_http(self):
        assay, old_lot, new_lot, instrument = self._setup()
        status, bridge = self._request(
            "POST",
            "/api/qc_bridge",
            {
                "instrument_id": instrument["id"],
                "assay_id": assay["id"],
                "qc_lot_id": new_lot["id"],
                "borrowed_lot_id": old_lot["id"],
                "valid_until": "2099-12-31",
                "cutoff_at": "2026-09-27T08:00:00Z",
            },
        )
        self.assertEqual(status, 201)
        status, bridge = self._request(
            "POST", "/api/entities/%s/actions" % bridge["id"], {"action": "confirm", "data": {"confirmed_by": "qc-2"}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(bridge["status"], "confirmed")
        status, run = self._request(
            "POST",
            "/api/qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": new_lot["id"],
                "instrument_id": instrument["id"],
                "value": 5.12,
                "run_at": "2026-09-27T08:01:00Z",
            },
        )
        self.assertEqual(status, 201)
        self._request("POST", "/api/entities/%s/actions" % run["id"], {"action": "evaluate", "data": {"evaluated_by": "qc-1"}})
        status, batch = self._request(
            "POST",
            "/api/result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": "2026-09-27T08:01:00Z",
                "patient_count": 3,
            },
        )
        status, batch = self._request(
            "POST", "/api/entities/%s/actions" % batch["id"], {"action": "release", "data": {"reviewer_id": "qc-2"}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(batch["status"], "released")
        self.assertEqual(batch["data"]["bridge_id"], bridge["id"])
        status, migrated = self._request("POST", "/api/migrate/bridges")
        self.assertEqual(status, 200)
        self.assertEqual(migrated["count"], 0)


if __name__ == "__main__":
    unittest.main()
