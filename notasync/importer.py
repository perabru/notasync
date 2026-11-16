"""Read grade workbooks without running macros or trusting Excel's formula cache.

Only the arithmetic/functions used in grade sheets are accepted. Assessment
codes are read from explicit headers; exam grades stay discipline-scoped and
project grades read/recalculate their explicit final column.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
import posixpath
import re
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REF = re.compile(r"\$?([A-Z]{1,3})\$?([1-9]\d*)")
TOKEN = re.compile(r'\s*(?:(\d+(?:\.\d*)?(?:[Ee][+-]?\d+)?)|(\$?[A-Z]{1,3}\$?[1-9]\d*)|([A-Z_][A-Z_0-9.]*)|("(?:[^"]|"")*")|(>=|<=|<>|[+*/^(),:;=<>-]))', re.I)
SUPPORTED_ASSESSMENTS = ("AT1", "A11", "AT2", "A12", "AT3", "A13", "AT4", "A14")


class WorkbookError(ValueError):
    pass


class FormulaError(ValueError):
    pass


def normalize(value):
    return " ".join(unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().upper().split())


def detect_assessment(header):
    """Recognize writable source headers, never concepts or calculated averages.

    AT columns must identify the final project grade explicitly. A1n can carry
    the discipline after whitespace, a dash or a colon. A generic MÉDIA or a
    filename never determines the assessment code.
    """
    text = normalize(header)
    project = re.fullmatch(r"(?:MEDIA(?: FINAL)?\s+)?(AT[1-4])", text)
    if project:
        return project[1]
    exam = re.fullmatch(r"(A1[1-4])(?:[\s:-]+[^\s].*)?", text)
    return exam[1] if exam else None


def registration(value):
    """Preserve text identifiers; normalize Excel scientific numeric identifiers."""
    if value is None or not str(value).strip():
        return ""
    text = str(value).strip()
    if re.fullmatch(r"\d+", text):
        return text
    try:
        number = Decimal(text.replace(",", "."))
        return str(number.quantize(Decimal(1))) if number.is_finite() and number == number.to_integral_value() and number >= 0 else ""
    except InvalidOperation:
        return ""


def _col_number(col):
    result = 0
    for char in col.replace("$", "").upper():
        result = result * 26 + ord(char) - 64
    return result


def _col_name(number):
    if number < 1:
        raise FormulaError("Referência de coluna inválida")
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _translate_formula(formula, origin, target):
    src, dst = REF.fullmatch(origin), REF.fullmatch(target)
    dc = _col_number(dst[1]) - _col_number(src[1])
    dr = int(dst[2]) - int(src[2])
    def replace(match):
        token = match[0]
        col = match[1] if token.startswith("$") else _col_name(_col_number(match[1]) + dc)
        row = int(match[2]) if "$" in token[len(match[1]) + (1 if token.startswith("$") else 0):] else int(match[2]) + dr
        if row < 1:
            raise FormulaError("Referência de linha inválida")
        return ("$" if token.startswith("$") else "") + col + ("$" if "$" in token[1:] else "") + str(row)
    return REF.sub(replace, formula)


@dataclass
class Cell:
    value: str | None = None
    formula: str | None = None
    kind: str | None = None


class FormulaParser:
    def __init__(self, formula):
        if len(formula) > 8192:
            raise FormulaError("Fórmula muito longa")
        self.tokens, self.index = [], 0
        source = formula.lstrip("=").strip().upper()
        while source:
            found = TOKEN.match(source)
            if not found:
                raise FormulaError("Fórmula contém expressão não suportada")
            self.tokens.append(found[0].strip())
            source = source[found.end():]

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self, expected=None):
        result = self.peek()
        if result is None or (expected is not None and result != expected):
            raise FormulaError("Sintaxe de fórmula inválida")
        self.index += 1
        return result

    def parse(self):
        result = self.compare()
        if self.peek() is not None:
            raise FormulaError("Expressão de fórmula incompleta")
        return result

    def compare(self):
        node = self.add()
        if self.peek() in (">", "<", "=", ">=", "<=", "<>"):
            op = self.take()
            node = ("binary", op, node, self.add())
        return node

    def add(self):
        node = self.multiply()
        while self.peek() in ("+", "-"):
            op = self.take()
            node = ("binary", op, node, self.multiply())
        return node

    def multiply(self):
        node = self.power()
        while self.peek() in ("*", "/"):
            op = self.take()
            node = ("binary", op, node, self.power())
        return node

    def power(self):
        node = self.atom()
        if self.peek() == "^":
            self.take()
            node = ("binary", "^", node, self.power())
        return node

    def atom(self):
        token = self.take()
        if token in ("+", "-"):
            return ("unary", token, self.atom())
        if token == "(":
            node = self.compare()
            self.take(")")
            return node
        if re.fullmatch(r"\d+(?:\.\d*)?(?:E[+-]?\d+)?", token):
            return ("number", Decimal(token))
        if token.startswith('"'):
            return ("text", token[1:-1].replace('""', '"'))
        if REF.fullmatch(token):
            token = token.replace("$", "")
            if self.peek() == ":":
                self.take()
                end = self.take().replace("$", "")
                if not REF.fullmatch(end):
                    raise FormulaError("Intervalo inválido")
                return ("range", token, end)
            return ("ref", token)
        if token in ("TRUE", "FALSE"):
            return ("number", Decimal(token == "TRUE"))
        if self.peek() == "(":
            if token not in ("IF", "SUM", "AVERAGE", "MIN", "MAX", "ROUND"):
                raise FormulaError("Função não suportada: " + token)
            self.take()
            args = []
            if self.peek() != ")":
                args.append(self.compare())
                while self.peek() in (",", ";"):
                    self.take()
                    args.append(self.compare())
            self.take(")")
            return ("call", token, args)
        raise FormulaError("Referência ou função não suportada")


class Sheet:
    def __init__(self, name, cells):
        self.name, self.cells = name, cells
        self.cache = {}

    def raw(self, address):
        cell = self.cells.get(address, Cell())
        return cell.value

    def is_blank(self, address):
        cell = self.cells.get(address)
        return cell is None or (not cell.formula and (cell.value is None or not str(cell.value).strip()))

    def numeric(self, address, stack=None):
        if address in self.cache:
            return self.cache[address]
        stack = set(stack or ())
        if address in stack or len(stack) > 64:
            raise FormulaError("Referência circular ou fórmula muito profunda")
        stack.add(address)
        cell = self.cells.get(address, Cell())
        if cell.kind == "e":
            raise FormulaError("Erro na célula: " + str(cell.value))
        if cell.formula:
            value = self.evaluate(FormulaParser(cell.formula).parse(), stack)
        elif cell.value is None or not str(cell.value).strip():
            value = Decimal(0)
        else:
            try:
                value = Decimal(str(cell.value).strip().replace(",", "."))
            except InvalidOperation as exc:
                raise FormulaError("Valor não numérico em " + address) from exc
        if not isinstance(value, (Decimal, bool)) or not Decimal(value).is_finite():
            raise FormulaError("Resultado não numérico em " + address)
        self.cache[address] = Decimal(value)
        return self.cache[address]

    def evaluate(self, node, stack):
        kind = node[0]
        if kind in ("number", "text"):
            return node[1]
        if kind == "ref":
            return self.numeric(node[1], stack)
        if kind == "unary":
            number = self.evaluate(node[2], stack)
            return number if node[1] == "+" else -number
        if kind == "range":
            start, end = REF.fullmatch(node[1]), REF.fullmatch(node[2])
            left, right = sorted((_col_number(start[1]), _col_number(end[1])))
            top, bottom = sorted((int(start[2]), int(end[2])))
            if (right - left + 1) * (bottom - top + 1) > 10000:
                raise FormulaError("Intervalo de fórmula muito extenso")
            return [self.numeric(f"{_col_name(col)}{row}", stack) for row in range(top, bottom + 1) for col in range(left, right + 1) if not self.is_blank(f"{_col_name(col)}{row}")]
        if kind == "binary":
            op, lhs, rhs = node[1], self.evaluate(node[2], stack), self.evaluate(node[3], stack)
            if op == "+": return lhs + rhs
            if op == "-": return lhs - rhs
            if op == "*": return lhs * rhs
            if op == "/":
                if not rhs: raise FormulaError("Divisão por zero")
                return lhs / rhs
            if op == "^":
                if rhs != rhs.to_integral_value() or abs(rhs) > 20: raise FormulaError("Expoente não suportado")
                return lhs ** int(rhs)
            if op == ">": return lhs > rhs
            if op == "<": return lhs < rhs
            if op == "=": return lhs == rhs
            if op == ">=": return lhs >= rhs
            if op == "<=": return lhs <= rhs
            if op == "<>": return lhs != rhs
        if kind == "call":
            function, args = node[1], node[2]
            if function == "IF":
                if len(args) != 3: raise FormulaError("IF precisa de três argumentos")
                return self.evaluate(args[1] if self.evaluate(args[0], stack) else args[2], stack)
            if function == "ROUND":
                if len(args) != 2: raise FormulaError("ROUND precisa de dois argumentos")
                value, digits = self.evaluate(args[0], stack), int(self.evaluate(args[1], stack))
                if abs(digits) > 10: raise FormulaError("Precisão de arredondamento inválida")
                return value.quantize(Decimal(10) ** -digits, rounding=ROUND_HALF_UP)
            values = []
            for arg in args:
                value = self.evaluate(arg, stack)
                values.extend(value if isinstance(value, list) else [value])
            if function == "SUM": return sum(values, Decimal(0))
            if not values: raise FormulaError("Função sem valores")
            if function == "AVERAGE": return sum(values) / len(values)
            if function == "MIN": return min(values)
            if function == "MAX": return max(values)
        raise FormulaError("Expressão não suportada")


def _read_xlsx(content):
    if len(content) > 30 * 1024 * 1024:
        raise WorkbookError("A planilha deve ter até 30 MB")
    try:
        archive = ZipFile(BytesIO(content))
        if len(archive.infolist()) > 10000 or sum(info.file_size for info in archive.infolist()) > 150 * 1024 * 1024:
            raise WorkbookError("Conteúdo da planilha excede o limite de leitura")
        if "xl/vbaProject.bin" in archive.namelist():
            raise WorkbookError("Use uma planilha .xlsx sem macros")
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ["".join(node.itertext()) for node in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("m:si", NS)]
        rels = {rel.attrib["Id"]: rel.attrib["Target"] for rel in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels")) if rel.attrib.get("TargetMode") != "External"}
        result = []
        for sheet in ET.fromstring(archive.read("xl/workbook.xml")).findall("m:sheets/m:sheet", NS):
            target = rels.get(sheet.attrib.get("{" + REL_NS + "}id"))
            if not target:
                continue
            target = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            if not target.startswith("xl/"):
                raise WorkbookError("Caminho inválido no arquivo Excel")
            cells, shared, deferred = {}, {}, []
            root = ET.fromstring(archive.read(target))
            for node in root.findall("m:sheetData/m:row/m:c", NS):
                address = node.attrib.get("r", "").upper()
                if not REF.fullmatch(address):
                    continue
                kind = node.attrib.get("t")
                raw = node.find("m:v", NS)
                value = raw.text if raw is not None else None
                if kind == "s" and value is not None:
                    value = strings[int(value)]
                elif kind == "inlineStr":
                    inline = node.find("m:is", NS)
                    value = "".join(inline.itertext()) if inline is not None else ""
                formula = node.find("m:f", NS)
                cell = Cell(value, formula.text if formula is not None else None, kind)
                if formula is not None and formula.attrib.get("t") == "shared":
                    si = formula.attrib.get("si")
                    if formula.text:
                        shared[si] = (address, formula.text)
                    else:
                        deferred.append((address, si))
                if cell.value is not None or cell.formula is not None or formula is not None:
                    cells[address] = cell
            for address, si in deferred:
                if si not in shared:
                    cells[address].formula = "UNRESOLVED_SHARED_FORMULA()"
                else:
                    origin, formula = shared[si]
                    cells[address].formula = _translate_formula(formula, origin, address)
            result.append(Sheet(sheet.attrib["name"], cells))
        return result
    except WorkbookError:
        raise
    except (BadZipFile, KeyError, ET.ParseError, IndexError, ValueError) as exc:
        raise WorkbookError("Não foi possível ler a planilha .xlsx. Verifique o arquivo.") from exc


def _issue(code, message, severity="error"):
    return {"code": code, "message": message, "severity": severity}


def _class_from_title(text):
    found = re.search(r"(?<!\w)([1-3]\s*[AFHIJM])\b", normalize(text))
    return found[1].replace(" ", "") if found else ""


def _status_name(value):
    text = str(value or "").strip()
    if " - " in text:
        name, suffix = text.rsplit(" - ", 1)
        if normalize(suffix) in ("MATRICULADO", "CANCELADO", "TRANSFERIDO", "DESISTENTE", "TRANCADO"):
            return name.strip(), suffix.strip()
    return text, ""


def _metadata_value(values, names):
    ordered = sorted(values, key=_col_number)
    for index, col in enumerate(ordered):
        raw = str(values[col] or "").strip()
        if normalize(raw) in names and index + 1 < len(ordered):
            candidate = str(values[ordered[index + 1]] or "").strip()
            if candidate:
                return candidate
        for name in names:
            match = re.match(r"^" + re.escape(name) + r"\s*:\s*(.+)$", normalize(raw))
            if match:
                return raw.split(":", 1)[1].strip()
    return ""


def _discipline_metadata(value):
    """Separate an explicit portal code without guessing a subject abbreviation."""
    text = " ".join(str(value or "").split())
    match = re.fullmatch(r"([A-Za-z]{1,4}\d{4,12})\s+(.+)", text)
    return (match[1], match[2].strip()) if match else ("", text)


def _period_metadata(value):
    text = " ".join(str(value or "").split())
    match = re.fullmatch(r"(\d{4})(?:\s*/\s*(\d+))?", text)
    if not match or not 1900 <= int(match[1]) <= 2200:
        return None, ""
    return int(match[1]), match[2] or ""


def _is_portal_export(headers):
    names = {normalize(value) for value in headers.values()}
    return {"ALUNO", "CODIGO", "SITUACAO", "CURSO", "MBF", "EF", "NC"}.issubset(names) and any(
        "AR" + str(bimester) in names for bimester in range(1, 5))


def _formula_component_columns(formula, row, headers):
    """Return same-row precedents, expanding explicit ranges, for useful warnings."""
    try:
        parsed = FormulaParser(formula).parse()
    except FormulaError:
        return []
    refs = set()
    def visit(node):
        if node[0] == "ref":
            parts = REF.fullmatch(node[1])
            if int(parts[2]) == row:
                refs.add(parts[1])
        elif node[0] == "range":
            first, last = REF.fullmatch(node[1]), REF.fullmatch(node[2])
            if min(int(first[2]), int(last[2])) <= row <= max(int(first[2]), int(last[2])):
                left, right = sorted((_col_number(first[1]), _col_number(last[1])))
                for column in range(left, right + 1):
                    refs.add(_col_name(column))
        elif node[0] == "binary":
            visit(node[2]); visit(node[3])
        elif node[0] == "unary":
            visit(node[2])
        elif node[0] == "call":
            for arg in node[2]:
                visit(arg)
    visit(parsed)
    return sorted((col for col in refs if col in headers), key=_col_number)


def analyze_workbook(content: bytes, filename: str, mode: str = "AT4") -> dict:
    mode = normalize(mode)
    if mode not in (*SUPPORTED_ASSESSMENTS, "AUTO"):
        raise WorkbookError("Selecione AUTO ou uma avaliação: " + ", ".join(SUPPORTED_ASSESSMENTS))
    sheets = _read_xlsx(content)
    output = {"filename": filename, "mode": mode, "available_assessments": [], "sheets": [], "issues": [], "summary": {}}
    entries, available = {}, set()
    for source in sheets:
        grouped_rows = {}
        for address, cell in source.cells.items():
            parts = REF.fullmatch(address)
            grouped_rows.setdefault(int(parts[2]), {})[parts[1]] = cell.value
        rows = sorted(grouped_rows)
        headers, header_cols, source_class, active = {}, {}, _class_from_title(source.name), False
        source_discipline = _metadata_value({"A": source.name}, ("DISCIPLINA", "MATERIA"))
        source_discipline_code, source_discipline = _discipline_metadata(source_discipline)
        source_metadata = {"course": "", "discipline_code": source_discipline_code,
                           "discipline_name": source_discipline, "class_name": source_class,
                           "year": None, "period": "", "physical_unit": "", "responsible_unit": "",
                           "evaluation_formula": "", "final_formula": ""}
        header_row, portal_export = None, False
        for row in rows:
            values = grouped_rows[row]
            title = next((_class_from_title(value) for value in values.values() if value and ("INFORMATICA" in normalize(value) or "ELETRONICA" in normalize(value))), "")
            if title:
                source_class = title
            student_header = next((col for col, value in values.items() if normalize(value) in ("ALUNO", "ALUNOS", "NOME", "NOME DO ALUNO")), None)
            if student_header:
                headers = {col: str(value or "").strip() for col, value in values.items()}
                header_cols = {normalize(value): col for col, value in headers.items()}
                header_row, portal_export = row, _is_portal_export(headers)
                available.update(code for code in map(detect_assessment, headers.values()) if code)
                active = True
                continue
            metadata_discipline = _metadata_value(values, ("DISCIPLINA", "MATERIA"))
            if metadata_discipline:
                source_discipline_code, source_discipline = _discipline_metadata(metadata_discipline)
                source_metadata.update(discipline_code=source_discipline_code, discipline_name=source_discipline)
                continue
            metadata_class = _metadata_value(values, ("TURMA",))
            if metadata_class:
                source_class = metadata_class
                source_metadata["class_name"] = source_class
                continue
            metadata_found = False
            for key, labels in (("course", ("CURSO",)), ("physical_unit", ("UNIDADE FISICA",)),
                                ("responsible_unit", ("UNIDADE RESPONSAVEL",)),
                                ("evaluation_formula", ("FORMULA CRITERIO AVALIACAO",)),
                                ("final_formula", ("FORMULA MEDIA FINAL",))):
                value = _metadata_value(values, labels)
                if value:
                    source_metadata[key] = " ".join(value.split())
                    metadata_found = True
            period = _metadata_value(values, ("ANO/PERIODO", "ANO / PERIODO", "ANO LETIVO", "PERIODO LETIVO"))
            if period:
                source_metadata["year"], source_metadata["period"] = _period_metadata(period)
                metadata_found = True
            if metadata_found:
                continue
            if not active:
                continue
            name_col = next((col for value, col in header_cols.items() if value in ("ALUNO", "ALUNOS", "NOME", "NOME DO ALUNO")), "B")
            reg_col = next((col for value, col in header_cols.items() if value in ("CODIGO", "RA", "MATRICULA", "REGISTRO")), "A")
            raw_reg = values.get(reg_col)
            reg = registration(raw_reg)
            name, status = _status_name(values.get(name_col))
            suffix_status = status
            if not name or not reg:
                continue
            status_col = header_cols.get("SITUACAO") or header_cols.get("STATUS")
            if status_col and str(values.get(status_col) or "").strip():
                # The export has a dedicated authoritative situation column.
                status = str(values.get(status_col) or "").strip()
            raw_class_col = header_cols.get("TURMA")
            discipline_col = header_cols.get("DISCIPLINA") or header_cols.get("MATERIA")
            class_name = str(values.get(raw_class_col) or source_class or "").strip()
            explicit_discipline_code, explicit_discipline = _discipline_metadata(values.get(discipline_col) or source_discipline)
            course_col = header_cols.get("CURSO")
            course = str(values.get(course_col) or source_metadata["course"] or "").strip()
            portal_values = {normalize(title): str(values[col] if values.get(col) is not None else "").strip()
                             for col, title in headers.items() if title} if portal_export else {}
            targets = [(col, title, detect_assessment(title)) for col, title in headers.items()
                       if detect_assessment(title) and (mode == "AUTO" or detect_assessment(title) == mode)]
            if not targets:
                continue
            for target_col, target_header, assessment in targets:
                is_project = assessment.startswith("AT")
                discipline = assessment if is_project else re.sub(r"^" + assessment + r"\s*[-:]?\s*", "", target_header, flags=re.I).strip()
                if is_project and explicit_discipline:
                    discipline = explicit_discipline
                if not is_project and not discipline:
                    discipline = explicit_discipline
                identity = (source.name, class_name, assessment, discipline, target_col)
                if identity not in entries:
                    metadata = {**source_metadata, "class_name": class_name,
                                "discipline_code": explicit_discipline_code or source_discipline_code,
                                "discipline_name": explicit_discipline,
                                "header_row": header_row, "source_format": "portal_export" if portal_export else "assessment_sheet"}
                    entry = {"id": "|".join(identity), "sheet": source.name, "class_name": class_name,
                             "assessment": assessment, "scope": "shared" if is_project else "discipline",
                             "discipline": discipline, "source_column": target_col, "headers": dict(headers),
                             "source_format": metadata["source_format"], "header_row": header_row,
                             "portal_metadata": metadata, "metadata": metadata,
                             "portal_columns": [headers[col] for col in sorted(headers, key=_col_number) if headers[col]] if portal_export else [],
                             "students": [], "issues": []}
                    entries[identity] = entry
                    output["sheets"].append(entry)
                entry = entries[identity]
                address = target_col + str(row)
                cell = source.cells.get(address, Cell())
                issues, warnings = [], []
                if not class_name:
                    issues.append(_issue("missing_class", "Turma não identificada. Informe o vínculo antes do lançamento."))
                if not is_project and not discipline:
                    issues.append(_issue("missing_discipline", assessment + " sem disciplina identificada. Informe a disciplina na planilha ou configure um vínculo explícito."))
                if status and normalize(status) not in ("MATRICULADO", "ATIVO", "REGULAR"):
                    issues.append(_issue("inactive_student", "Aluno com situação " + status, "info"))
                active_states = ("MATRICULADO", "ATIVO", "REGULAR")
                if suffix_status and status and normalize(suffix_status) != normalize(status) and not (
                        normalize(suffix_status) in active_states and normalize(status) in active_states):
                    issues.append(_issue("status_conflict", "A situação explícita diverge do sufixo do nome. Confira o registro na origem."))
                if is_project:
                    excluded = {reg_col, name_col, raw_class_col, discipline_col, status_col, course_col, target_col}
                    if cell.formula:
                        component_cols = [col for col in _formula_component_columns(cell.formula, row, headers) if col not in excluded]
                    elif portal_export:
                        # Exported grade-grid cells are final values. Recovery and
                        # averages are protected fields, never project components.
                        component_cols = []
                    else:
                        component_cols = [col for col, header in headers.items() if col not in excluded and header
                                          and _col_number(col) < _col_number(target_col) and not detect_assessment(header)
                                          and not normalize(header).startswith(("C-", "MB", "MEDIA FINAL"))]
                else:
                    preceding = sorted([col for col in headers if _col_number(col) < _col_number(target_col)], key=_col_number)[-2:]
                    component_cols = [col for col in preceding if normalize(headers[col]).startswith(("PROVA ", "LISTA "))]
                components = []
                for col in component_cols:
                    component_cell = col + str(row)
                    components.append({"label": headers[col], "cell": component_cell, "value": source.raw(component_cell), "blank": source.is_blank(component_cell)})
                project_components = [c for c in components if normalize(c["label"]) not in ("FEIRA", "BONUS", "BONUS FEIRA")]
                grade = None
                if source.is_blank(address):
                    issues.append(_issue("missing_grade", "Nota final ausente em " + address))
                elif cell.formula and components and not any(not c["blank"] for c in project_components):
                    issues.append(_issue("no_assessed_components", "Nenhuma nota de projeto/avaliação preenchida para este aluno."))
                elif cell.formula and not is_project and component_cols and any(normalize(c["label"]).startswith("PROVA ") and c["blank"] for c in components):
                    issues.append(_issue("missing_exam", "Nota de prova ausente; o resultado da fórmula não será lançado."))
                else:
                    try:
                        number = source.numeric(address)
                        if not Decimal(0) <= number <= Decimal(10):
                            issues.append(_issue("out_of_range", "Nota fora do intervalo de 0 a 10 em " + address))
                        else:
                            grade = float(number)
                        if cell.formula and cell.value not in (None, ""):
                            try:
                                cache = Decimal(str(cell.value))
                                if abs(number - cache) > Decimal("0.000001"):
                                    warnings.append(_issue("cache_recalculated", "O resultado salvo estava desatualizado; a fórmula foi recalculada.", "warning"))
                            except InvalidOperation:
                                pass
                        if cell.formula and any(c["blank"] for c in components):
                            warnings.append(_issue("blank_components", "Fórmula da planilha considera componentes vazios como zero.", "warning"))
                    except (FormulaError, InvalidOperation, ArithmeticError, TypeError) as exc:
                        issues.append(_issue("formula_error", "Não foi possível calcular " + address + ": " + str(exc)))
                # Keep contradictory source labels visible rather than guessing PDM.
                if not is_project and normalize(discipline) == "BD" and any("PDM" in normalize(c["label"]) for c in components):
                    issues.append(_issue("discipline_header_conflict", "Cabeçalho " + assessment + " - BD contém PROVA/LISTA PDM. Corrija o título na origem antes de lançar."))
                student = {"key": "|".join((assessment, class_name, discipline, reg)), "name": name,
                           "registration": reg, "class_name": class_name, "assessment": assessment,
                           "scope": "shared" if is_project and not explicit_discipline else "discipline", "discipline": discipline,
                           "source_format": "portal_export" if portal_export else "assessment_sheet",
                           "portal_discipline": explicit_discipline or (discipline if not is_project else ""),
                           "course": course, "year": source_metadata["year"], "period": source_metadata["period"],
                           "portal_values": portal_values,
                           "status": status, "grade": grade, "source_cell": address, "source_formula": cell.formula,
                           "cached_value": cell.value, "components": components, "issues": issues + warnings}
                student["eligible"] = grade is not None and not any(issue["severity"] == "error" or issue["code"] == "inactive_student" for issue in issues)
                entry["students"].append(student)
    output["available_assessments"] = [code for code in SUPPORTED_ASSESSMENTS if code in available]
    all_students = [student for entry in output["sheets"] for student in entry["students"]]
    counts = Counter(student["key"] for student in all_students)
    for student in all_students:
        if counts[student["key"]] > 1:
            student["issues"].append(_issue("duplicate_registration", "Registro repetido na mesma turma e disciplina; resolva a duplicidade."))
            student["eligible"] = False
    if not output["sheets"]:
        label = "das avaliações identificadas" if mode == "AUTO" else mode
        output["issues"].append(_issue("no_grade_columns", "Não foram encontradas colunas " + label + " com alunos e códigos."))
    output["summary"] = {"students": len({student["registration"] for student in all_students}), "records": len(all_students),
                         "valid": sum(student["eligible"] for student in all_students), "pending": sum(not student["eligible"] for student in all_students),
                         "inactive": sum(bool(student["status"]) and normalize(student["status"]) not in ("MATRICULADO", "ATIVO", "REGULAR") for student in all_students),
                         "warnings": sum(any(issue["severity"] == "warning" for issue in student["issues"]) for student in all_students),
                         "classes": len({entry["class_name"] for entry in output["sheets"]}), "sheets": len(output["sheets"]),
                         "assessments": {code: {"records": sum(student["assessment"] == code for student in all_students),
                                                "valid": sum(student["assessment"] == code and student["eligible"] for student in all_students),
                                                "pending": sum(student["assessment"] == code and not student["eligible"] for student in all_students)}
                                         for code in SUPPORTED_ASSESSMENTS if any(student["assessment"] == code for student in all_students)}}
    return output
