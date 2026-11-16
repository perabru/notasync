"""Mixed-bimester backend tests with synthetic workbooks and a local fake portal."""
import copy
import importlib
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from notasync.importer import SUPPORTED_ASSESSMENTS
from test_app import approve_local_test
from test_importer import make_xlsx

backend = importlib.import_module("notasync.app")


def grade_workbook(codes=SUPPORTED_ASSESSMENTS, rows=None):
    columns = {code: chr(70 + index) for index, code in enumerate(codes)}
    headers = {"A": "Código", "B": "Aluno", "C": "Turma", "D": "Disciplina", "E": "Situação"}
    headers.update({column: code for code, column in columns.items()})
    source_rows = {1: headers}
    records = rows or [{"registration": 50000001, "name": "Aluno Fictício", "status": "Matriculado",
                        "grades": {code: (int(code[-1]) + (0.1 if code.startswith("AT") else 0.2)) for code in codes}}]
    for index, row in enumerate(records, 2):
        cells = {"A": row["registration"], "B": row.get("name", "Aluno Fictício"), "C": "1F",
                 "D": "Banco de Dados", "E": row.get("status", "Matriculado")}
        cells.update({columns[code]: value for code, value in row["grades"].items() if code in columns and value is not None})
        source_rows[index] = cells
    return make_xlsx([("Notas 1F", source_rows)])


class MultiAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(backend.state)
        backend.state.clear()
        backend.state.update(imports=[], portal={"status": "disconnected", "message": "Exemplo", "classes": []},
                             jobs=[], demo=False, config={"target_year": 2026, "grade_column": "AT4",
                             "discipline_aliases": {"BD": "Banco de Dados", "REDES": "Redes de Computadores"}},
                             plan=None, revision=0, audit=[], adapter=None, task=None,
                             _test_adapter=None, validation={"status": "idle", "checks": [], "test_id": None})

    def tearDown(self):
        backend.state.clear()
        backend.state.update(self.original)

    def upload(self, client, files, mode="AUTO"):
        response = client.post("/api/import", headers={"x-notasync-token": backend.TOKEN},
                               files=[("files", (name, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
                                      for name, data in files], data={"mode": mode})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def portal_fixture(self, columns_by_class=None, codes=SUPPORTED_ASSESSMENTS):
        columns_by_class = columns_by_class or {"bd": list(codes), "redes": list(codes)}
        offerings = [{"id": "bd", "class_name": "INF-1A", "discipline": "Banco de Dados", "year": "2026"},
                     {"id": "redes", "class_name": "INF-1A", "discipline": "Redes de Computadores", "year": "2026"}]
        captured = {"instances": [], "inspect": [], "reads": [], "writes": []}
        current = {offering["id"]: {"50000001": {code: 9.9 for code in codes},
                                    "50000002": {code: 9.9 for code in codes}} for offering in offerings}
        class FakeAdapter:
            def __init__(self, config, directory=None):
                self.config = copy.deepcopy(config)
                captured["instances"].append(self)
            async def open(self, username, password):
                return {"status": "opened", "message": "Exemplo conectado"}
            async def discover(self):
                return copy.deepcopy(offerings)
            async def inspect_offering(self, offering):
                captured["inspect"].append(offering["id"])
                assessment_columns = columns_by_class[offering["id"]]
                return {"assessments": list(assessment_columns),
                        "headers": ["Aluno", "Código", "Situação", *assessment_columns, "C-AT1", "MB1", "MBF"]}
            async def read_offering(self, offering):
                code = self.config["grade_column"]
                if code not in columns_by_class[offering["id"]]:
                    raise AssertionError("A coluna inexistente não deve ser lida")
                captured["reads"].append((offering["id"], code))
                return [{"registration": ra, "name": "Aluno Fictício", "status": "Matriculado", "grade": grades[code]}
                        for ra, grades in current[offering["id"]].items()]
            async def apply_offering(self, offering, entries):
                code = self.config["grade_column"]
                # Fail before mutation if the caller mixes grade columns in a batch.
                if any(entry["column"] != code for entry in entries):
                    raise AssertionError("Um lote misturou avaliações diferentes")
                if code not in columns_by_class[offering["id"]]:
                    raise AssertionError("Tentativa de escrita em avaliação inexistente")
                captured["writes"].append((offering["id"], code, copy.deepcopy(entries)))
                for entry in entries:
                    current[offering["id"]][entry["registration"]][code] = entry["new"]
                return [{**entry, "status": "confirmed", "verified": True} for entry in entries]
            async def close(self):
                pass
        return FakeAdapter, captured, current

    def launch(self, client, codes):
        test_id = approve_local_test(codes)
        self.assertEqual(backend.state["validation"]["source_mode"], "imported_sheet")
        response = client.post("/api/launch", headers={"x-notasync-token": backend.TOKEN},
                               json={"username": "coordenador_ficticio", "password": "senha_ficticia",
                                     "assessments": list(codes), "show_browser": False, "test_id": test_id})
        self.assertEqual(response.status_code, 200, response.text)
        return client.get("/api/state").json()["jobs"][-1]

    def test_auto_upload_exposes_all_eight_source_assessments(self):
        with TestClient(backend.app) as client:
            imported = self.upload(client, [("todos.xlsx", grade_workbook())])["imports"][0]
            self.assertEqual(imported["available_assessments"], list(SUPPORTED_ASSESSMENTS))
            self.assertEqual(imported["summary"]["records"], 8)
            available = {entry["code"] for entry in client.get("/api/assessments").json() if entry["available"]}
            self.assertEqual(available, set(SUPPORTED_ASSESSMENTS))

    def test_multiple_auto_uploads_retain_distinct_files_and_codes(self):
        with TestClient(backend.app) as client:
            self.upload(client, [("primeiro.xlsx", grade_workbook(("AT1", "A11"))),
                                 ("segundo.xlsx", grade_workbook(("AT2", "A12")))])
            self.upload(client, [("terceiro.xlsx", grade_workbook(("AT3", "A13")))])
            state = client.get("/api/state").json()
            self.assertEqual({item["filename"] for item in state["imports"]}, {"primeiro.xlsx", "segundo.xlsx", "terceiro.xlsx"})
            available = {entry["code"] for entry in state["assessments"] if entry["available"]}
            self.assertEqual(available, {"AT1", "A11", "AT2", "A12", "AT3", "A13"})

    def test_catalogue_never_enables_assessments_absent_from_sources(self):
        with TestClient(backend.app) as client:
            self.upload(client, [("quarto.xlsx", grade_workbook(("AT4",)))])
            catalogue = client.get("/api/assessments").json()
            self.assertEqual([item["code"] for item in catalogue if item["available"]], ["AT4"])
            self.assertTrue(all(item["source_records"] == 0 for item in catalogue if item["code"] != "AT4"))

    def test_launch_all_eight_uses_correct_column_and_subject_scope(self):
        fake, captured, current = self.portal_fixture()
        with patch.object(backend, "PortalAdapter", fake), patch.object(backend, "audit"), \
                patch.object(backend, "notify_finished"), TestClient(backend.app) as client:
            self.upload(client, [("todos.xlsx", grade_workbook())])
            job = self.launch(client, SUPPORTED_ASSESSMENTS)
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(set(captured["inspect"]), {"bd", "redes"})
            self.assertEqual(job["summary"]["saved"], 8)
            writes = {(offering, code) for offering, code, _ in captured["writes"]}
            self.assertEqual(writes, {("bd", code) for code in SUPPORTED_ASSESSMENTS})
            for offering, code, entries in captured["writes"]:
                self.assertTrue(all(entry["registration"] == "50000001" and entry["column"] == code for entry in entries))
                expected_cell = chr(70 + SUPPORTED_ASSESSMENTS.index(code)) + "2"
                self.assertTrue(all(entry["source_cell"] == expected_cell for entry in entries))
                expected = int(code[-1]) + (0.1 if code.startswith("AT") else 0.2)
                self.assertAlmostEqual(current[offering]["50000001"][code], expected)
                self.assertEqual(current[offering]["50000002"][code], 9.9)
            self.assertTrue(all(current["redes"]["50000001"][code] == 9.9 for code in SUPPORTED_ASSESSMENTS))
            self.assertFalse(any(code in ("C-AT1", "MB1", "MBF") for _, code, _ in captured["writes"]))

    def test_launch_request_cannot_relabel_at4_source_as_at1(self):
        with patch.object(backend, "PortalAdapter") as adapter, TestClient(backend.app) as client:
            self.upload(client, [("notas.xlsx", grade_workbook(("AT4",)))])
            response = client.post("/api/launch", headers={"x-notasync-token": backend.TOKEN},
                                   json={"username": "exemplo", "password": "exemplo", "assessments": ["AT1"]})
            self.assertEqual(response.status_code, 400)
            adapter.assert_not_called()

    def test_portal_missing_selected_column_is_blocked_while_other_columns_save(self):
        fake, captured, _ = self.portal_fixture({"bd": ["AT1"], "redes": ["AT1", "AT2"]}, codes=("AT1", "AT2"))
        with patch.object(backend, "PortalAdapter", fake), patch.object(backend, "audit"), \
                patch.object(backend, "notify_finished"), TestClient(backend.app) as client:
            self.upload(client, [("projetos.xlsx", grade_workbook(("AT1", "AT2")))])
            job = self.launch(client, ("AT1", "AT2"))
            self.assertEqual(job["summary"]["saved"], 1, job)
            self.assertGreater(job["summary"]["pending"], 0)
            self.assertNotIn(("redes", "AT2"), captured["reads"])
            self.assertNotIn(("redes", "AT2"), [(offering, code) for offering, code, _ in captured["writes"]])
            self.assertNotIn(("bd", "AT2"), captured["reads"])
            blocked = client.get("/api/plan").json()["blocked"]
            self.assertTrue(any(row.get("column") == "AT2" and "não existe" in row["reason"] for row in blocked))

    def test_catalogue_matches_source_and_inspected_portal_intersection(self):
        fake, _, _ = self.portal_fixture({"bd": ["AT1"], "redes": ["AT1"]}, codes=("AT1", "AT2"))
        with patch.object(backend, "PortalAdapter", fake), patch.object(backend, "audit"), \
                patch.object(backend, "notify_finished"), TestClient(backend.app) as client:
            self.upload(client, [("projetos.xlsx", grade_workbook(("AT1", "AT2")))])
            self.launch(client, ("AT1",))
            catalogue = {item["code"]: item for item in client.get("/api/assessments").json()}
            self.assertTrue(catalogue["AT1"]["available"])
            self.assertTrue(catalogue["AT1"]["portal_available"])
            self.assertEqual(catalogue["AT2"]["source_records"], 1)
            self.assertFalse(catalogue["AT2"]["portal_available"])
            self.assertFalse(catalogue["AT2"]["available"])

    def test_no_matching_portal_column_reports_pending_and_never_claims_verified_updates(self):
        fake, captured, _ = self.portal_fixture({"bd": ["AT4"], "redes": ["AT4"]}, codes=("AT1", "AT4"))
        with patch.object(backend, "PortalAdapter", fake), patch.object(backend, "audit"), \
                patch.object(backend, "notify_finished"), TestClient(backend.app) as client:
            self.upload(client, [("primeiro.xlsx", grade_workbook(("AT1",)))])
            job = self.launch(client, ("AT1",))
            self.assertFalse(captured["writes"])
            self.assertFalse(captured["reads"])
            plan = client.get("/api/plan").json()
            self.assertFalse(plan["changes"])
            self.assertTrue(plan["blocked"])
            self.assertIn("pendências", job["message"], job)
            self.assertNotIn("Lançamento concluído e conferido", job["message"])

    def test_missing_student_grade_is_not_sent_for_that_assessment(self):
        codes = ("A11", "A12")
        rows = [{"registration": 50000001, "grades": {"A11": 4.2, "A12": None}},
                {"registration": 50000002, "grades": {"A11": 5.2, "A12": 6.2}}]
        fake, captured, current = self.portal_fixture(codes=codes)
        with patch.object(backend, "PortalAdapter", fake), patch.object(backend, "audit"), \
                patch.object(backend, "notify_finished"), TestClient(backend.app) as client:
            self.upload(client, [("provas.xlsx", grade_workbook(codes, rows=rows))])
            job = self.launch(client, codes)
            self.assertEqual(job["summary"]["saved"], 3, job)
            a12 = [entry for offering, code, entries in captured["writes"] if code == "A12" for entry in entries]
            self.assertEqual([entry["registration"] for entry in a12], ["50000002"])
            self.assertEqual(current["bd"]["50000001"]["A12"], 9.9)
            self.assertGreater(job["summary"]["pending"], 0)

    def test_repeated_multi_launch_does_not_rewrite_unchanged_codes(self):
        fake, captured, _ = self.portal_fixture()
        with patch.object(backend, "PortalAdapter", fake), patch.object(backend, "audit"), \
                patch.object(backend, "notify_finished"), TestClient(backend.app) as client:
            self.upload(client, [("todos.xlsx", grade_workbook())])
            first = self.launch(client, SUPPORTED_ASSESSMENTS)
            written = len(captured["writes"])
            second = self.launch(client, SUPPORTED_ASSESSMENTS)
            self.assertEqual(first["summary"]["saved"], 8, first)
            self.assertEqual(second["summary"]["saved"], 0, second)
            self.assertEqual(second["summary"]["unchanged"], 8)
            self.assertEqual(len(captured["writes"]), written)

    def test_nonwritable_column_codes_cannot_be_requested(self):
        with patch.object(backend, "PortalAdapter") as adapter, TestClient(backend.app) as client:
            self.upload(client, [("todos.xlsx", grade_workbook())])
            for code in ("C-AT1", "MB1", "MBF", "A15"):
                response = client.post("/api/launch", headers={"x-notasync-token": backend.TOKEN},
                                       json={"username": "exemplo", "password": "exemplo", "assessments": [code]})
                self.assertEqual(response.status_code, 400, (code, response.text))
            adapter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
