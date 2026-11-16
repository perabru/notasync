"""Local-test approval boundaries; all browser operations are mocked here."""
import asyncio
import copy
import importlib
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from test_app import imported, student

backend = importlib.import_module("notasync.app")
REQUIRED_CHECKS = ("browser", "login", "classes", "columns", "fill", "save", "reload",
                   "preservation", "stale", "cancelled", "complete")


def training_result(**updates):
    return {"passed": True, "checks": [{"id": check, "label": check, "status": "passed"}
                                      for check in REQUIRED_CHECKS],
            "message": "Teste fictício concluído", "session_id": "synthetic-local-session",
            "source_mode": "imported_sheet", "test_mode": "full", **updates}


class ValidationGateTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(backend.state)
        backend.state.clear()
        config = copy.deepcopy(backend.DEFAULT_CONFIG)
        config.update(target_year=2026, discipline_aliases={})
        backend.state.update(
            imports=[imported([student()], mode="AT4"),
                     imported([student(value=4.2, discipline="AT1")], mode="AT1", filename="primeiro.xlsx")],
            portal={"status": "disconnected", "message": "Exemplo", "classes": []}, jobs=[], demo=False,
            config=config, plan=None, revision=0, audit=[], adapter=None, task=None, _test_adapter=None,
            validation={"status": "idle", "checks": [], "assessments": [], "test_id": None})

    def tearDown(self):
        backend.state.clear()
        backend.state.update(self.original)

    def post(self, client, url, body):
        return client.post(url, headers={"x-notasync-token": backend.TOKEN}, json=body)

    def wait_for_job(self, client):
        async def wait():
            await asyncio.wait_for(asyncio.shield(backend.state["task"]), timeout=3)
        client.portal.call(wait)
        return client.get("/api/state").json()

    def run_test(self, client, result=None, assessments=("AT4",), **body_updates):
        from notasync.training import get_training_plan

        result = result if result is not None else training_result()
        test_mode = body_updates.get("mode", "full")
        plan = get_training_plan(backend.training_snapshot(assessments), assessments, test_mode)
        if "counts" not in result:
            result["counts"] = {**plan, "tested_records": plan["planned_records"],
                                "confirmed_writes": plan["planned_records"]}
        elif isinstance(result["counts"], dict):
            result["counts"] = {**plan, **result["counts"]}
        run = AsyncMock(return_value=result)
        with patch("notasync.training.run_training", run):
            response = self.post(client, "/api/test", {"assessments": list(assessments), **body_updates})
            self.assertEqual(response.status_code, 200, response.text)
            snapshot = self.wait_for_job(client)
        return snapshot, run

    def launch(self, client, test_id=None, assessments=("AT4",)):
        return self.post(client, "/api/launch",
                         {"username": "coordenador_ficticio", "password": "senha_ficticia",
                          "assessments": list(assessments), "show_browser": False, "test_id": test_id})

    def assert_gate_rejects(self, client, test_id, assessments=("AT4",)):
        jobs_before = len(backend.state["jobs"])
        with patch.object(backend, "PortalAdapter") as adapter:
            response = self.launch(client, test_id, assessments)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("Testar", response.json()["detail"])
        adapter.assert_not_called()
        self.assertEqual(len(backend.state["jobs"]), jobs_before)

    def test_launch_without_successful_test_stops_before_opening_real_browser(self):
        with TestClient(backend.app) as client:
            for status in ("idle", "running", "failed", "outdated"):
                with self.subTest(status=status):
                    backend.state["validation"]["status"] = status
                    self.assert_gate_rejects(client, None)

    def test_execute_without_test_stops_before_saving_any_grade(self):
        adapter = AsyncMock()
        backend.state["adapter"] = adapter
        backend.state["plan"] = {"plan_id": "synthetic-plan", "revision": 0, "consumed": False,
                                 "assessments": ["AT4"], "changes": [{"new": 8.5}]}
        with patch.object(backend, "execute_plan", new_callable=AsyncMock) as execute, \
                TestClient(backend.app) as client:
            response = self.post(client, "/api/portal/execute", {"plan_id": "synthetic-plan"})
            self.assertEqual(response.status_code, 409, response.text)
            execute.assert_not_awaited()
            adapter.apply_offering.assert_not_awaited()
            self.assertFalse(backend.state["plan"]["consumed"])

    def test_all_passed_checks_issue_proof_for_current_source_config_and_selection(self):
        browser = AsyncMock()
        with TestClient(backend.app) as client:
            snapshot, run = self.run_test(client, training_result(adapter=browser), assessments=("AT4", "AT1"))
            proof = snapshot["validation"]
            self.assertEqual(snapshot["jobs"][-1]["status"], "completed", snapshot)
            self.assertEqual(proof["status"], "passed")
            self.assertTrue(proof["test_id"])
            self.assertEqual(proof["assessments"], ["AT1", "AT4"])
            self.assertEqual({check["id"] for check in proof["checks"]}, set(REQUIRED_CHECKS))
            self.assertEqual(proof["fingerprint"], backend.test_fingerprint(["AT1", "AT4"]))
            self.assertEqual(proof["scope"], "local_simulation")
            self.assertEqual(proof["source_mode"], "imported_sheet")
            self.assertEqual(proof["test_mode"], "full")
            self.assertEqual(proof["summary"]["tested_records"], 2)
            self.assertEqual(proof["summary"]["confirmed_writes"], 2)
            self.assertIs(backend.state["_test_adapter"], browser)
            self.assertNotIn("adapter", proof)
            backend.require_local_test(["AT4", "AT1"], proof["test_id"])
            run.assert_awaited_once()
            frozen = run.await_args.kwargs["source_snapshot"]
            self.assertEqual(frozen["expected_records"], 2)
            self.assertEqual({row["assessment"]: row["grade"] for row in frozen["rows"]},
                             {"AT1": 4.2, "AT4": 8.5})
        browser.close.assert_awaited_once()

    def test_missing_any_required_check_cannot_approve_or_mint_test_id(self):
        with TestClient(backend.app) as client:
            for missing in REQUIRED_CHECKS:
                with self.subTest(missing=missing):
                    result = training_result()
                    result["checks"] = [check for check in result["checks"] if check["id"] != missing]
                    snapshot, _ = self.run_test(client, result)
                    proof = snapshot["validation"]
                    self.assertEqual(proof["status"], "failed", proof)
                    self.assertIsNone(proof["test_id"])
                    self.assertIsNone(proof["fingerprint"])
                    self.assertEqual(snapshot["jobs"][-1]["status"], "failed")
                    self.assert_gate_rejects(client, "invented-id")

    def test_failed_running_or_extra_failed_checks_cannot_approve(self):
        results = []
        for status in ("failed", "running"):
            result = training_result()
            result["checks"][5]["status"] = status
            results.append(result)
        results.append(training_result(checks=training_result()["checks"] +
                                      [{"id": "additional-verification", "status": "failed"}]))
        results.append(training_result(passed=False))
        with TestClient(backend.app) as client:
            for result in results:
                with self.subTest(result=result):
                    snapshot, _ = self.run_test(client, result)
                    self.assertEqual(snapshot["validation"]["status"], "failed", snapshot)
                    self.assertIsNone(snapshot["validation"]["test_id"])
                    self.assert_gate_rejects(client, "invented-id")

    def test_failed_test_closes_private_browser_and_revokes_previous_approval(self):
        first_browser, failed_browser = AsyncMock(), AsyncMock()
        with TestClient(backend.app) as client:
            first, _ = self.run_test(client, training_result(adapter=first_browser))
            identifier = first["validation"]["test_id"]
            failed, _ = self.run_test(client, training_result(passed=False, adapter=failed_browser))
            self.assertEqual(failed["validation"]["status"], "failed")
            first_browser.close.assert_awaited_once()
            failed_browser.close.assert_awaited_once()
            self.assertIsNone(backend.state["_test_adapter"])
            self.assert_gate_rejects(client, identifier)

    def test_source_grade_change_rejects_previous_test_even_without_invalidation(self):
        with TestClient(backend.app) as client:
            snapshot, _ = self.run_test(client)
            backend.state["imports"][0]["sheets"][0]["students"][0]["grade"] = 9.9
            self.assert_gate_rejects(client, snapshot["validation"]["test_id"])

    def test_config_change_rejects_previous_test_even_without_invalidation(self):
        with TestClient(backend.app) as client:
            snapshot, _ = self.run_test(client)
            backend.state["config"]["target_year"] = 2027
            self.assert_gate_rejects(client, snapshot["validation"]["test_id"])

    def test_changed_assessment_selection_cannot_reuse_previous_test_id(self):
        with TestClient(backend.app) as client:
            snapshot, _ = self.run_test(client)
            identifier = snapshot["validation"]["test_id"]
            self.assert_gate_rejects(client, identifier, assessments=("AT1",))
            self.assert_gate_rejects(client, identifier, assessments=("AT1", "AT4"))

    def test_missing_or_incorrect_test_id_is_rejected_despite_current_pass(self):
        with TestClient(backend.app) as client:
            self.run_test(client)
            self.assert_gate_rejects(client, None)
            self.assert_gate_rejects(client, "another-session-id")

    def test_new_test_id_supersedes_previous_approval(self):
        with TestClient(backend.app) as client:
            first, _ = self.run_test(client)
            second, _ = self.run_test(client)
            old_id, current_id = first["validation"]["test_id"], second["validation"]["test_id"]
            self.assertNotEqual(old_id, current_id)
            self.assert_gate_rejects(client, old_id)
            backend.require_local_test(["AT4"], current_id)

    def test_selection_invalidation_revokes_id_and_closes_test_browser(self):
        browser = AsyncMock()
        with TestClient(backend.app) as client:
            snapshot, _ = self.run_test(client, training_result(adapter=browser))
            response = self.post(client, "/api/test/invalidate", {"reason": "Avaliações alteradas"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "outdated")
            self.assertIsNone(response.json()["test_id"])
            browser.close.assert_awaited_once()
            self.assert_gate_rejects(client, snapshot["validation"]["test_id"])

    def test_source_changed_while_test_runs_never_receives_approval(self):
        async def changed_during_test(*args, **kwargs):
            backend.state["imports"][0]["sheets"][0]["students"][0]["grade"] = 1.2
            return training_result()
        with patch("notasync.training.run_training", side_effect=changed_during_test), \
                TestClient(backend.app) as client:
            response = self.post(client, "/api/test", {"assessments": ["AT4"]})
            self.assertEqual(response.status_code, 200, response.text)
            snapshot = self.wait_for_job(client)
            self.assertEqual(snapshot["validation"]["status"], "failed", snapshot)
            self.assertIsNone(snapshot["validation"]["test_id"])
            self.assertIn("alterada durante", snapshot["validation"]["message"])

    def test_synthetic_or_missing_source_mode_never_approves_an_imported_sheet(self):
        with TestClient(backend.app) as client:
            for mode in (None, "", "synthetic_demo", "synthetic", "unrecognized"):
                with self.subTest(source_mode=mode):
                    result = training_result(source_mode=mode)
                    if mode is None:
                        result.pop("source_mode")
                    snapshot, _ = self.run_test(client, result)
                    self.assertEqual(snapshot["validation"]["status"], "failed", snapshot)
                    self.assertIsNone(snapshot["validation"]["test_id"])
                    self.assert_gate_rejects(client, "synthetic-approval")

    def test_zero_invalid_or_unconfirmed_record_counts_cannot_approve(self):
        invalid_counts = (
            {},
            {"tested_records": 0, "confirmed_writes": 0},
            {"tested_records": -1, "confirmed_writes": -1},
            {"tested_records": None, "confirmed_writes": 1},
            {"tested_records": "1", "confirmed_writes": 1},
            {"tested_records": 1.0, "confirmed_writes": 1},
            {"tested_records": True, "confirmed_writes": 1},
            {"tested_records": 1, "confirmed_writes": True},
            {"tested_records": 1, "confirmed_writes": 0},
            {"tested_records": 2, "confirmed_writes": 1},
            {"tested_records": 1, "confirmed_writes": 2},
            {"tested_records": 1, "confirmed_writes": "1"},
            {"tested_records": 1},
        )
        with TestClient(backend.app) as client:
            for counts in invalid_counts:
                with self.subTest(counts=counts):
                    snapshot, _ = self.run_test(client, training_result(counts=counts))
                    self.assertEqual(snapshot["validation"]["status"], "failed", snapshot)
                    self.assertIsNone(snapshot["validation"]["test_id"])
                    self.assertEqual(snapshot["jobs"][-1]["status"], "failed")
                    self.assert_gate_rejects(client, "unconfirmed-records")

    def test_imported_sheet_proof_cannot_fall_back_to_a_synthetic_proof_at_launch(self):
        with TestClient(backend.app) as client:
            for mode in (None, "synthetic_demo"):
                with self.subTest(source_mode=mode):
                    snapshot, _ = self.run_test(client)
                    backend.state["validation"]["source_mode"] = mode
                    self.assert_gate_rejects(client, snapshot["validation"]["test_id"])

    def test_partial_source_coverage_never_approves_even_if_reported_writes_match(self):
        with TestClient(backend.app) as client:
            result, _ = self.run_test(client, training_result(counts={"tested_records": 1, "confirmed_writes": 1}),
                                      assessments=("AT1", "AT4"))
            self.assertEqual(result["validation"]["status"], "failed", result)
            self.assertIsNone(result["validation"]["test_id"])
            self.assert_gate_rejects(client, "partial-test", assessments=("AT1", "AT4"))

    def test_selected_source_snapshot_preserves_grades_metadata_and_protected_values(self):
        sheet = backend.state["imports"][0]["sheets"][0]
        sheet.update(source_format="portal_export", portal_columns=["Aluno", "Código", "Situação", "AT4", "AR4", "MB4", "MBF"],
                     metadata={"class_name": "1FID", "discipline": "Redes de Computadores", "course": "Curso de Teste",
                               "discipline_code": "TESTE001", "year": 2026, "period": "0"})
        source_row = sheet["students"][0]
        source_row.update(source_cell="R10", portal_values={"A11": "7.2", "AR4": "2.5", "MB4": "7.6", "MBF": "8.1", "C-AT4": "S"})
        sheet["students"].append(student(ra="50000002", value=None, status="Cancelado", eligible=False,
                                           source_cell="R11", portal_values={"AR4": "", "MB4": "", "MBF": ""}))
        before = copy.deepcopy(backend.state["imports"])
        with TestClient(backend.app) as client:
            result, run = self.run_test(client, username="unused-real-login", password="unused-real-secret")
            frozen = run.await_args.kwargs["source_snapshot"]
            self.assertEqual({row["assessment"] for row in frozen["rows"]}, {"AT4"})
            self.assertEqual({row["source_file"] for row in frozen["rows"]}, {"exemplo.xlsx"})
            self.assertEqual(len(frozen["rows"]), 2)
            self.assertEqual(frozen["expected_records"], 1)
            eligible = [row for row in frozen["rows"] if row["eligible"]]
            self.assertEqual(len(eligible), 1)
            self.assertEqual(eligible[0]["registration"], "50000001")
            self.assertEqual(eligible[0]["grade"], 8.5)
            self.assertEqual(eligible[0]["class_name"], "1FID")
            self.assertEqual(eligible[0]["source_cell"], "R10")
            self.assertEqual(eligible[0]["portal_values"], source_row["portal_values"])
            blocked = [row for row in frozen["rows"] if not row["eligible"]]
            self.assertEqual(blocked[0]["status"], "Cancelado")
            self.assertIsNone(blocked[0]["grade"])
            selected_metadata = next(item for item in frozen["metadata"] if item["source_file"] == "exemplo.xlsx")
            self.assertEqual(selected_metadata["metadata"], sheet["metadata"])
            self.assertEqual(selected_metadata["portal_columns"], sheet["portal_columns"])
            self.assertEqual(selected_metadata["source_format"], "portal_export")
            self.assertEqual(backend.state["imports"], before)
            self.assertIsNot(eligible[0]["portal_values"], source_row["portal_values"])
            self.assertIsNot(selected_metadata["metadata"], sheet["metadata"])
            frozen["rows"][0]["portal_values"]["MBF"] = "0.1"
            selected_metadata["metadata"]["year"] = 2030
            self.assertEqual(backend.state["imports"], before)
            public_snapshot = json.dumps(frozen)
            self.assertNotIn("unused-real-login", public_snapshot)
            self.assertNotIn("unused-real-secret", public_snapshot)
            backend.require_local_test(["AT4"], result["validation"]["test_id"])

    def test_source_class_status_metadata_and_protected_values_bind_the_fingerprint(self):
        sheet = backend.state["imports"][0]["sheets"][0]
        sheet["metadata"] = {"year": 2026, "period": "0", "class_name": "1FID"}
        sheet["students"][0]["portal_values"] = {"MBF": "8.0", "AR4": "3.0"}
        mutations = (
            lambda source: source[0]["sheets"][0]["students"][0].update(class_name="2FID"),
            lambda source: source[0]["sheets"][0]["students"][0].update(status="Cancelado"),
            lambda source: source[0]["sheets"][0]["students"][0]["portal_values"].update(MBF="8.1"),
            lambda source: source[0]["sheets"][0]["metadata"].update(year=2027),
            lambda source: source[0]["sheets"][0].update(class_name="2FID"),
        )
        original_imports = copy.deepcopy(backend.state["imports"])
        with TestClient(backend.app) as client:
            for index, mutate in enumerate(mutations):
                with self.subTest(mutation=index):
                    backend.state["imports"] = copy.deepcopy(original_imports)
                    result, _ = self.run_test(client)
                    proof = result["validation"]
                    mutate(backend.state["imports"])
                    self.assertNotEqual(backend.test_fingerprint(["AT4"]), proof["fingerprint"])
                    with self.assertRaises(backend.HTTPException) as rejected:
                        backend.require_local_test(["AT4"], proof["test_id"])
                    self.assertEqual(rejected.exception.status_code, 409)

    def test_training_receives_no_coordinator_credentials(self):
        username, password = "real_user_marker", "real_password_marker"
        with patch.object(backend, "PortalAdapter") as live_adapter, TestClient(backend.app) as client:
            snapshot, run = self.run_test(client, username=username, password=password)
            args, kwargs = run.await_args
            self.assertEqual(args[:2], ("http://testserver", ["AT4"]))
            self.assertEqual(args[2], backend.state["config"])
            self.assertIsNot(args[2], backend.state["config"])
            self.assertEqual(set(kwargs), {"progress", "source_snapshot", "test_mode"})
            self.assertEqual(kwargs["test_mode"], "full")
            public_data = (json.dumps(snapshot) + json.dumps(args) +
                           json.dumps(kwargs["source_snapshot"]) + client.get("/api/config").text)
            self.assertNotIn(username, public_data)
            self.assertNotIn(password, public_data)
            live_adapter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
