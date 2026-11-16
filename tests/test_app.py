"""Backend tests with synthetic students; never contact the real portal."""
import asyncio
import copy
import importlib
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from test_importer import at4_rows, make_xlsx

backend = importlib.import_module("notasync.app")
FIXTURE_TEST_ID = "fixture-local-test"


def approve_local_test(assessments):
    """Seed current proof only for tests whose subject is the later launch flow."""
    backend.state["validation"] = {"status": "passed", "assessments": list(assessments),
                                   "test_id": FIXTURE_TEST_ID,
                                   "fingerprint": backend.test_fingerprint(assessments),
                                   "checks": [{"id": code, "status": "passed"} for code in
                                              ("browser", "login", "classes", "columns", "fill", "save", "reload",
                                               "preservation", "stale", "cancelled", "complete")],
                                   "scope": "local_simulation",
                                   "test_mode": "full",
                                   "source_mode": "synthetic_demo" if backend.state["demo"] else "imported_sheet"}
    return FIXTURE_TEST_ID


def student(ra="50000001", value=8.5, **updates):
    return {"key": ra, "registration": ra, "name": "Aluno Fictício", "class_name": "1FID",
            "discipline": "AT4", "grade": value, "status": "Matriculado", "issues": [],
            "source_cell": "G2", "eligible": True, **updates}


def imported(students, mode="AT4", filename="exemplo.xlsx"):
    return {"id": filename, "filename": filename, "mode": mode,
            "sheets": [{"sheet": "1ºS INFO", "class_name": "1FID", "discipline": mode,
                        "students": students, "issues": []}], "issues": []}


