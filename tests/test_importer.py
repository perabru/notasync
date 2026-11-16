from io import BytesIO
import unittest
from xml.sax.saxutils import escape
from zipfile import ZipFile

from notasync.importer import SUPPORTED_ASSESSMENTS, WorkbookError, analyze_workbook, detect_assessment, registration


def make_xlsx(sheets, dimension=None):
    """Tiny synthetic in-memory fixture, with no real student data."""
    buf = BytesIO()
    with ZipFile(buf, "w") as archive:
        nodes, relationships = [], []
        for index, (title, rows) in enumerate(sheets, 1):
            nodes.append(f'<sheet name="{escape(title)}" sheetId="{index}" r:id="rId{index}"/>')
            relationships.append(f'<Relationship Id="rId{index}" Type="worksheet" Target="worksheets/sheet{index}.xml"/>')
            row_nodes = []
            for rownum, cells in rows.items():
                cell_nodes = []
                for col, value in cells.items():
                    ref = col + str(rownum)
                    if isinstance(value, dict):
                        attrs = ' t="shared" si="1"' if value.get("shared") else ""
                        f = f'<f{attrs}>{escape(value.get("f", ""))}</f>'
                        cache = f'<v>{value["v"]}</v>' if "v" in value else ""
                        cell_nodes.append(f'<c r="{ref}">{f}{cache}</c>')
                    elif isinstance(value, (int, float)):
                        cell_nodes.append(f'<c r="{ref}"><v>{value}</v></c>')
                    else:
                        cell_nodes.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
                row_nodes.append(f'<row r="{rownum}">{"".join(cell_nodes)}</row>')
            dimension_node = '<dimension ref="' + escape(dimension) + '"/>' if dimension else ''
            archive.writestr(f"xl/worksheets/sheet{index}.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + dimension_node + '<sheetData>' + "".join(row_nodes) + '</sheetData></worksheet>')
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + "".join(nodes) + '</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships>' + "".join(relationships) + '</Relationships>')
    return buf.getvalue()


def at4_rows():
    return {1: {"A": "Código", "B": "Aluno", "C": "TURMA", "D": "REDES", "E": "JOGOS", "F": "FEIRA", "G": "MÉDIA AT4"}, 2: {"A": 50000001, "B": "Aluno Teste - Matriculado", "C": "1FID", "D": 8, "E": 10, "F": 1, "G": {"f": "IF((D2+E2)/2+F2>10,10,(D2+E2)/2+F2)", "v": 10}}}


