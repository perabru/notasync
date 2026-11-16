"""Local replica of the supplied Univap grade screen.

No real portal is contacted. Imported records live only in this process for the
current test session; the distributed app contains fictitious defaults only.
The replica uses the production browser adapter and a strict loopback boundary.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
import html
import inspect
import ipaddress
import json
import math
import secrets
import time
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .portal import ASSESSMENT_CODES, DEFAULT_CONFIG, PortalAdapter, PortalError, grade, normalize, registration

router = APIRouter()
_csrf_token = ""
_sessions: dict[str, "TrainingSession"] = {}
LOGIN = "coordenacao.teste"
PASSWORD = "teste-local"
TARGETS = {"A11": 8.4, "AT1": 0.0, "A12": 10.0, "AT2": 6.7,
           "A13": 4.5, "AT3": 9.3, "A14": 7.8, "AT4": 8.3}
SESSION_SECONDS = 30 * 60
QUICK_SAMPLE_SIZE = 3
PROTECTED_COLUMNS = tuple([item for n in range(1, 5) for item in ("AR" + str(n), "MB" + str(n))] + ["MBF", "EF", "NC"])


def configure_training(csrf_token: str):
    global _csrf_token
    _csrf_token = str(csrf_token)


def _readonly(values=None):
    values = values or {}
    result = {code: str(values.get(code, "") if values.get(code) is not None else "") for code in PROTECTED_COLUMNS}
    result.update({"C-" + code: str(values.get("C-" + code, "S")) for code in (*ASSESSMENT_CODES, *PROTECTED_COLUMNS)})
    return result


def _student(ra, name, status="Matriculado", values=None, course="Técnico em Informática"):
    values = values or {}
    return {"registration": ra, "name": name, "status": status, "course": course,
            "grades": {code: grade(values.get(code)) for code in ASSESSMENT_CODES}, "readonly": _readonly(values)}


@dataclass
class TrainingSession:
    identifier: str
    assessments: tuple[str, ...]
    year: int
    cookie: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    created: float = field(default_factory=time.monotonic)
    save_count: int = 0
    confirmations: int = 0
    offerings: list = field(default_factory=list)
    rosters: dict = field(default_factory=dict)
    targets: dict = field(default_factory=dict)
    source_mode: str = "synthetic"
    source_records: int = 0
    source_students: int = 0
    source_files: list = field(default_factory=list)
    controls: list = field(default_factory=list)
    test_mode: str = "full"
    expected_records: int = 0
    planned_records: int = 0

    @property
    def prefix(self):
        return "/test-portal/" + self.identifier

    def protected_snapshot(self):
        return {key: [{"registration": row["registration"], "name": row["name"], "status": row["status"],
                       "course": row["course"], "readonly": copy.deepcopy(row["readonly"]),
                       "grades": {code: value for code, value in row["grades"].items()
                                  if code not in self.assessments or row["status"] != "Matriculado"}}
                      for row in rows] for key, rows in self.rosters.items()}


def _test_mode(value):
    if value not in ("quick", "full"):
        raise ValueError("Selecione Teste resumido ou Teste completo.")
    return value


def _assessment_codes(assessments):
    if (not isinstance(assessments, (list, tuple)) or not assessments
            or any(code not in ASSESSMENT_CODES for code in assessments)):
        raise ValueError("O teste aceita somente A11/AT1 até A14/AT4.")
    return tuple(code for code in ASSESSMENT_CODES if code in assessments)


def _sample_targets(targets, test_mode):
    """Use first/middle/last registrations so the quick path spans the grid."""
    if test_mode == "full" or len(targets) <= QUICK_SAMPLE_SIZE:
        return dict(targets)
    registrations = sorted(targets)
    indices = (0, len(registrations) // 2, len(registrations) - 1)
    return {registrations[index]: targets[registrations[index]] for index in indices}


def _coverage(session):
    return {"test_mode": session.test_mode,
            "expected_records": session.expected_records,
            "planned_records": session.planned_records,
            "sampled_out_records": session.expected_records - session.planned_records,
            "coverage_mode": "sample" if session.test_mode == "quick" else "full",
            "coverage_complete": session.planned_records == session.expected_records}


def _source_groups(session, source_snapshot):
    snapshot = source_snapshot if isinstance(source_snapshot, dict) else {"rows": source_snapshot}
    records = snapshot.get("rows")
    if not isinstance(records, list) or not records or len(records) > 100000:
        raise ValueError("O teste precisa de uma cópia válida das notas importadas.")
    metadata = snapshot.get("metadata", [])
    if not isinstance(metadata, list):
        metadata = []
    groups = {}
    files = set()
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("A cópia de teste contém uma linha inválida.")
        code = item.get("assessment") or item.get("column")
        if code not in session.assessments:
            continue
        session.source_records += 1
        ra = registration(item.get("registration"))
        if not ra:
            continue
        class_name = str(item.get("class_name") or item.get("source_sheet") or "Turma da planilha").strip()
        discipline = str(item.get("portal_discipline") or item.get("source_discipline") or item.get("discipline") or "Projetos integrados").strip()
        if discipline in ASSESSMENT_CODES:
            discipline = "Projetos integrados"
        key = (class_name, discipline)
        if key not in groups:
            meta = next((m for m in metadata if isinstance(m, dict)
                         and (not m.get("class_name") or str(m.get("class_name")) == class_name)
                         and (not m.get("discipline_name") or str(m.get("discipline_name")) == discipline)), {})
            internal = "turma-" + str(len(groups) + 1)
            offering = {"key": internal, "code": str(meta.get("discipline_code") or "TESTE-" + str(len(groups) + 1)),
                        "class_name": class_name, "discipline": discipline, "metadata": copy.deepcopy(meta)}
            groups[key] = {"offering": offering, "rows": {}, "targets": {code: {} for code in session.assessments}}
        group = groups[key]
        values = item.get("portal_values") if isinstance(item.get("portal_values"), dict) else {}
        status = "Matriculado" if normalize(item.get("status") or "Matriculado") in ("MATRICULADO", "ATIVO", "REGULAR") else str(item.get("status") or "Cancelado")
        if ra not in group["rows"]:
            group["rows"][ra] = _student(ra, str(item.get("name") or item.get("student") or "Aluno da planilha"),
                                         status, values, str(item.get("course") or values.get("CURSO") or group["offering"]["metadata"].get("course") or ""))
        row = group["rows"][ra]
        if status != "Matriculado":
            row["status"] = status
            for targets in group["targets"].values():
                targets.pop(ra, None)
        problems = item.get("issues") or []
        has_error = any(not isinstance(issue, dict) or issue.get("severity") == "error" or issue.get("code") == "inactive_student" for issue in problems)
        if item.get("eligible") is False or has_error or row["status"] != "Matriculado":
            continue
        try:
            target = grade(item.get("grade"))
        except PortalError:
            continue
        if target is None:
            continue
        existing = group["targets"][code].get(ra)
        if existing is not None and existing != target:
            raise ValueError("Há notas conflitantes para a mesma matrícula e avaliação na cópia de teste.")
        group["targets"][code][ra] = target
        if item.get("source_file"):
            files.add(str(item["source_file"]))
    if not groups or not any(targets for group in groups.values() for targets in group["targets"].values()):
        raise ValueError("A planilha não possui notas aptas para testar as avaliações selecionadas.")
    session.source_mode = "imported_sheet"
    session.source_files = sorted(files)
    for group in groups.values():
        offering = group["offering"]
        rows = list(group["rows"].values())
        session.source_students += len(rows)
        session.offerings.append(offering)
        session.rosters[offering["key"]] = rows
        session.expected_records += sum(len(values) for values in group["targets"].values())
        sampled = {code: _sample_targets(values, session.test_mode)
                   for code, values in group["targets"].items()}
        session.planned_records += sum(len(values) for values in sampled.values())
        session.targets[offering["key"]] = sampled
        # Every selected eligible source value must cause an observable write.
        # Other source values retain their original grades in a quick test.
        for code, targets in sampled.items():
            for row in rows:
                if row["registration"] in targets:
                    row["grades"][code] = 1.0 if targets[row["registration"]] == 0 else 0.0
        used = set(group["rows"])
        def control_ra():
            number = 99000000
            while str(number) in used:
                number += 1
            used.add(str(number))
            return str(number)
        unchanged = _student(control_ra(), "Controle local — não alterar", values={code: 6.4 for code in ASSESSMENT_CODES})
        rows.append(unchanged)
        session.controls.append((offering["key"], unchanged["registration"]))
        if not any(row["status"] != "Matriculado" for row in rows):
            cancelled = _student(control_ra(), "Controle local cancelado", "Cancelado")
            rows.append(cancelled)
            session.controls.append((offering["key"], cancelled["registration"]))


def get_training_plan(source_snapshot, assessments, test_mode="full"):
    """Plan coverage without opening a browser or registering a local session."""
    mode = _test_mode(test_mode)
    codes = _assessment_codes(assessments)
    session = TrainingSession("plan-only", codes, 2026, test_mode=mode)
    _source_groups(session, source_snapshot)
    return _coverage(session)


def create_training_session(assessments, year, source_snapshot=None, test_mode="full"):
    mode = _test_mode(test_mode)
    codes = _assessment_codes(assessments)
    if not isinstance(year, int) or isinstance(year, bool) or not 2000 <= year <= 2100:
        raise ValueError("Ano letivo inválido para o teste local.")
    now = time.monotonic()
    for key, session in list(_sessions.items()):
        if now - session.created > SESSION_SECONDS:
            _sessions.pop(key, None)
    while len(_sessions) >= 8:
        _sessions.pop(next(iter(_sessions)))
    session = TrainingSession(secrets.token_urlsafe(24), codes, year, test_mode=mode)
    if source_snapshot is not None:
        _source_groups(session, source_snapshot)
    else:
        session.offerings = [
            {"key": "redes", "code": "TESTE-RED", "class_name": "TESTE-1A", "discipline": "Redes de Computadores", "metadata": {}},
            {"key": "banco", "code": "TESTE-BD", "class_name": "TESTE-2B", "discipline": "Banco de Dados", "metadata": {}},
        ]
        for offering in session.offerings:
            session.rosters[offering["key"]] = [
                _student("90000001", "Aluno Teste 01", values={code: 5.0 for code in ASSESSMENT_CODES}),
                _student("90000002", "Aluno Teste 02", values={code: 7.0 for code in ASSESSMENT_CODES}),
                _student("90000003", "Aluno Teste Cancelado", "Cancelado"),
            ]
            session.targets[offering["key"]] = {code: {"90000001": TARGETS[code]} for code in codes}
        session.source_students = 3
        session.source_records = 2 * len(codes)
        session.expected_records = session.source_records
        session.planned_records = session.source_records
    _sessions[session.identifier] = session
    return session


def _session(identifier):
    session = _sessions.get(identifier)
    if session is None or time.monotonic() - session.created > SESSION_SECONDS:
        raise HTTPException(404, "Este teste expirou. Volte ao NotaSync e clique em Testar novamente.")
    return session


def _authenticated(request, session):
    if request.cookies.get("notasync_training") != session.cookie:
        raise HTTPException(401, "Entre com a conta fictícia do ambiente de teste.")


def _json_js(value):
    return json.dumps(value, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")


def _esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


STYLE = """*{box-sizing:border-box}html,body{margin:0;background:#fff;color:#222;font:12px Arial,Helvetica,sans-serif}button,input{font:12px Arial,Helvetica,sans-serif}button,a{cursor:pointer}#_header{height:130px;border-bottom:1px solid #8aa2bd;background:linear-gradient(#e8eef5 0,#fff 46%,#dce5ee 46%,#f5f8fb 70%,#d3dfec 70%,#f8fafc 100%);position:relative}.topo{height:59px;display:flex;align-items:center;padding:7px 18px;gap:24px}.bgLogo{color:#0a4778;font-size:31px;font-weight:bold;letter-spacing:-1px;line-height:1}.bgLogo small{display:block;font-size:9px;letter-spacing:.6px;font-weight:normal;margin-top:5px}.campos{margin-left:auto;font-size:11px;display:flex;gap:23px;color:#395c7a}.menuButtons{height:30px;display:flex;align-items:center;padding:0 18px;gap:25px}.menuButtons a{color:#1b4264;text-decoration:none}.titulo{height:40px;display:flex;align-items:center;justify-content:center;color:#1b4c77;font-size:18px;font-weight:bold;text-shadow:0 1px #fff}#menuHolder{height:29px;background:linear-gradient(#fdfefe,#d3dfec);border-bottom:1px solid #98acc2;display:flex;gap:27px;align-items:center;padding:0 17px;color:#264b6b;font-size:11px}.ribbon{padding:8px 14px;background:#fff3cd;color:#6b531b;border-bottom:1px solid #e6d6a7;font-size:12px;font-weight:bold}.ribbon small{font-weight:normal;margin-left:15px}main{padding:10px 13px 48px;min-width:660px}h1{font-size:16px;color:#1f4e75;margin:0 0 12px}.instruction{border:1px solid #a4b6ca;background:linear-gradient(#f9fcff,#dce7f3);height:27px;display:flex;gap:8px;align-items:center;padding:3px 7px;color:#204d76;font-weight:bold;margin:0 0 7px}.instruction .number{background:#406c96;color:white;padding:1px 5px;font-size:11px}.metadata{border:1px solid #c2cbd7;padding:9px 12px;margin-bottom:8px;display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:5px 20px}.metadata .field{display:flex;gap:6px;min-width:0;line-height:1.6}.metadata label{font-weight:bold;white-space:nowrap;color:#2b4358}.metadata span{overflow-wrap:anywhere}.metadata .wide{grid-column:span 2}.another{color:#315e82;font-weight:bold;text-decoration:underline;font-size:12px;display:inline-block;margin:4px 0}.panel-title{border:1px solid #a4b6ca;background:linear-gradient(#f8fbff,#d8e3ef);padding:5px 7px;color:#284f72;font-weight:bold;margin-bottom:7px}.toolbar{display:flex;align-items:center;gap:4px;border:1px solid #a6b8ca;border-bottom:0;background:linear-gradient(#fff,#e2e8f0);padding:3px 5px;min-height:29px}.toolbar button,.pager button,.x-window button{border:1px solid transparent;border-radius:2px;background:transparent;color:#29445e;padding:4px 8px;white-space:nowrap}.toolbar button:hover,.pager button:hover,.x-window button{background:linear-gradient(#fff,#dfe8f3);border-color:#9cb0c8}.toolbar .separator{height:18px;border-left:1px solid #b6c5d4;margin:0 3px}.toolbar .icon-save:before{content:'▣';color:#3f6d91;margin-right:5px}.toolbar .icon-undo:before{content:'↶';color:#558d44;margin-right:5px}.status{color:#5d7186;font-size:11px;margin-left:auto}.status.error{color:#9d3333}.x-grid-panel{border:1px solid #9eafc3}.x-grid3-scroller{overflow:auto;height:300px;background:white}.grade-table{border-collapse:separate;border-spacing:0;min-width:100%;font-size:11px;white-space:nowrap;table-layout:fixed}.grade-table th{position:sticky;top:0;z-index:2;text-align:left;font-weight:normal;background:linear-gradient(#fff,#e6e9ed);border-right:1px solid #bfc9d5;border-bottom:1px solid #a7b4c3;height:25px;padding:4px 6px}.grade-table td{border-right:1px solid #e2e5e8;border-bottom:1px solid #ededed;height:23px;padding:3px 6px;line-height:16px}.grade-table tr:nth-child(even) td{background:#fafafa}.grade-table tr:hover td{background:#eaf1fa}.grade-table th:first-child,.grade-table td:first-child{min-width:230px;max-width:270px}.grade-table td:nth-child(2){min-width:85px}.grade-table td:nth-child(3){min-width:90px}.grade-table td:nth-child(4){min-width:140px}.numeric{min-width:49px;text-align:right;font-variant-numeric:tabular-nums}.concept{min-width:46px;text-align:center;color:#777}.readonly{color:#666;background:#f8fafc}.grade-editable{cursor:text}.grade-editable:hover{outline:1px solid #9ab7d8;outline-offset:-1px}.pager{border-top:1px solid #b5c3d2;background:linear-gradient(#f9fbfd,#e0e7ee);padding:4px 7px;display:flex;align-items:center;gap:5px;font-size:11px;color:#50677e;min-height:28px}.pager span:last-child{margin-left:auto}.pager button:disabled{opacity:.4;cursor:default}.grid-note{padding:5px 7px;color:#65778a;background:#f6f8fb;font-size:10px;border-top:1px solid #d9e0e8}.x-editor{position:absolute;background:white;border:1px solid #507db0;z-index:50;box-shadow:0 2px 5px #5a6a7a40}.x-editor input{width:48px;height:22px;padding:2px;border:0;outline:0;color:#172c43}.x-mask{position:fixed;inset:0;background:#0003;z-index:80;display:flex;align-items:center;justify-content:center}.x-window{width:375px;border:1px solid #647f9d;border-radius:5px;padding:4px;background:#dbe6f2;box-shadow:0 7px 30px #0005}.x-window-header{color:#254c73;font-size:12px;font-weight:bold;padding:4px 6px 7px}.x-window-body{background:#fff;border:1px solid #b4c4d7;padding:24px 18px;font-size:12px}.x-window-footer{padding:9px 8px 4px;display:flex;gap:7px;justify-content:center}.x-window button{min-width:70px;color:#222}.login{max-width:430px;margin:35px auto;border:1px solid #aebed0;background:#f6f8fb;padding:22px}.login h1{font-size:18px}.login label{display:block;margin:13px 0 5px}.login input{width:100%;border:1px solid #9eb2c9;height:29px;padding:5px;background:white}.login button{margin-top:20px;border:1px solid #809bb7;background:linear-gradient(#fff,#dbe7f3);padding:7px 12px;width:100%;color:#244a72}.notice{background:#f3f7fc;border:1px solid #d7e1ed;padding:9px 11px;margin-top:12px;color:#62768d;font-size:11px;line-height:1.6}.login-error{color:#a23b3b;margin-top:12px}.approval{padding:13px;border:1px solid #72a38c;background:#eef9f3;color:#245c43;margin-bottom:10px}.approval strong{font-size:15px;display:block;margin-bottom:5px}footer{position:fixed;bottom:0;left:0;right:0;height:30px;background:linear-gradient(#e9eff6,#c8d6e5);border-top:1px solid #9cafc4;display:flex;align-items:center;justify-content:space-between;padding:0 15px;color:#587086;font-size:10px}.links{display:flex;gap:20px;padding:12px 2px;font-size:11px}.links a{color:#2c5c82;text-decoration:underline}.column-legend{padding:8px 9px;border:1px solid #ccd6e2;color:#536b82;font-size:11px;margin-bottom:8px}.column-legend strong{color:#2b5276}.class-list{height:auto;min-height:70px;max-height:300px}.class-list .grade-table th:first-child,.class-list .grade-table td:first-child{min-width:100px}.subject{color:#235b87;text-decoration:underline}.class-title{font-size:14px;color:#284e72;padding:7px;background:#eff4fa;border:1px solid #b4c4d7;border-bottom:0}.mini{font-size:10px;color:#6b7c91}@media(max-width:760px){.metadata{grid-template-columns:repeat(2,minmax(140px,1fr))}.campos{font-size:10px;gap:8px}.bgLogo{font-size:24px}.ribbon small{display:block;margin:4px 0 0}.topo{padding-left:12px}.menuButtons{padding-left:12px}}"""


SCREEN_STYLE = """#_header{height:119px;background:#08589b;border:0}.topo{height:72px;background:linear-gradient(110deg,#0c60a3,#075592);padding:10px 5.2vw;color:white}.bgLogo{font-family:Georgia,serif;font-size:46px;font-weight:bold;letter-spacing:-3px;color:white}.bgLogo small{font:8px Arial,sans-serif;letter-spacing:.15px;margin-top:2px}.campos{color:white;font-size:11px;margin-left:30px;margin-right:230px;max-width:650px;flex:1;justify-content:space-between;font-weight:bold}.campos span:last-child{display:none}.menuButtons{height:25px;position:absolute;right:10px;top:22px;gap:20px}.menuButtons a{color:white;font-weight:bold}.titulo{height:25px;background:#296aa0;color:#c5d6e6;font-size:14px;justify-content:flex-start;padding-left:36px;text-shadow:none;position:absolute;top:119px;left:0;right:0;letter-spacing:.2px}#menuHolder{height:22px;margin-top:-22px;position:relative;z-index:4;justify-content:center;background:transparent;border:0;color:white;font-size:10px;gap:10px;font-weight:bold}.ribbon{margin-top:25px;padding:6px 14px;text-align:center;font-size:11px}main{max-width:960px;margin:auto;padding:10px 13px 46px}.instruction{background:white;border:0;border-bottom:1px solid #687788;height:24px;color:#2d4359;font-family:Georgia,serif;font-size:14px;letter-spacing:.3px;margin-bottom:6px;padding:1px 0}.instruction .number{border-radius:50%;background:linear-gradient(#b2bfcd,#2a3e59);font-family:Arial,sans-serif;width:17px;height:17px;text-align:center;padding:1px 0;font-size:12px}.metadata{background:#ebebeb;gap:4px 14px;padding:7px 14px;font-size:10px;grid-template-columns:1.15fr 1.15fr .8fr .8fr;margin-bottom:8px}.metadata .field{gap:5px;align-items:flex-start}.metadata label{font-weight:normal;min-width:60px;color:#203e55;font-size:10px}.metadata span{background:#fff;border:1px solid #d1d1d1;flex:1;min-height:17px;padding:0 3px;line-height:15px;color:#111}.metadata .wide{grid-column:4;grid-row:4;margin-top:-5px}.another{background:linear-gradient(#fafcfd,#9cb0c2);border:1px solid #b3c0ca;text-decoration:none;color:#163a58;font-weight:bold;font-size:10px;padding:2px 5px;border-radius:4px;white-space:nowrap;display:block}.panel-title{font-family:Georgia,serif;font-size:18px;color:white;background:linear-gradient(#9badbe,#bfd0df);border:1px solid #c9d4de;padding:1px 4px;height:27px;margin-bottom:4px;letter-spacing:.2px}.panel-title span{float:right;color:#406c87}.column-legend{font-size:10px;padding:5px;margin-bottom:5px}.launch-heading{margin-top:8px;margin-bottom:30px}.toolbar{background:#a3b6c8;height:30px;min-height:30px}.toolbar button{font-size:10px;padding:3px 5px;color:#243c52}.toolbar button.icon-save:before{content:'▣';color:#c88e0c}.toolbar button.icon-undo:before{color:#eef8e9}.toolbar button:not(.icon-save):not(.icon-undo),.toolbar>.separator{display:none}.status{font-size:10px;color:#334d67}.x-grid-panel{border:1px solid #c3d5e7;border-left:5px solid #c3dcf5;border-right:5px solid #c3dcf5}.grade-table th{color:white;background:linear-gradient(#658096,#2e3f4c);border-right:1px solid white;border-bottom:1px solid #122537;height:21px;font-size:10px;padding:3px 4px}.grade-table td{height:21px;font-size:11px;padding:2px 4px}.grade-table th:first-child,.grade-table td:first-child{min-width:218px;max-width:240px}.grade-table td:nth-child(2){min-width:85px}.grade-table td:nth-child(3){min-width:76px}.grade-table td:nth-child(4){min-width:76px}.numeric{min-width:48px}.concept{min-width:43px}.x-grid3-scroller{height:300px}.pager{font-size:10px}.grid-note{font-size:10px}footer{background:#07599d;color:white;justify-content:center;font-weight:bold;font-size:11px;border:0}footer span:last-child{position:absolute;right:15px;font-size:9px;font-weight:normal}.class-list{height:auto;min-height:70px}.links{font-size:10px;padding-top:7px}@media(max-width:850px){.campos{font-size:10px;margin-left:12px;margin-right:0;gap:8px}.campos span:last-child{display:none}.menuButtons{display:none}.metadata{grid-template-columns:repeat(2,minmax(150px,1fr))}.metadata .wide{grid-column:2;grid-row:auto}#menuHolder{gap:6px;font-size:9px}.bgLogo{font-size:36px}.ribbon small{display:block;margin-left:0}footer span:last-child{display:none}}"""

def _page(title, content, script="", session=None):
    source = session and session.source_mode == "imported_sheet"
    subtitle = "Cópia local da planilha importada; nenhum envio à Univap." if source else "Dados fictícios para verificar a automação."
    if session:
        subtitle += (" Teste resumido: até 3 notas por turma, disciplina e avaliação."
                     if session.test_mode == "quick" else " Teste completo: todas as notas aptas selecionadas.")
    return HTMLResponse("<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>" + _esc(title) + " · Teste NotaSync</title><style>" + STYLE + SCREEN_STYLE + "</style></head><body><div id='_header'><div class='topo'><div class='bgLogo'>UniVap<small>UNIVERSIDADE DO VALE DO PARAÍBA · RÉPLICA LOCAL</small></div><div class='campos'><span>Usuário: <b>Conta de teste local</b></span><span>Docente On-Line</span><span>9.2 · Teste</span></div></div><div class='menuButtons'><a href='#' onclick='return false'>Inicial</a><a href='#' onclick='return false'>Suporte</a><a href='#' onclick='return false'>Sair</a></div><div class='titulo'>Lançar Notas (Incluir ou Alterar)</div></div><div id='menuHolder'><span>AVISOS</span><span>CALENDÁRIO</span><span>TURMAS</span><span>DISCIPLINA</span><span>NOTAS E FALTAS</span><span>DOCENTE</span><span>AVALIAÇÃO INSTITUCIONAL</span><span>PLATAFORMA</span></div><div class='ribbon'>Ambiente de teste • Nenhuma nota real será alterada<small>" + subtitle + "</small></div><main>" + content + "</main><footer><span>Lyceum - Techne® · Réplica local NotaSync</span><span>Sem conexão com o portal real</span></footer><script>" + script + "</script></body></html>", headers={"Cache-Control": "no-store"})


@router.get("/test-portal/{identifier}/login", response_class=HTMLResponse)
async def login_page(identifier: str):
    session = _session(identifier)
    content = "<div class='login'><h1>Acesso ao teste local</h1><p>O NotaSync preencherá esta conta fictícia automaticamente.</p><form id='login-form'><label for='username'>Login de teste</label><input id='username' autocomplete='off'><label for='password'>Senha de teste</label><input id='password' type='password' autocomplete='off'><button id='sendCredentials' type='submit'>Entrar no ambiente de teste</button><div id='login-error' class='login-error'></div></form><div class='notice'>Não informe seu login ou senha da Univap nesta página.</div></div>"
    script = "const prefix=" + _json_js(session.prefix) + ";const csrf=" + _json_js(_csrf_token) + ";document.getElementById('login-form').addEventListener('submit',async e=>{e.preventDefault();const r=await fetch(prefix+'/login',{method:'POST',headers:{'Content-Type':'application/json','X-NotaSync-Token':csrf},body:JSON.stringify({username:document.getElementById('username').value,password:document.getElementById('password').value})});if(r.ok)location.assign(prefix+'/classes');else document.getElementById('login-error').textContent='Use somente a conta fictícia do teste.';});"
    return _page("Acesso", content, script, session)


@router.post("/test-portal/{identifier}/login")
async def login_submit(identifier: str, request: Request):
    session = _session(identifier)
    body = await request.json()
    if not isinstance(body, dict) or body.get("username") != LOGIN or body.get("password") != PASSWORD:
        raise HTTPException(401, "Somente credenciais fictícias são aceitas aqui.")
    result = JSONResponse({"ok": True, "training": True})
    result.set_cookie("notasync_training", session.cookie, httponly=True, samesite="strict", path=session.prefix, max_age=SESSION_SECONDS)
    return result


@router.get("/test-portal/{identifier}/classes", response_class=HTMLResponse)
async def classes_page(identifier: str, request: Request, page: int = 1):
    session = _session(identifier)
    _authenticated(request, session)
    total = len(session.offerings)
    page = min(max(1, page), total)
    offering = session.offerings[page - 1]
    href = session.prefix + "/grades/" + offering["key"]
    content = "<div class='instruction'><span class='number'>1</span>Selecionar uma Turma:</div><div class='class-title'>Turmas do Docente</div><section class='x-grid-panel' id='grdTurmas'><div class='x-grid3-scroller class-list'><table class='grade-table'><thead><tr><th>Disciplina</th><th>Nome</th><th>Turma</th><th>Período Letivo</th></tr></thead><tbody><tr><td>" + _esc(offering["code"]) + "</td><td><a class='subject' href='" + href + "'>" + _esc(offering["discipline"]) + "</a></td><td>" + _esc(offering["class_name"]) + "</td><td>Ano " + str(session.year) + "</td></tr></tbody></table></div><div class='pager'><button class='x-tbar-page-first' " + ("disabled" if page == 1 else "") + " onclick=\"location.assign('" + session.prefix + "/classes?page=1')\">Primeira</button><span>Página " + str(page) + " de " + str(total) + "</span><button class='x-tbar-page-next' " + ("disabled" if page == total else "") + " onclick=\"location.assign('" + session.prefix + "/classes?page=" + str(page + 1) + "')\">Próxima</button><span>Exibindo registros " + str(page) + " - " + str(page) + " de " + str(total) + "</span></div></section><div class='notice'>As turmas do teste foram montadas a partir da origem selecionada. A conta utilizada aqui é fictícia e as gravações ficam somente nesta cópia local.</div>"
    return _page("Turmas do Docente", content, session=session)


def _grade_columns():
    columns = ["Aluno", "Código", "Situação", "Curso", "Média Final"]
    for n in range(1, 5):
        columns += ["A1" + str(n), "C-A1" + str(n), "AT" + str(n), "C-AT" + str(n), "AR" + str(n), "C-AR" + str(n), "MB" + str(n), "C-MB" + str(n)]
    return columns + ["MBF", "EF", "NC"]


@router.get("/test-portal/{identifier}/grades/{offering_key}", response_class=HTMLResponse)
async def grades_page(identifier: str, offering_key: str, request: Request):
    session = _session(identifier)
    _authenticated(request, session)
    offerings = [item for item in session.offerings if item["key"] == offering_key]
    if len(offerings) != 1:
        raise HTTPException(404, "Turma de teste não encontrada.")
    offering = offerings[0]
    columns = _grade_columns()
    header = "".join("<th>" + _esc(value) + "</th>" for value in columns)
    body = []
    for row in session.rosters[offering_key]:
        cells = ["<td>" + _esc(row["name"]) + "</td>", "<td>" + row["registration"] + "</td>", "<td>" + _esc(row["status"]) + "</td>", "<td>" + _esc(row["course"]) + "</td>"]
        for code in columns[4:]:
            if code in ASSESSMENT_CODES:
                value = row["grades"][code]
                text = "" if value is None else format(value, ".1f")
                editable = row["status"] == "Matriculado" and code in session.assessments
                attrs = " data-ra='" + row["registration"] + "' data-code='" + code + "'"
                cells.append("<td class='numeric x-grid3-td-prova-" + code + (" grade-editable'" if editable else " readonly'") + attrs + "><div class='x-grid3-cell-inner'>" + text + "</div></td>")
            else:
                cells.append("<td class='" + ("concept" if code.startswith("C-") else "numeric readonly") + "'>" + _esc(row["readonly"].get("MBF" if code == "Média Final" else code, "")) + "</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    meta = offering.get("metadata", {})
    fields = [("Docente", "Conta de teste local"), ("Email", "—"), ("Turma", offering["class_name"]), ("Matriculados", sum(row["status"] == "Matriculado" for row in session.rosters[offering_key])),
              ("Curso", meta.get("course") or session.rosters[offering_key][0]["course"] or "—"), ("Disciplina", offering["code"] + " - " + offering["discipline"]), ("Turno", meta.get("shift") or "—"), ("Período Letivo", str(session.year) + "/" + str(meta.get("period") or "0")),
              ("Unid. Física", meta.get("physical_unit") or "—"), ("Unid. Resp.", meta.get("responsible_unit") or "—"), ("Sala", "—"), ("Situação", "Aberta")]
    metadata_html = "".join("<div class='field'><label>" + _esc(label) + ":</label><span>" + _esc(value) + "</span></div>" for label, value in fields)
    formula = _esc(meta.get("evaluation_formula") or "(A1 + AT) / 2")
    count = len(session.rosters[offering_key])
    content = "<div class='instruction'><span class='number'>1</span>Selecionar uma Turma:</div><section class='metadata'>" + metadata_html + "<div class='wide'><a class='another' id='blkTurma.lkmListarTurmas:text' href='" + session.prefix + "/classes'>Selecionar outra turma</a></div></section><div class='instruction'><span class='number'>2</span>Opções:</div><div class='panel-title'>Critérios de Avaliação <span>⌄</span></div><div class='panel-title'>Médias <span>⌄</span></div><div class='panel-title'>Selecionar avaliações a serem exibidas <span>⌄</span></div><div class='column-legend'><strong>Fórmula de avaliação:</strong> " + formula + " <span class='mini'>· A1n: avaliação · ATn: projeto · ARn: recuperação · MBn: média do bimestre</span></div><div class='instruction launch-heading'><span class='number'>3</span>Lançamento de notas:</div><div class='toolbar'><button class='x-btn-text icon-undo' id='undo-notes'>Desfazer</button><button class='x-btn-text icon-save' id='save-notes'>Salvar</button><span class='separator'></span><button class='x-btn-text' type='button' onclick='return false'>Imprimir</button><button class='x-btn-text' type='button' onclick='return false'>Exportar</button><span id='save-status' class='status'>Nenhuma alteração pendente</span></div><div class='x-grid-panel' id='gridNotas'><div class='x-grid3-scroller'><table class='grade-table'><thead><tr>" + header + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div><div class='pager'><span>Todos os alunos da turma</span><span>Exibindo registros 1 - " + str(count) + " de " + str(count) + "</span></div><div class='grid-note'>Uma cópia da origem e controles locais. Médias, recuperações, conceitos e matrículas canceladas são preservados pelo teste.</div></div><div class='links'><a href='#' onclick='return false'>Lançar Faltas</a><a href='#' onclick='return false'>Lançar Diário Eletrônico</a></div>"
    script = "const csrf=" + _json_js(_csrf_token) + ";const saveUrl=" + _json_js(session.prefix + "/grades/" + offering_key + "/save") + ";" + r"""
const pending=new Map();let editor=null;
function finishEditor(commit){if(!editor)return;const {box,input,cell,previous}=editor;editor=null;if(commit){let raw=input.value.trim().replace(',','.');let value=Number(raw);if(raw!==''&&Number.isFinite(value)&&value>=0&&value<=10){cell.querySelector('.x-grid3-cell-inner').textContent=value.toFixed(1);pending.set(cell.dataset.ra+'|'+cell.dataset.code,{registration:cell.dataset.ra,assessment:cell.dataset.code,value});document.getElementById('save-status').textContent='Alterações aguardando Salvar';}else cell.querySelector('.x-grid3-cell-inner').textContent=previous;}box.remove();}
document.querySelectorAll('td.grade-editable .x-grid3-cell-inner').forEach(inner=>inner.addEventListener('click',()=>{finishEditor(true);const cell=inner.parentElement;const rect=cell.getBoundingClientRect();const box=document.createElement('div');box.className='x-layer x-editor x-small-editor x-grid-editor';box.style.left=(rect.left+scrollX)+'px';box.style.top=(rect.top+scrollY)+'px';const input=document.createElement('input');input.type='text';input.className='x-form-text x-form-field';input.value=inner.textContent;box.append(input);document.body.append(box);editor={box,input,cell,previous:inner.textContent};input.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();finishEditor(false);}else if(e.key==='Tab'||e.key==='Enter'){e.preventDefault();finishEditor(true);}});input.addEventListener('blur',()=>finishEditor(true));input.focus();input.select();}));
function ask(message,yes){if(document.querySelector('.x-mask'))return;const mask=document.createElement('div');mask.className='x-mask';const win=document.createElement('div');win.className='x-window x-window-dlg';win.setAttribute('role','dialog');win.setAttribute('aria-modal','true');win.setAttribute('aria-label','Confirmação');win.innerHTML='<div class="x-window-header"><span class="x-window-header-text">Confirmação</span></div><div class="x-window-body"></div><div class="x-window-footer"><button class="x-btn-text" type="button">Sim</button><button class="x-btn-text" type="button">Não</button></div>';win.querySelector('.x-window-body').textContent=message;const buttons=win.querySelectorAll('button');buttons[0].onclick=()=>{mask.remove();yes();};buttons[1].onclick=()=>mask.remove();mask.append(win);document.body.append(mask);buttons[0].focus();}
async function savePending(){const status=document.getElementById('save-status');status.textContent='Salvando no portal de teste…';status.className='status';try{const r=await fetch(saveUrl,{method:'POST',headers:{'Content-Type':'application/json','X-NotaSync-Token':csrf},body:JSON.stringify({changes:[...pending.values()],confirmation:'Deseja salvar as alterações efetuadas?'})});const result=await r.json();if(!r.ok)throw new Error(result.detail||'Não foi possível salvar.');pending.clear();status.textContent='Notas de teste salvas e persistidas';}catch(e){status.className='status error';status.textContent=e.message;}}
document.getElementById('save-notes').addEventListener('click',()=>{finishEditor(true);if(!pending.size){document.getElementById('save-status').textContent='Nenhum registro a ser alterado!';return;}ask('Deseja salvar as alterações efetuadas?',savePending);});
document.getElementById('undo-notes').addEventListener('click',()=>{finishEditor(false);if(pending.size)ask('Deseja desfazer as alterações efetuadas?',()=>location.reload());});
"""
    return _page("Lançar Notas (Incluir ou Alterar)", content, script, session)


@router.post("/test-portal/{identifier}/grades/{offering_key}/save")
async def save_grades(identifier: str, offering_key: str, request: Request):
    session = _session(identifier)
    _authenticated(request, session)
    roster = session.rosters.get(offering_key)
    if roster is None:
        raise HTTPException(404, "Turma de teste não encontrada.")
    body = await request.json()
    changes = body.get("changes") if isinstance(body, dict) else None
    if not isinstance(changes, list) or len(changes) > len(roster) * len(ASSESSMENT_CODES):
        raise HTTPException(400, "Lote de teste inválido.")
    staged, seen = [], set()
    for change in changes:
        if not isinstance(change, dict):
            raise HTTPException(400, "Registro de teste inválido.")
        ra, assessment = change.get("registration"), change.get("assessment")
        if not isinstance(ra, str) or not isinstance(assessment, str) or assessment not in session.assessments or (ra, assessment) in seen:
            raise HTTPException(400, "Coluna ausente, repetida ou protegida no teste.")
        matches = [row for row in roster if row["registration"] == ra]
        if len(matches) != 1 or matches[0]["status"] != "Matriculado":
            raise HTTPException(400, "Matrícula ausente ou cancelada; lote não salvo.")
        value = change.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 10:
            raise HTTPException(400, "Nota de teste fora do intervalo de 0 a 10.")
        staged.append((matches[0], assessment, float(value)))
        seen.add((ra, assessment))
    for row, assessment, value in staged:
        row["grades"][assessment] = value
    session.save_count += 1
    if body.get("confirmation") == "Deseja salvar as alterações efetuadas?":
        session.confirmations += 1
    return {"saved": len(staged), "training": True, "save_count": session.save_count}


class TrainingPortalAdapter(PortalAdapter):
    """The sole adapter permitted to visit this one local training session."""
    def __init__(self, base_url, session, config=None):
        parsed = urlparse(base_url)
        try:
            local = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as exc:
            raise PortalError("O teste precisa usar o endereço local numérico do NotaSync.") from exc
        if parsed.scheme != "http" or not local.is_loopback or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment or not parsed.port:
            raise PortalError("O teste está restrito à origem local do próprio programa.")
        self.training_origin = f"http://{parsed.netloc}"
        self.training_prefix = session.prefix + "/"
        options = {**copy.deepcopy(DEFAULT_CONFIG), **(config or {})}
        options.update(login_url=self.training_origin + session.prefix + "/login", headless=False,
                       target_year=session.year, validated=True, grade_column=session.assessments[0],
                       offerings_container="#grdTurmas", grades_container="#gridNotas",
                       offerings_first_selector="#grdTurmas button.x-tbar-page-first",
                       offerings_next_selector="#grdTurmas button.x-tbar-page-next",
                       another_class_selector='[id="blkTurma.lkmListarTurmas:text"]',
                       save_selector="button.x-btn-text.icon-save", strict_metadata=True)
        super().__init__(options)

    def _check_target(self, url):
        parsed = urlparse(url)
        actual = f"{parsed.scheme}://{parsed.netloc}"
        path = unquote(parsed.path)
        if actual != self.training_origin or not parsed.path.startswith(self.training_prefix) or not path.startswith(self.training_prefix) or any(part in (".", "..") for part in path.split("/")) or "\\" in path or parsed.username or parsed.password:
            raise PortalError("O navegador de teste tentou sair do ambiente local autorizado.")

    def _frame_allowed(self, url):
        try:
            self._check_target(url)
            return True
        except PortalError:
            return False

    def _launch_options(self):
        return {"headless": False, "slow_mo": 8}

    async def open(self, username=LOGIN, password=PASSWORD):
        if username != LOGIN or password != PASSWORD:
            raise PortalError("O ambiente de teste só utiliza a conta fictícia incluída no programa.")
        result = await super().open(LOGIN, PASSWORD)

        async def guard(route):
            try:
                self._check_target(route.request.url)
            except PortalError:
                await route.abort()
            else:
                await route.continue_()
        await self.context.route("**/*", guard)
        if result.get("status") != "waiting_login":
            # The login input can disappear while the next document is still
            # committing. Discovery starts only after the local class grid is
            # visible, rather than racing that navigation and guessing a menu.
            await self.page.locator("#grdTurmas").wait_for(state="visible", timeout=int(self.config["timeout_ms"]))
        return result


async def run_training(base_url, assessments, config, progress=None, source_snapshot=None, test_mode="full"):
    """Test the chosen source coverage through the real browser adapter.

    `source_snapshot` is a list of source records, or {rows: [...], metadata:
    [...]}. It is copied in memory and is never saved with the application.
    `adapter` is private and must be removed before serializing the result.
    """
    session = create_training_session(assessments, int(config.get("target_year", datetime.now().year)),
                                      copy.deepcopy(source_snapshot), test_mode=test_mode)
    checks, adapter = {}, None
    fill_label = ("Preencher a amostra de notas da planilha" if session.test_mode == "quick"
                  else "Preencher todas as notas aptas da planilha")

    def note_progress(phase, filled, verified, *, complete=False):
        total = session.planned_records
        filled = min(total, max(0, filled))
        verified = min(total, max(0, verified))
        messages = {"opening": f"Preparando o teste de {total} notas.",
                    "fill": f"Preenchendo {filled} de {total} notas; {verified} já conferidas.",
                    "save": f"Salvando as notas preenchidas; {verified} de {total} já conferidas.",
                    "verify": f"Conferindo {verified} de {total} notas após recarregar.",
                    "complete": f"Teste concluído: {verified} de {total} notas conferidas."}
        return {"phase": phase, "completed": verified, "filled": filled, "verified": verified,
                "total": total, "percent": 100 if complete else min(99, round((filled + verified) * 100 / max(1, 2 * total), 1)),
                "unit": "notas", "message": messages[phase]}

    async def check(key, label, status="passed", detail="", progress_info=None):
        item = {"id": key, "label": label, "status": status, "detail": detail}
        if progress_info is not None:
            item["progress"] = progress_info
        checks[key] = item
        if progress:
            try:
                result = progress(item)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # Display observers cannot fail, repeat or alter a browser Save.
                pass

    try:
        await check("browser", "Abrir a réplica local da Univap", "running",
                    progress_info=note_progress("opening", 0, 0))
        adapter = TrainingPortalAdapter(base_url, session, config)
        result = await adapter.open()
        if result.get("status") == "waiting_login":
            raise PortalError("A conta fictícia não entrou no portal de teste.")
        await check("browser", "Abrir a réplica local da Univap", detail="Janela visível, grade e campos baseados na tela fornecida.")
        offerings = await adapter.discover()
        if len(offerings) != len(session.offerings):
            raise PortalError("A navegação do teste não localizou todas as turmas da origem.")
        await check("login", "Preencher login e senha fictícios", detail="Nenhuma credencial real foi utilizada.")
        await check("classes", "Localizar as turmas e disciplinas da origem", detail=f"{len(offerings)} turmas/disciplinas identificadas no ambiente local.")
        baseline = session.protected_snapshot()
        expected = copy.deepcopy(session.rosters)
        for key, by_code in session.targets.items():
            for code, targets in by_code.items():
                for row in expected[key]:
                    if row["registration"] in targets:
                        row["grades"][code] = targets[row["registration"]]
        confirmations, batches = 0, 0
        first_batch = None
        for number, offering in enumerate(offerings, 1):
            matching = next((item for item in session.offerings if item["class_name"] == offering["class_name"] and item["discipline"] == offering["discipline"]), None)
            if matching is None:
                raise PortalError("A turma reconhecida pelo navegador não corresponde à origem do teste.")
            key = matching["key"]
            metadata = await adapter.inspect_offering(offering)
            if not all(code in metadata["assessments"] for code in session.assessments):
                raise PortalError("As avaliações selecionadas não foram reconhecidas na tabela de teste.")
            await check("columns", "Identificar os campos exatos da avaliação", detail=", ".join(session.assessments))
            for code in session.assessments:
                targets = session.targets[key][code]
                if not targets:
                    continue
                for step, label in (("fill", fill_label), ("save", "Confirmar Salvar e gravar no portal local"), ("reload", "Reabrir e conferir as notas salvas")):
                    await check(step, label, "running", f"Turma {number}/{len(offerings)} · {code} · {len(targets)} notas · {confirmations} já conferidas")
                rows = await adapter.read_offering(offering, assessment=code)
                by_ra = {row["registration"]: row for row in rows}
                if len(by_ra) != len(session.rosters[key]):
                    raise PortalError("O navegador não leu todas as matrículas da turma de teste.")
                changes = [{"registration": ra, "old": by_ra[ra]["grade"], "new": target, "column": code} for ra, target in targets.items()]

                async def batch_progress(event, before=confirmations, batch_size=len(changes), assessment=code):
                    phase = event.get("phase")
                    done = event.get("completed")
                    if phase not in ("fill", "save", "verify") or type(done) is not int:
                        return
                    done = min(batch_size, max(0, done))
                    filled = before + (done if phase == "fill" else batch_size)
                    verified = before + (done if phase == "verify" else 0)
                    value = note_progress(phase, filled, verified)
                    step, label = {"fill": ("fill", fill_label),
                                   "save": ("save", "Confirmar Salvar e gravar no portal local"),
                                   "verify": ("reload", "Reabrir e conferir as notas salvas")}[phase]
                    await check(step, label, "running", assessment + " · " + value["message"], progress_info=value)

                adapter.progress_callback = batch_progress
                written = await adapter.apply_offering(offering, changes, assessment=code)
                if len(written) != len(changes) or any(not row.get("verified") or row.get("status") != "confirmed" for row in written):
                    raise PortalError("O teste não confirmou todas as notas após salvar e recarregar a página.")
                confirmations += len(written)
                batches += 1
                if first_batch is None:
                    first_batch = (offering, key, code, next(iter(targets)), next(iter(targets.values())))
        adapter.progress_callback = None
        if session.protected_snapshot() != baseline or session.rosters != expected:
            raise PortalError("Uma coluna protegida, matrícula cancelada ou nota fora do lote mudou no teste.")
        if (session.save_count != batches or session.confirmations != batches
                or confirmations != session.planned_records):
            raise PortalError("A gravação do teste não passou pela confirmação de Salvar esperada.")
        for step, label in (("fill", fill_label), ("save", "Confirmar Salvar e gravar no portal local"), ("reload", "Reabrir e conferir as notas salvas")):
            await check(step, label, detail=f"{confirmations} notas confirmadas após salvar e recarregar; {batches} confirmações de Salvar.")
        await check("preservation", "Preservar médias, recuperações e cancelados", detail="AR*, MB*, MBF, EF, NC, conceitos e registros fora do lote permaneceram intactos.")
        if first_batch is None:
            raise PortalError("Nenhuma nota apta foi testada.")
        first, key, code, active_ra, active_value = first_batch
        cancelled_ra = next(row["registration"] for row in session.rosters[key] if row["status"] != "Matriculado")
        cancelled_value = next(row["grades"][code] for row in session.rosters[key] if row["registration"] == cancelled_ra)
        bad_old = 1.0 if active_value == 0 else 0.0
        for check_id, label, reason, changes in (
            ("stale", "Interromper quando uma nota muda", "Uma nota mudou no portal depois da conferência", [{"registration": active_ra, "old": bad_old, "new": 9.9, "column": code}]),
            ("cancelled", "Bloquear matrícula cancelada", "Um aluno não está matriculado", [{"registration": cancelled_ra, "old": cancelled_value, "new": 9.9, "column": code}]),
        ):
            try:
                await adapter.apply_offering(first, changes, assessment=code)
            except PortalError as exc:
                if not str(exc).startswith(reason):
                    raise
                await check(check_id, label, detail="A alteração indevida foi bloqueada antes de salvar.")
            else:
                raise PortalError("O teste não bloqueou uma alteração indevida: " + label)
        if session.protected_snapshot() != baseline or session.rosters != expected or session.save_count != batches:
            raise PortalError("Um bloqueio do teste executou uma gravação inesperada.")
        adapter._require_page()
        title = "Teste resumido aprovado" if session.test_mode == "quick" else "Teste completo aprovado"
        coverage = f"{confirmations} de {session.expected_records} notas aptas testadas"
        explanation = ("A amostra foi salva e conferida, sem alterar as demais notas."
                       if session.test_mode == "quick" else "Todas as notas aptas selecionadas foram testadas, sem amostragem.")
        await adapter.page.evaluate("""({title,detail}) => {
            const notice=document.createElement('section'); notice.className='approval'; notice.setAttribute('role','status');
            const heading=document.createElement('strong'); heading.textContent=title;
            const text=document.createElement('span'); text.textContent=detail+' Volte à janela do NotaSync para executar em tempo real.';
            notice.append(heading,text);document.querySelector('main').prepend(notice);window.scrollTo(0,0);
        }""", {"title": title, "detail": coverage + ". " + explanation})
        await check("complete", title, detail=coverage + ". " + explanation + " Sem acesso à Univap.",
                    progress_info=note_progress("complete", confirmations, confirmations, complete=True))
        return {"passed": True, "checks": list(checks.values()),
                "message": title + ": " + coverage + ". O acesso e as permissões do portal real serão conferidos ao executar.",
                "session_id": session.identifier, "url": base_url.rstrip("/") + session.prefix + "/classes", "confirmed_writes": confirmations,
                "source_mode": session.source_mode, "test_mode": session.test_mode, "assessments": list(session.assessments),
                "counts": {**_coverage(session), "classes": len(offerings), "students": session.source_students, "source_records": session.source_records,
                           "tested_records": confirmations, "confirmed_writes": confirmations, "save_batches": batches,
                           "controls": len(session.controls), "blocked": max(0, session.source_records - session.expected_records),
                           "blocked_records": max(0, session.source_records - session.expected_records)}, "adapter": adapter}
    except Exception as exc:
        message = str(exc).split("Call log:")[0][:650] or "O teste local não foi concluído."
        await check("complete", "Teste local não concluído", "failed", message)
        return {"passed": False, "checks": list(checks.values()), "message": message, "source_mode": session.source_mode, "test_mode": session.test_mode,
                "session_id": session.identifier, "assessments": list(session.assessments), "adapter": adapter}
