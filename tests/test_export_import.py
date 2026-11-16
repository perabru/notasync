"""Synthetic portal-export fixtures; no real student records are bundled."""
import unittest

from test_importer import make_xlsx
from notasync.importer import SUPPORTED_ASSESSMENTS, analyze_workbook


def portal_export_rows():
    headers = ("Aluno", "Código", "Situação", "Curso", "A11", "AT1", "AR1", "MB1",
               "A12", "AT2", "AR2", "MB2", "A13", "AT3", "AR3", "MB3",
               "A14", "AT4", "AR4", "MB4", "MBF", "EF", "NC")
    rows = {1: {"A": "\n  Curso: Curso Técnico Exemplo  \n"},
            2: {"A": "\n Disciplina: M123456  Redes de Computadores \n"},
            3: {"A": "\n Turma: EConInf-1TESTE \n"},
            4: {"A": "\n Ano/Periodo: 2026/0 \n"},
            5: {"A": "Unidade Física: Unidade de Teste"},
            6: {"A": "Unidade Responsável: Ensino Técnico"},
            7: {"A": "Fórmula Critério Avaliação: MBF"},
            8: {"A": "Fórmula Média Final: MBF"},
            9: {chr(65 + index): "\n" + title + "\n" for index, title in enumerate(headers)}}
    for number, status in ((10, "Matriculado"), (11, "Cancelado")):
        values = {title: "" for title in headers}
        values.update(Aluno="Aluno de Teste " + str(number) + " - " + status,
                      Código="9000000" + str(number - 9), Situação=status, Curso="TESTE")
        if status == "Matriculado":
            values.update(A11="8,4", AT1="7.0", AR1="6.0", MB1="7.7", A12="6.5", AT2="8.1",
                          AR2="5.5", MB2="7.3", A13="0.0", AT3="10", AR3="4", MB3="5",
                          A14="9.5", AT4="8.3", AR4="3.5", MB4="8.9", MBF="7.2", EF="6.0", NC="0")
        rows[number] = {chr(65 + index): values[title] for index, title in enumerate(headers)}
    return rows