class BackendStateTest(unittest.TestCase):
    def setUp(self):
        self.original = dict(backend.state)
        backend.state.clear()
        backend.state.update(imports=[], portal={"status": "connected", "message": "Exemplo", "classes": []},
                             jobs=[], demo=True, config={"target_year": 2026,
                             "discipline_aliases": {"REDES": "Redes de Computadores", "BD": "Banco de Dados"}},
                             plan=None, revision=0, audit=[], adapter=None, task=None, demo_rosters={},
                             _test_adapter=None, validation={"status": "idle", "checks": [], "test_id": None})

    def tearDown(self):
        backend.state.clear()
        backend.state.update(self.original)

    def setup_offerings(self, sources=None):
        backend.state["imports"] = sources or [imported([student()])]
        backend.state["portal"]["classes"] = [
            {"id": "redes", "class_name": "INF-1A", "discipline": "Redes de Computadores", "year": "Ano 2026"},
            {"id": "bd", "class_name": "INF-1A", "discipline": "Banco de Dados", "year": "Ano 2026"},
        ]
        backend.state["demo_rosters"] = {name: [
            {"registration": "50000001", "name": "Nome diferentemente grafado", "status": "Matriculado", "grade": 6},
            {"registration": "50000002", "name": "Aluno Fictício", "status": "Matriculado", "grade": 9},
        ] for name in ("redes", "bd")}

    def compare(self, mode="AT4", ids=None):
        return asyncio.run(backend.build_plan(ids or [], mode, {}))

    def launch_fixture(self, login_result=None, login_error=None):
        self.setup_offerings()
        backend.state["demo"] = False
        captured = {"instances": [], "read": [], "saved": [], "opens": [], "discover": 0}
        offerings = copy.deepcopy(backend.state["portal"]["classes"])
        rosters = copy.deepcopy(backend.state["demo_rosters"])
        class FakeAdapter:
            def __init__(self, config, data_dir):
                self.config = copy.deepcopy(config)
                self.closed = False
                captured["instances"].append(self)
            async def open(self, username, password):
                captured["opens"].append((username, password))
                if login_error:
                    raise ValueError(login_error)
                return login_result or {"status": "opened", "message": "Conectado"}
            async def discover(self):
                captured["discover"] += 1
                return copy.deepcopy(offerings)
            async def read_offering(self, offering):
                captured["read"].append(offering["id"])
                return copy.deepcopy(rosters[offering["id"]])
            async def apply_offering(self, offering, entries):
                captured["saved"].append((offering["id"], copy.deepcopy(entries)))
                for entry in entries:
                    for row in rosters[offering["id"]]:
                        if row["registration"] == entry["registration"]:
                            row["grade"] = entry["new"]
                return [{**entry, "status": "confirmed", "verified": True} for entry in entries]
            async def close(self):
                self.closed = True
        return FakeAdapter, captured, rosters

    def post_launch(self, client, **updates):
        body = {"username": "coordenador_exemplo", "password": "senha_exemplo_privada",
                "mode": "AT4", "show_browser": False, **updates}
        body["test_id"] = approve_local_test(body.get("assessments", [body["mode"]]))
        response = client.post("/api/launch", json=body,
                               headers={"x-notasync-token": backend.TOKEN})
        self.assertEqual(response.status_code, 200, response.text)
        jobs = client.get("/api/state").json()["jobs"]
        return jobs[-1]

    def test_at4_replicates_to_all_disciplines_by_exact_registration(self):
        self.setup_offerings()
        result = self.compare()
        self.assertEqual(len(result["changes"]), 2)
        self.assertEqual({row["discipline"] for row in result["changes"]}, {"Redes de Computadores", "Banco de Dados"})
        self.assertTrue(all(row["registration"] == "50000001" and row["new"] == 8.5 for row in result["changes"]))
        self.assertFalse(any(row["registration"] == "50000002" for row in result["changes"]))

    def test_zero_is_an_actual_grade(self):
        self.setup_offerings([imported([student(value=0)])])
        result = self.compare()
        self.assertEqual([row["new"] for row in result["changes"]], [0, 0])

    def test_cancelled_source_does_not_block_same_ra_active_elsewhere(self):
        cancelled = student(value=1, class_name="1AED", status="Cancelado", eligible=False,
                            issues=[{"code": "inactive_student", "message": "Aluno cancelado", "severity": "info"}])
        self.setup_offerings([imported([cancelled, student(value=9)])])
        result = self.compare()
        self.assertEqual(len(result["changes"]), 2)
        self.assertTrue(all(row["new"] == 9 for row in result["changes"]))

    def test_cancelled_target_never_receives_grade(self):
        self.setup_offerings()
        backend.state["demo_rosters"]["redes"][0]["status"] = "Cancelado"
        result = self.compare()
        self.assertEqual([row["class_id"] for row in result["changes"]], ["bd"])
        self.assertTrue(any("Situação da matrícula" in row["reason"] for row in result["blocked"]))

    def test_warning_only_importer_row_remains_eligible(self):
        warning = {"code": "blank_components", "message": "Fórmula da planilha considera componentes vazios como zero.", "severity": "warning"}
        self.setup_offerings([imported([student(issues=[warning])])])
        result = self.compare()
        self.assertEqual(len(result["changes"]), 2)
        self.assertTrue(all(row.get("warnings") for row in result["changes"]))

    def test_conflicting_sources_are_blocked(self):
        self.setup_offerings([imported([student(value=8)], filename="primeira.xlsx"),
                              imported([student(value=9)], filename="segunda.xlsx")])
        result = self.compare()
        self.assertEqual(result["changes"], [])
        self.assertTrue(any("divergentes" in row["reason"] for row in result["blocked"]))

    def test_a14_stays_in_its_own_discipline(self):
        self.setup_offerings([imported([student(value=4, discipline="REDES"),
                                       student(value=9, discipline="BD")], mode="A14")])
        result = self.compare("A14")
        self.assertEqual({row["class_id"]: row["new"] for row in result["changes"]}, {"redes": 4, "bd": 9})
        self.assertTrue(all(row["column"] == "A14" for row in result["changes"]))

    def test_header_conflict_from_importer_is_blocked(self):
        issue = {"code": "discipline_header_conflict", "message": "Cabeçalho A14 - BD contém PROVA/LISTA PDM.", "severity": "error"}
        self.setup_offerings([imported([student(value=10, discipline="BD", eligible=False, issues=[issue])], mode="A14")])
        result = self.compare("A14")
        self.assertFalse(result["changes"])
        self.assertTrue(any("PDM" in row["reason"] for row in result["blocked"]))

    def test_duplicate_portal_registration_is_blocked(self):
        self.setup_offerings()
        backend.state["demo_rosters"]["redes"].append(copy.deepcopy(backend.state["demo_rosters"]["redes"][0]))
        result = self.compare()
        self.assertEqual([row["class_id"] for row in result["changes"]], ["bd"])

    def test_wrong_year_offering_is_blocked(self):
        self.setup_offerings()
        backend.state["portal"]["classes"][0]["year"] = "Ano 2025"
        result = self.compare()
        self.assertEqual([row["class_id"] for row in result["changes"]], ["bd"])

    def test_source_revision_change_during_comparison_rejects_plan(self):
        self.setup_offerings()
        backend.state["demo"] = False
        class ChangedDuringRead:
            config = {}
            async def read_offering(self, offering):
                backend.state["revision"] += 1
                return backend.state["demo_rosters"][offering["id"]]
        backend.state["adapter"] = ChangedDuringRead()
        with self.assertRaisesRegex(ValueError, "alterada durante"):
            self.compare()
        self.assertIsNone(backend.state["plan"])

    def test_http_import_and_previous_plan_invalidated(self):
        backend.state["demo"] = False
        backend.state["plan"] = {"plan_id": "old", "revision": 0, "consumed": False}
        with TestClient(backend.app) as client:
            response = client.post("/api/import", headers={"x-notasync-token": backend.TOKEN},
                                   files=[("files", ("exemplo.xlsx", make_xlsx([("1ºS INFO", at4_rows())]),
                                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
                                   data={"mode": "AT4"})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()["imports"][0]
            self.assertEqual(result["summary"]["valid"], 1)
            self.assertEqual(result["sheets"][0]["students"][0]["grade"], 10)
            self.assertIsNone(backend.state["plan"])
            self.assertEqual(backend.state["revision"], 1)

    def test_stale_revision_cannot_execute(self):
        self.setup_offerings()
        plan = self.compare()
        backend.state["revision"] += 1
        with TestClient(backend.app) as client:
            response = client.post("/api/portal/execute", json={"plan_id": plan["plan_id"]},
                                   headers={"x-notasync-token": backend.TOKEN})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(backend.state["demo_rosters"]["redes"][0]["grade"], 6)

    def test_consumed_plan_cannot_execute_twice(self):
        self.setup_offerings()
        plan = self.compare()
        backend.state["plan"]["consumed"] = True
        with TestClient(backend.app) as client:
            response = client.post("/api/portal/execute", json={"plan_id": plan["plan_id"]},
                                   headers={"x-notasync-token": backend.TOKEN})
            self.assertEqual(response.status_code, 409)

    def test_http_import_requires_local_token(self):
        with TestClient(backend.app) as client:
            response = client.post("/api/import", files=[("files", ("exemplo.xlsx", b"bad"))])
            self.assertEqual(response.status_code, 403)

    def test_confirmed_adapter_results_finish_all_offerings(self):
        self.setup_offerings()
        plan = self.compare()
        backend.state["demo"] = False
        calls = []
        class ConfirmedAdapter:
            async def apply_offering(self, offering, entries):
                calls.append(offering["id"])
                return [{**entry, "status": "confirmed", "verified": True} for entry in entries]
            async def close(self):
                pass
        backend.state["adapter"] = ConfirmedAdapter()
        test_id = approve_local_test(["AT4"])
        with patch.object(backend, "audit"), TestClient(backend.app) as client:
            response = client.post("/api/portal/execute", json={"plan_id": plan["plan_id"], "test_id": test_id},
                                   headers={"x-notasync-token": backend.TOKEN})
            self.assertEqual(response.status_code, 200)
            jobs = client.get("/api/state").json()["jobs"]
            self.assertEqual(jobs[-1]["status"], "completed", jobs)
            self.assertEqual(calls, ["redes", "bd"])
            self.assertTrue(backend.state["plan"]["consumed"])

    def test_unconfirmed_adapter_result_stops_before_next_offering(self):
        self.setup_offerings()
        plan = self.compare()
        backend.state["demo"] = False
        calls = []
        class UnconfirmedAdapter:
            async def apply_offering(self, offering, entries):
                calls.append(offering["id"])
                return [{**entry, "status": "unknown", "verified": False} for entry in entries]
            async def close(self):
                pass
        backend.state["adapter"] = UnconfirmedAdapter()
        test_id = approve_local_test(["AT4"])
        with patch.object(backend, "audit"), TestClient(backend.app) as client:
            response = client.post("/api/portal/execute", json={"plan_id": plan["plan_id"], "test_id": test_id},
                                   headers={"x-notasync-token": backend.TOKEN})
            self.assertEqual(response.status_code, 200)
            jobs = client.get("/api/state").json()["jobs"]
            self.assertEqual(jobs[-1]["status"], "failed", jobs)
            self.assertEqual(calls, ["redes"])
            self.assertTrue(backend.state["plan"]["consumed"])

    def test_one_click_background_login_reads_and_saves_all_offerings(self):
        fake, captured, _ = self.launch_fixture()
        config_before = copy.deepcopy(backend.state["config"])
        with patch.object(backend, "PortalAdapter", fake), patch.object(backend, "audit"), \
                patch.object(backend, "notify_finished") as notified, TestClient(backend.app) as client:
            job = self.post_launch(client)
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(job["kind"], "launch")
            self.assertEqual(job["stage"], "finished")
            self.assertEqual(job["summary"]["saved"], 2)
            self.assertEqual(captured["read"], ["redes", "bd"])
            self.assertEqual([offering for offering, _ in captured["saved"]], ["redes", "bd"])
            self.assertEqual(captured["opens"], [("coordenador_exemplo", "senha_exemplo_privada")])
            self.assertTrue(captured["instances"][0].config["headless"])
            self.assertTrue(captured["instances"][0].closed)
            self.assertIsNone(backend.state["adapter"])
            self.assertTrue(backend.state["plan"]["consumed"])
            self.assertEqual(backend.state["config"], config_before)
            notified.assert_called_once_with(job["message"], failed=False)
            snapshot = client.get("/api/state").text + client.get("/api/config").text + client.get("/api/plan").text
            self.assertNotIn("senha_exemplo_privada", snapshot)
            self.assertNotIn("coordenador_exemplo", snapshot)

    def test_visible_launch_keeps_browser_open_and_does_not_popup(self):
        fake, captured, _ = self.launch_fixture()
        with patch.object(backend, "PortalAdapter", fake), patch.object(backend, "audit"), \
                patch.object(backend, "notify_finished") as notified, TestClient(backend.app) as client:
            job = self.post_launch(client, show_browser=True)
            self.assertEqual(job["status"], "completed", job)
            self.assertFalse(captured["instances"][0].config["headless"])
            self.assertFalse(captured["instances"][0].closed)
            notified.assert_not_called()

    def test_hidden_incomplete_login_stops_once_and_requests_visible_retry(self):
        fake, captured, _ = self.launch_fixture(login_result={"status": "waiting_login"})
        with patch.object(backend, "PortalAdapter", fake), patch.object(backend, "audit"), \
                patch.object(backend, "notify_finished") as notified, TestClient(backend.app) as client:
            job = self.post_launch(client)
            self.assertEqual(job["status"], "failed", job)
            self.assertIn("Marque Mostrar navegador", job["message"])
            self.assertEqual(backend.state["portal"]["status"], "waiting_login")
            self.assertEqual(len(captured["opens"]), 1)
            self.assertEqual(captured["discover"], 0)
            self.assertEqual(captured["saved"], [])
            self.assertTrue(captured["instances"][0].closed)
            notified.assert_called_once_with(job["message"], failed=True)

    def test_login_error_cannot_expose_credentials(self):
        fake, captured, _ = self.launch_fixture(login_error="Falha para coordenador_exemplo com senha_exemplo_privada")
        with patch.object(backend, "PortalAdapter", fake), patch.object(backend, "audit"), \
                patch.object(backend, "notify_finished") as notified, TestClient(backend.app) as client:
            job = self.post_launch(client)
            self.assertEqual(job["status"], "failed", job)
            self.assertNotIn("senha_exemplo_privada", job["message"])
            self.assertNotIn("coordenador_exemplo", job["message"])
            self.assertEqual(captured["discover"], 0)
            self.assertEqual(captured["saved"], [])
            notified.assert_called_once_with(job["message"], failed=True)

    def test_launch_without_source_never_opens_portal(self):
        backend.state["demo"] = False
        with patch.object(backend, "PortalAdapter") as adapter, TestClient(backend.app) as client:
            response = client.post("/api/launch", json={"username": "exemplo", "password": "segredo", "mode": "AT4"},
                                   headers={"x-notasync-token": backend.TOKEN})
            self.assertEqual(response.status_code, 400)
            adapter.assert_not_called()

    def test_launch_zero_changes_completes_without_saves(self):
        fake, captured, rosters = self.launch_fixture()
        for roster in rosters.values():
            roster[0]["grade"] = 8.5
        with patch.object(backend, "PortalAdapter", fake), patch.object(backend, "audit"), \
                patch.object(backend, "notify_finished"), TestClient(backend.app) as client:
            job = self.post_launch(client)
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(job["summary"], {"saved": 0, "unchanged": 2, "pending": 0, "classes": 2})
            self.assertIn("nenhuma nota para atualizar", job["message"])
            self.assertFalse(captured["saved"])

    def test_relaunch_recompares_and_does_not_repeat_equal_grades(self):
        fake, captured, _ = self.launch_fixture()
        with patch.object(backend, "PortalAdapter", fake), patch.object(backend, "audit"), \
                patch.object(backend, "notify_finished"), TestClient(backend.app) as client:
            first = self.post_launch(client)
            second = self.post_launch(client)
            self.assertEqual(first["summary"]["saved"], 2)
            self.assertEqual(second["summary"]["saved"], 0)
            self.assertEqual(second["summary"]["unchanged"], 2)
            self.assertEqual(len(captured["saved"]), 2)
            self.assertEqual(captured["discover"], 2)

    def test_demo_launch_works_with_empty_credentials_without_portal(self):
        self.setup_offerings()
        with patch.object(backend, "PortalAdapter") as adapter, patch.object(backend, "notify_finished"), \
                TestClient(backend.app) as client:
            job = self.post_launch(client, username="", password="")
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(job["summary"]["saved"], 2)
            self.assertIn("Nenhum portal foi alterado", job["message"])
            adapter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