class ImporterTests(unittest.TestCase):
    def analyze(self, rows=None):
        return analyze_workbook(make_xlsx([("1ºS INFO", rows or at4_rows())]), "teste.xlsx")

    def student(self, rows=None):
        return self.analyze(rows)["sheets"][0]["students"][0]

    def test_recalculates_stale_formula_cache(self):
        rows = at4_rows()
        rows[2]["E"] = 4
        result = self.student(rows)
        self.assertEqual(result["grade"], 7)
        self.assertTrue(result["eligible"])
        self.assertIn("cache_recalculated", [i["code"] for i in result["issues"]])

    def test_blank_project_follows_explicit_arithmetic(self):
        rows = at4_rows()
        del rows[2]["E"]
        result = self.student(rows)
        self.assertEqual(result["grade"], 5)
        self.assertTrue(result["eligible"])
        self.assertIn("blank_components", [i["code"] for i in result["issues"]])

    def test_all_projects_blank_not_invented_grade(self):
        rows = at4_rows()
        del rows[2]["D"], rows[2]["E"]
        result = self.student(rows)
        self.assertIsNone(result["grade"])
        self.assertFalse(result["eligible"])

    def test_legitimate_zero_remains_zero(self):
        rows = at4_rows()
        rows[2].update(D=0, E=0, F=0)
        self.assertEqual(self.student(rows)["grade"], 0)
        self.assertTrue(self.student(rows)["eligible"])

    def test_cancelled_is_ineligible(self):
        rows = at4_rows()
        rows[2]["B"] = "Aluno Teste - Cancelado"
        self.assertFalse(self.student(rows)["eligible"])
        self.assertEqual(self.analyze(rows)["summary"]["inactive"], 1)

    def test_shared_formulas_translate_rows(self):
        rows = at4_rows()
        rows[2]["G"]["shared"] = True
        rows[3] = {"A": 50000002, "B": "Aluno Dois - Matriculado", "C": "1FID", "D": 4, "E": 6, "F": 1, "G": {"shared": True, "v": 99}}
        students = self.analyze(rows)["sheets"][0]["students"]
        self.assertEqual(students[1]["grade"], 6)
        self.assertIn("D3", students[1]["source_formula"])

    def test_missing_final_is_pending(self):
        rows = at4_rows()
        del rows[2]["G"]
        result = self.student(rows)
        self.assertIsNone(result["grade"])
        self.assertFalse(result["eligible"])

    def test_duplicate_registrations_block_both(self):
        rows = at4_rows()
        rows[3] = dict(rows[2], G=10)
        result = self.analyze(rows)
        self.assertEqual(result["summary"]["valid"], 0)
        self.assertTrue(all(any(issue["code"] == "duplicate_registration" for issue in student["issues"]) for student in result["sheets"][0]["students"]))

    def test_rejects_unsupported_formula_even_with_cache(self):
        rows = at4_rows()
        rows[2]["G"] = {"f": 'WEBSERVICE("https://invalid.test")', "v": 10}
        result = self.student(rows)
        self.assertIsNone(result["grade"])
        self.assertIn("formula_error", [i["code"] for i in result["issues"]])

    def test_lazy_if_does_not_evaluate_unused_division(self):
        rows = at4_rows()
        rows[2]["G"] = {"f": "IF(D2>0,10,1/0)", "v": 10}
        self.assertEqual(self.student(rows)["grade"], 10)

    def test_out_of_range(self):
        rows = at4_rows()
        rows[2]["G"] = 11
        self.assertFalse(self.student(rows)["eligible"])

    def test_a14_preserves_disciplines_and_stacked_classes(self):
        rows = {1: {"A": "1F INFORMÁTICA"}, 2: {"A": "Código", "B": "Aluno", "C": "LISTA REDES", "D": "PROVA REDES", "E": "A14 REDES"}, 3: {"A": 50000001, "B": "Aluno Teste - Matriculado", "C": 2, "D": 6, "E": {"f": "IF(D3>=6,IF(C3+D3>10,10,C3+D3),D3)", "v": 8}}, 5: {"A": "1H INFORMÁTICA"}, 6: {"A": "Código", "B": "Aluno", "C": "LISTA REDES", "D": "PROVA REDES", "E": "A14 REDES"}, 7: {"A": 50000002, "B": "Aluno Dois - Matriculado", "C": 2, "D": 5, "E": {"f": "IF(D7>=6,IF(C7+D7>10,10,C7+D7),D7)", "v": 5}}}
        result = analyze_workbook(make_xlsx([("1ºS INFO", rows)]), "teste.xlsx", "A14")
        self.assertEqual([entry["class_name"] for entry in result["sheets"]], ["1F", "1H"])
        self.assertEqual([entry["students"][0]["grade"] for entry in result["sheets"]], [8, 5])

    def test_scientific_identifiers(self):
        self.assertEqual(registration("9.9012001E7"), "99012001")
        self.assertEqual(registration("00123"), "00123")
        self.assertEqual(registration("123.5"), "")

    def test_bad_workbook(self):
        with self.assertRaises(WorkbookError):
            analyze_workbook(b"not xlsx", "bad.xlsx")

    def test_wrong_mode(self):
        with self.assertRaises(WorkbookError):
            analyze_workbook(b"", "bad.xlsx", "AT5")

    def test_assessment_detection_is_explicit_and_strict(self):
        for code in SUPPORTED_ASSESSMENTS:
            self.assertEqual(detect_assessment(code), code)
        for header, code in (("MÉDIA AT1", "AT1"), ("MÉDIA FINAL AT2", "AT2"),
                             ("A11 - BD", "A11"), ("A12: REDES", "A12"), ("A13 PDM", "A13")):
            self.assertEqual(detect_assessment(header), code)
        for header in ("MÉDIA", "Média Final", "MB1", "MBF", "C-A11", "C - AT1", "AT11", "A110", "AT1 REDES", "PROVA AT1", "AR1", "EF", "NC"):
            self.assertIsNone(detect_assessment(header), header)

    def test_auto_import_detects_all_eight_codes_without_calculated_columns(self):
        headers = {"A": "Código", "B": "Aluno", "C": "Turma", "D": "Disciplina",
                   "E": "A11", "F": "AT1", "G": "A12", "H": "MÉDIA AT2", "I": "A13",
                   "J": "AT3", "K": "A14", "L": "MÉDIA FINAL AT4", "M": "MB1", "N": "C-AT1", "O": "Média Final"}
        values = {"A": 50000001, "B": "Aluno Teste", "C": "1F", "D": "Redes de Computadores",
                  "E": 5, "F": 0, "G": 6, "H": 7, "I": 8, "J": 9, "K": 10, "L": 6.5, "M": 1, "N": "S", "O": 7}
        result = analyze_workbook(make_xlsx([("1F", {1: headers, 2: values})]), "notas.xlsx", "AUTO")
        self.assertEqual(result["available_assessments"], list(SUPPORTED_ASSESSMENTS))
        self.assertEqual(result["summary"]["records"], 8)
        self.assertEqual(result["summary"]["valid"], 8)
        self.assertEqual(set(result["summary"]["assessments"]), set(SUPPORTED_ASSESSMENTS))
        rows = [entry["students"][0] for entry in result["sheets"]]
        self.assertEqual({row["assessment"] for row in rows}, set(SUPPORTED_ASSESSMENTS))
        self.assertTrue(all(row["scope"] == "discipline" and row["discipline"] == "Redes de Computadores" for row in rows if row["assessment"].startswith("AT")))
        self.assertTrue(all(row["discipline"] == "Redes de Computadores" for row in rows if row["assessment"].startswith("A1")))
        self.assertEqual(next(row for row in rows if row["assessment"] == "AT1")["grade"], 0)

    def test_exact_mode_filters_source_present_code_without_relabeling(self):
        rows = at4_rows()
        content = make_xlsx([("1ºS INFO", rows)])
        absent = analyze_workbook(content, "NOTAS A11.xlsx", "A11")
        self.assertEqual(absent["available_assessments"], ["AT4"])
        self.assertFalse(absent["sheets"])
        self.assertEqual(absent["summary"]["records"], 0)
        actual = analyze_workbook(content, "NOTAS A11.xlsx", "AT4")
        self.assertEqual(actual["sheets"][0]["assessment"], "AT4")

    def test_earlier_project_formula_recalculates_and_honors_blank_semantics(self):
        for code in ("AT1", "AT2", "AT3"):
            rows = at4_rows()
            rows[1]["G"] = "MÉDIA " + code
            del rows[2]["E"]
            result = analyze_workbook(make_xlsx([("1ºS INFO", rows)]), "exemplo.xlsx", code)
            row = result["sheets"][0]["students"][0]
            self.assertEqual(row["assessment"], code)
            self.assertEqual(row["grade"], 5)
            self.assertTrue(row["eligible"])
            self.assertIn("blank_components", [issue["code"] for issue in row["issues"]])

    def test_earlier_exam_codes_keep_subject_suffix_and_formula(self):
        for code in ("A11", "A12", "A13"):
            rows = {1: {"A": "Código", "B": "Aluno", "C": "TURMA", "D": "LISTA BD", "E": "PROVA BD", "F": code + " - BD"},
                    2: {"A": 50000001, "B": "Aluno Teste - Matriculado", "C": "2F", "D": 2, "E": 6,
                        "F": {"f": "IF(E2>=6,MIN(10,D2+E2),E2)", "v": 4}}}
            result = analyze_workbook(make_xlsx([("2F", rows)]), "notas.xlsx", "AUTO")
            entry = result["sheets"][0]
            self.assertEqual(entry["assessment"], code)
            self.assertEqual(entry["discipline"], "BD")
            self.assertEqual(entry["students"][0]["grade"], 8)
            self.assertTrue(entry["students"][0]["eligible"])

    def test_generic_exam_needs_explicit_discipline(self):
        rows = {1: {"A": "Código", "B": "Aluno", "C": "TURMA", "D": "A11"},
                2: {"A": 50000001, "B": "Aluno Teste - Matriculado", "C": "1F", "D": 8}}
        result = analyze_workbook(make_xlsx([("1F", rows)]), "notas.xlsx", "AUTO")
        row = result["sheets"][0]["students"][0]
        self.assertEqual(row["discipline"], "")
        self.assertFalse(row["eligible"])
        self.assertIn("missing_discipline", [issue["code"] for issue in row["issues"]])

    def test_generic_exam_accepts_explicit_metadata_without_guessing_sheet_name(self):
        rows = {1: {"A": "Disciplina: Redes de Computadores"},
                2: {"A": "Código", "B": "Aluno", "C": "TURMA", "D": "A11"},
                3: {"A": 50000001, "B": "Aluno Teste - Matriculado", "C": "1F", "D": 8}}
        result = analyze_workbook(make_xlsx([("Notas", rows)]), "notas.xlsx", "AUTO")
        row = result["sheets"][0]["students"][0]
        self.assertTrue(row["eligible"])
        self.assertEqual(row["discipline"], "Redes de Computadores")

    def test_portal_export_status_column_keeps_cancelled_ineligible(self):
        rows = {1: {"A": "Aluno", "B": "Código", "C": "TURMA", "D": "Situação", "E": "AT1"},
                2: {"A": "Aluno Teste", "B": 50000001, "C": "1F", "D": "Cancelado", "E": 8}}
        result = analyze_workbook(make_xlsx([("Notas", rows)]), "notas.xlsx", "AUTO")
        self.assertFalse(result["sheets"][0]["students"][0]["eligible"])
        self.assertEqual(result["summary"]["inactive"], 1)

    def test_generic_average_and_filename_never_invent_assessment(self):
        rows = {1: {"A": "Código", "B": "Aluno", "C": "TURMA", "D": "MÉDIA"},
                2: {"A": 50000001, "B": "Aluno Teste", "C": "1F", "D": 8}}
        result = analyze_workbook(make_xlsx([("AT1", rows)]), "AT1.xlsx", "AUTO")
        self.assertEqual(result["available_assessments"], [])
        self.assertEqual(result["sheets"], [])

    def test_mixed_assessments_do_not_cross_contaminate_component_warnings(self):
        rows = {1: {"A": "Código", "B": "Aluno", "C": "TURMA", "D": "A11 - BD", "E": "AT1", "F": "A12 - BD", "G": "AT2"},
                2: {"A": 50000001, "B": "Aluno Teste - Matriculado", "C": "1F", "D": 8, "E": 7,
                    "G": {"f": "E2", "v": 7}}}
        result = analyze_workbook(make_xlsx([("Notas", rows)]), "notas.xlsx", "AUTO")
        row = next(entry["students"][0] for entry in result["sheets"] if entry["assessment"] == "AT2")
        self.assertTrue(row["eligible"])
        self.assertEqual([component["cell"] for component in row["components"]], ["E2"])
        self.assertFalse(row["issues"])


if __name__ == "__main__":
    unittest.main()