class PortalExportTests(unittest.TestCase):
    def analyze(self, rows=None, mode="AUTO"):
        return analyze_workbook(make_xlsx([("Notas_0000", rows or portal_export_rows())], dimension="A1"), "consulta-teste.xlsx", mode)

    def test_incorrect_a1_dimension_does_not_truncate_metadata_or_students(self):
        result = self.analyze()
        self.assertEqual(result["summary"]["students"], 2)
        self.assertEqual(result["summary"]["records"], 16)
        self.assertEqual(result["summary"]["valid"], 8)
        self.assertEqual(result["summary"]["inactive"], 8)
        self.assertEqual(set(result["available_assessments"]), set(SUPPORTED_ASSESSMENTS))
        for entry in result["sheets"]:
            self.assertEqual(entry["source_format"], "portal_export")
            self.assertEqual(entry["header_row"], 9)
            self.assertEqual(entry["class_name"], "EConInf-1TESTE")
            metadata = entry["portal_metadata"]
            self.assertEqual(metadata, entry["metadata"])
            self.assertEqual(metadata["discipline_code"], "M123456")
            self.assertEqual(metadata["discipline_name"], "Redes de Computadores")
            self.assertEqual(metadata["year"], 2026)
            self.assertEqual(metadata["period"], "0")
            self.assertEqual(metadata["course"], "Curso Técnico Exemplo")
            self.assertEqual(metadata["physical_unit"], "Unidade de Teste")
            self.assertEqual(metadata["responsible_unit"], "Ensino Técnico")
            self.assertEqual(metadata["evaluation_formula"], "MBF")
            self.assertEqual(metadata["final_formula"], "MBF")
            self.assertEqual(entry["portal_columns"][:4], ["Aluno", "Código", "Situação", "Curso"])
            row = entry["students"][0]
            self.assertEqual(row["portal_discipline"], "Redes de Computadores")
            self.assertEqual(row["course"], "TESTE")
            self.assertEqual(row["year"], 2026)
            self.assertEqual(row["name"], "Aluno de Teste 10")
            self.assertTrue(row["eligible"])
            self.assertEqual(row["components"], [])
            if entry["assessment"].startswith("A1"):
                self.assertEqual(row["discipline"], "Redes de Computadores")
                self.assertEqual(row["scope"], "discipline")
            else:
                self.assertEqual(row["discipline"], "Redes de Computadores")
                self.assertEqual(row["scope"], "discipline")

    def test_zero_comma_decimals_and_protected_values_are_preserved(self):
        rows = portal_export_rows()
        rows[10]["M"] = 0
        result = self.analyze(rows)
        targets = {entry["assessment"]: entry["students"][0] for entry in result["sheets"]}
        self.assertEqual(targets["A11"]["grade"], 8.4)
        self.assertEqual(targets["A13"]["grade"], 0)
        self.assertEqual(targets["A13"]["portal_values"]["A13"], "0")
        protected = targets["AT4"]["portal_values"]
        for key, value in {"AR1": "6.0", "MB1": "7.7", "MBF": "7.2", "EF": "6.0", "NC": "0"}.items():
            self.assertEqual(protected[key], value)
        self.assertEqual(set(targets), set(SUPPORTED_ASSESSMENTS))

    def test_empty_grade_is_pending_without_erasing_zero_or_activating_cancelled(self):
        rows = portal_export_rows()
        rows[10]["R"] = "  "
        result = self.analyze(rows)
        entries = {entry["assessment"]: entry for entry in result["sheets"]}
        self.assertEqual(result["summary"]["valid"], 7)
        self.assertFalse(entries["AT4"]["students"][0]["eligible"])
        self.assertIsNone(entries["AT4"]["students"][0]["grade"])
        self.assertTrue(entries["A13"]["students"][0]["eligible"])
        self.assertEqual(entries["A13"]["students"][0]["grade"], 0)
        self.assertTrue(all(not entry["students"][1]["eligible"] for entry in result["sheets"]))

    def test_exact_selection_keeps_original_column_and_all_available_codes(self):
        result = self.analyze(mode="AT2")
        self.assertEqual(len(result["sheets"]), 1)
        entry = result["sheets"][0]
        self.assertEqual(entry["assessment"], "AT2")
        self.assertEqual(entry["source_column"], "J")
        self.assertEqual(entry["students"][0]["source_cell"], "J10")
        self.assertEqual(entry["students"][0]["grade"], 8.1)
        self.assertEqual(set(result["available_assessments"]), set(SUPPORTED_ASSESSMENTS))

    def test_explicit_status_is_authoritative_and_contradiction_blocks(self):
        rows = portal_export_rows()
        rows[10]["C"] = "Cancelado"
        row = self.analyze(rows, mode="AT4")["sheets"][0]["students"][0]
        self.assertEqual(row["status"], "Cancelado")
        self.assertEqual(row["name"], "Aluno de Teste 10")
        self.assertFalse(row["eligible"])
        self.assertIn("status_conflict", [issue["code"] for issue in row["issues"]])

    def test_adjacent_metadata_labels_keep_subject_and_academic_period(self):
        rows = portal_export_rows()
        rows[2] = {"A": "Disciplina", "B": "M123456 Redes de Computadores"}
        rows[3] = {"A": "Turma", "B": "EConInf-1TESTE"}
        rows[4] = {"A": "Ano/Período", "B": "2027/1"}
        entry = self.analyze(rows, mode="A11")["sheets"][0]
        self.assertEqual(entry["discipline"], "Redes de Computadores")
        self.assertEqual(entry["portal_metadata"]["year"], 2027)
        self.assertEqual(entry["portal_metadata"]["period"], "1")
        self.assertTrue(entry["students"][0]["eligible"])


if __name__ == "__main__":
    unittest.main()
