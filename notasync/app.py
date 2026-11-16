from __future__ import annotations

import asyncio
import copy
import csv
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .importer import analyze_workbook
from .portal import DEFAULT_CONFIG, PortalAdapter
from . import __version__

ASSESSMENTS = ("A11", "AT1", "A12", "AT2", "A13", "AT3", "A14", "AT4")

MAX_BYTES = 30 * 1024 * 1024
TOKEN = secrets.token_urlsafe(32)
DATA_DIR = Path(os.environ.get("NOTASYNC_DATA_DIR") or
                (Path(os.environ["LOCALAPPDATA"]) / "NotaSync" if os.name == "nt" else Path.home() / ".local" / "share" / "NotaSync"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
AUDIT_PATH = DATA_DIR / "historico.jsonl"


def norm(value):
    return " ".join(unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().upper().split())


def registration(value):
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    if re.fullmatch(r"\d+", text):
        return text
    try:
        number = Decimal(text)
        if number.is_finite() and number >= 0 and number == number.to_integral_value():
            return format(number, "f").split(".")[0]
    except InvalidOperation:
        pass
    return text if re.fullmatch(r"\d+", text) else ""


def grade(value):
    if value is None or str(value).strip() in ("", "-", "—"):
        return None
    try:
        result = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation:
        raise ValueError("Nota não numérica")
    if not result.is_finite() or not 0 <= result <= 10:
        raise ValueError("Nota fora do intervalo de 0 a 10")
    return result.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def public_config():
    return copy.deepcopy(state["config"])


def load_config():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.setdefault("target_year", 2026)
    config.setdefault("discipline_aliases", {
        "REDES": "Redes de Computadores", "BD": "Banco de Dados",
        "PDM": "Programação para Dispositivo Móvel", "CLD I": "Circuitos Lógico-Digitais I",
        "CLD II": "Circuitos Lógico-Digitais II", "CLD1": "Circuitos Lógico-Digitais I",
        "CLDII": "Circuitos Lógico-Digitais II",
    })
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            aliases = {**config["discipline_aliases"], **saved.pop("discipline_aliases", {})}
            config.update(saved)
            config["discipline_aliases"] = aliases
        except (OSError, ValueError):
            pass
    config["validated"] = False  # leitura autenticada precisa ser feita em cada sessão
    return config


state = {"imports": [], "portal": {"status": "disconnected", "message": "Conecte sua conta do portal.", "classes": []},
         "jobs": [], "demo": False, "config": load_config(), "plan": None, "revision": 0,
         "audit": [], "adapter": None, "task": None, "_test_adapter": None,
         "validation": {"status": "idle", "message": "Escolha pular o teste, testar resumido ou testar completo.",
                        "checks": [], "assessments": [], "test_id": None, "scope": "local_simulation"}}


def invalidate(reset_validation=True):
    state["plan"] = None
    state["revision"] += 1
    if reset_validation:
        invalidate_test("Planilha ou configuração alterada. Escolha novamente como continuar.")


def invalidate_test(message="Escolha novamente como continuar."):
    old = state.get("validation", {})
    state["validation"] = {"status": "outdated" if old.get("status") not in (None, "idle") else "idle",
                           "message": message, "checks": old.get("checks", []), "assessments": [],
                           "test_id": None, "fingerprint": None, "scope": "local_simulation"}


def test_fingerprint(assessments):
    material = {"version": __version__, "assessments": [c for c in ASSESSMENTS if c in assessments],
                "sources": state["imports"],
                "config": {k: v for k, v in state["config"].items() if k != "validated"}, "demo": state["demo"]}
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_local_test(assessments, test_id):
    """Require the user's current execution choice: skip, quick pass or full pass."""
    proof = state.get("validation", {})
    expected = test_fingerprint(assessments)
    valid_choice = ((proof.get("status") == "passed" and proof.get("test_mode") in ("quick", "full"))
                    or (proof.get("status") == "skipped" and proof.get("test_mode") == "skip"))
    if (not valid_choice or not test_id or test_id != proof.get("test_id")
            or proof.get("fingerprint") != expected or set(proof.get("assessments", [])) != set(assessments)
            or (not state["demo"] and proof.get("source_mode") != "imported_sheet")):
        raise HTTPException(409, "Escolha Pular teste ou use Testar resumido/completo com esta planilha e estas avaliações antes de executar.")


def training_snapshot(assessments):
    """Freeze the selected local rows; the training portal never needs credentials."""
    rows, metadata = [], []
    for code in assessments:
        records, blocked = source_records(code)
        blocked_ids = {(r.get("source_file"), r.get("source_sheet"), r.get("source_cell"), r.get("registration"))
                       for r in blocked}
        for row in iter_source_rows(code):
            item = copy.deepcopy(row)
            item["source_discipline"] = item.get("portal_discipline") or item.get("source_discipline") or ""
            ra = registration(item.get("registration"))
            identity = (item.get("source_file"), item.get("source_sheet"), item.get("source_cell"), ra)
            key = source_record_key(item, code, ra)
            target = records.get(key)
            item["registration"] = ra
            item["eligible"] = bool(target and identity not in blocked_ids and item.get("eligible") is not False)
            if item["eligible"]:
                item["grade"] = target["new"]
            rows.append(item)
    for imported in state["imports"]:
        for sheet in imported.get("sheets", []):
            portal_metadata = copy.deepcopy(sheet.get("portal_metadata", sheet.get("metadata", {})))
            metadata.append({**portal_metadata, "source_file": imported["filename"], "sheet": sheet.get("sheet", ""),
                             "class_name": sheet.get("class_name", ""), "discipline": sheet.get("discipline", ""),
                             "source_format": sheet.get("source_format", ""),
                             "metadata": portal_metadata,
                             "portal_columns": copy.deepcopy(sheet.get("portal_columns", []))})
    targets = {(norm(row.get("class_name") or row.get("source_sheet") or "TURMA TESTE"),
                norm(row.get("source_discipline") or row.get("discipline") or row["assessment"]),
                row["registration"], row["assessment"])
               for row in rows if row.get("eligible")}
    return {"rows": rows, "metadata": metadata, "expected_records": len(targets)}


async def close_test_browser():
    adapter = state.get("_test_adapter")
    state["_test_adapter"] = None
    if adapter:
        await adapter.close()


def busy():
    if state["task"] and not state["task"].done():
        raise HTTPException(409, "Há uma operação em andamento. Aguarde sua conclusão.")


def notify_finished(message, failed=False):
    """A background run can finish while the local app is minimized."""
    if os.name != "nt":
        return

    def popup():
        try:
            import ctypes
            import winsound
            winsound.MessageBeep(winsound.MB_ICONHAND if failed else winsound.MB_ICONASTERISK)
            ctypes.windll.user32.MessageBoxW(None, str(message), "NotaSync", 0x10 if failed else 0x40)
        except Exception:
            # A desktop without interactive notifications still has the app banner.
            pass
    threading.Thread(target=popup, name="NotaSyncAviso", daemon=True).start()


def _safe_message(error, sensitive=()):
    message = str(error).split("Call log:")[0][:700]
    for value in sensitive:
        if value:
            message = message.replace(str(value), "[oculto]")
    return message or "Operação interrompida."


def set_progress(job, phase, completed=0, total=None, *, unit="notas", percent=None,
                 filled=None, verified=None, message=None):
    """Publish observed work only; an unknown total remains indeterminate."""
    total = max(0, int(total)) if isinstance(total, (int, float)) else None
    completed = max(0, int(completed))
    if total is not None:
        completed = min(completed, total)
    value = None if percent is None else max(0.0, min(100.0 if phase == "finished" else 99.0, float(percent)))
    result = {"phase": phase, "completed": completed, "total": total, "percent": value,
              "unit": unit, "message": message or job.get("message", "Em andamento…")}
    if filled is not None:
        result["filled"] = min(max(0, int(filled)), total) if total is not None else max(0, int(filled))
    if verified is not None:
        result["verified"] = min(max(0, int(verified)), total) if total is not None else max(0, int(verified))
    job["progress"] = result
    return result


def start_job(kind, coroutine, *, notify_on_finish=False):
    busy()
    job = {"id": secrets.token_hex(6), "kind": kind, "status": "running", "message": "Em andamento…"}
    set_progress(job, "preparing", unit="etapas")
    state["jobs"] = (state["jobs"] + [job])[-12:]

    async def runner():
        try:
            await coroutine(job)
            job["status"] = "completed"
            current = job.get("progress", {})
            total = current.get("total")
            set_progress(job, "finished", total or 0, total, unit=current.get("unit", "etapas"),
                         percent=100, filled=current.get("filled"), verified=current.get("verified"))
        except Exception as exc:
            # Não incluir credenciais, HTML, cookies ou pilhas de chamadas.
            job.update(status="failed", message=_safe_message(exc))
            if kind in ("open", "discover", "launch") and state["portal"].get("status") != "waiting_login":
                state["portal"].update(status="attention", message=job["message"])
        finally:
            if notify_on_finish:
                notify_finished(job["message"], failed=job["status"] == "failed")
    state["task"] = asyncio.create_task(runner())
    return job


def audit(items):
    for item in items:
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **item}
        state["audit"].append(entry)
        if not state["demo"]:
            with AUDIT_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


@asynccontextmanager
async def lifespan(app):
    yield
    await close_test_browser()
    if state["adapter"]:
        await state["adapter"].close()


app = FastAPI(title="NotaSync", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    if request.url.hostname not in ("127.0.0.1", "localhost", "testserver"):
        return JSONResponse({"detail": "Este programa atende apenas no próprio computador."}, status_code=403)
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        return JSONResponse({"detail": "Origem não autorizada."}, status_code=403)
    if request.method not in ("GET", "HEAD") and not secrets.compare_digest(request.headers.get("x-notasync-token", ""), TOKEN):
        return JSONResponse({"detail": "Reabra a janela do NotaSync."}, status_code=403)
    try:
        body_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        return JSONResponse({"detail": "Tamanho de solicitação inválido."}, status_code=400)
    if body_length > MAX_BYTES * 2:
        return JSONResponse({"detail": "Arquivo muito grande. Limite: 30 MB por planilha."}, status_code=413)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    return response


@app.get("/", response_class=HTMLResponse)
def index():
    content = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    return content.replace("__NOTASYNC_TOKEN__", TOKEN)


@app.get("/api/state")
def get_state():
    return {**{key: state[key] for key in ("imports", "portal", "jobs", "demo")},
            "assessments": assessment_catalog(), "validation": state.get("validation", {"status": "idle", "checks": []})}


@app.post("/api/test/invalidate")
async def clear_test(request: Request):
    busy()
    body = await request.json()
    invalidate_test(str(body.get("reason", "Seleção alterada. Escolha novamente como continuar."))[:250])
    await close_test_browser()
    return state["validation"]


def test_assessments(body):
    selected = body.get("assessments", []) if isinstance(body, dict) else []
    if not isinstance(selected, list) or not selected or any(c not in ASSESSMENTS for c in selected):
        raise HTTPException(400, "Selecione as avaliações que deseja testar.")
    assessments = [code for code in ASSESSMENTS if code in selected]
    for code in assessments:
        sources, _ = source_records(code)
        if not any(sources.values()):
            raise HTTPException(400, f"Não há notas aptas de {code} na planilha. Confira a origem antes de testar.")
    return assessments


@app.post("/api/test/skip")
async def skip_local_test(request: Request):
    busy()
    assessments = test_assessments(await request.json())
    await close_test_browser()
    snapshot = training_snapshot(assessments)
    expected = snapshot["expected_records"]
    summary = {"students": len({r["registration"] for r in snapshot["rows"] if r["registration"]}),
               "classes": len({(norm(r.get("class_name")), norm(r.get("source_discipline") or r.get("discipline")))
                               for r in snapshot["rows"] if r.get("eligible")}),
               "source_records": len(snapshot["rows"]), "expected_records": expected,
               "planned_records": 0, "tested_records": 0, "confirmed_writes": 0,
               "sampled_out_records": expected, "coverage_mode": "none", "coverage_complete": False,
               "source_files": list(dict.fromkeys(r.get("source_file", "Planilha") for r in snapshot["rows"]))}
    message = "Teste pulado por você. O programa conferirá as notas antes de lançar no portal real."
    state["validation"] = {"status": "skipped", "test_mode": "skip", "message": message, "checks": [],
                           "assessments": assessments, "test_id": secrets.token_urlsafe(24),
                           "fingerprint": test_fingerprint(assessments), "scope": "local_simulation",
                           "source_mode": "synthetic_demo" if state["demo"] else "imported_sheet",
                           "created_at": datetime.now(timezone.utc).isoformat(), "summary": summary}
    state["jobs"] = (state["jobs"] + [{"id": secrets.token_hex(6), "kind": "test", "test_mode": "skip",
                                       "status": "completed", "message": message, "summary": summary}])[-12:]
    return state["validation"]


@app.post("/api/test")
async def test_local_portal(request: Request):
    busy()
    body = await request.json()
    assessments = test_assessments(body)
    test_mode = body.get("mode", "full")
    if test_mode not in ("quick", "full"):
        raise HTTPException(400, "Escolha teste resumido ou completo. Para pular, use Pular teste.")
    await close_test_browser()
    revision = state["revision"]
    fingerprint = test_fingerprint(assessments)
    snapshot = training_snapshot(assessments)
    from .training import get_training_plan
    try:
        coverage = get_training_plan(snapshot, assessments, test_mode)
    except ValueError as exc:
        raise HTTPException(400, _safe_message(exc)) from None
    source_files = list(dict.fromkeys(row.get("source_file", "Planilha") for row in snapshot["rows"]))
    identifier = secrets.token_urlsafe(24)
    state["validation"] = {"status": "running", "message": "Abrindo o portal local de teste…", "checks": [],
                           "assessments": assessments, "test_id": None, "fingerprint": None,
                           "created_at": datetime.now(timezone.utc).isoformat(), "scope": "local_simulation",
                           "test_mode": test_mode,
                           "source_mode": "synthetic_demo" if state["demo"] else "imported_sheet"}
    base_url = str(request.base_url).rstrip("/")

    async def operation(job):
        from .training import run_training

        async def progress(check):
            checks = state["validation"]["checks"]
            for index, existing in enumerate(checks):
                if existing["id"] == check["id"]:
                    checks[index] = check
                    break
            else:
                checks.append(check)
            job["message"] = check.get("detail") or check.get("label") or "Testando…"
            state["validation"]["message"] = job["message"]
            update = check.get("progress")
            if isinstance(update, dict):
                set_progress(job, update.get("phase", check["id"]), update.get("completed", 0),
                             update.get("total"), unit=update.get("unit", "notas"), percent=update.get("percent"),
                             filled=update.get("filled"), verified=update.get("verified"),
                             message=update.get("message") or job["message"])
            elif not job.get("progress", {}).get("total"):
                set_progress(job, check["id"], unit="etapas")

        try:
            result = await run_training(base_url, assessments, copy.deepcopy(state["config"]), progress=progress,
                                        source_snapshot=copy.deepcopy(snapshot), test_mode=test_mode)
            state["_test_adapter"] = result.pop("adapter", None)
            checks = result.get("checks", [])
            if state["revision"] != revision or test_fingerprint(assessments) != fingerprint:
                raise ValueError("A planilha foi alterada durante o teste. Execute Testar novamente.")
            required = {"browser", "login", "classes", "columns", "fill", "save", "reload", "preservation", "stale", "cancelled", "complete"}
            counts = result.get("counts", {})
            if not result.get("passed"):
                raise ValueError(result.get("message") or "O teste local não foi concluído.")
            if (not required.issubset({c.get("id") for c in checks})
                    or any(c.get("status") != "passed" for c in checks)):
                raise ValueError("O teste local não concluiu todas as verificações obrigatórias.")
            if result.get("source_mode") != "imported_sheet":
                raise ValueError("O teste não utilizou a cópia da planilha selecionada. Faça um novo teste.")
            if result.get("test_mode") != test_mode:
                raise ValueError("O resultado não corresponde ao modo de teste escolhido.")
            if (type(counts.get("tested_records")) is not int
                    or counts["tested_records"] != coverage["planned_records"] or counts["tested_records"] <= 0
                    or type(counts.get("confirmed_writes")) is not int
                    or counts["confirmed_writes"] != counts["tested_records"]
                    or any(type(counts.get(key)) is not int or counts[key] != coverage[key]
                           for key in ("expected_records", "planned_records", "sampled_out_records"))
                    or type(counts.get("coverage_complete")) is not bool
                    or counts["coverage_complete"] != coverage["coverage_complete"]
                    or counts.get("coverage_mode") != coverage["coverage_mode"]):
                raise ValueError("O teste não confirmou a quantidade de notas prevista para este modo. A execução continua bloqueada.")
            summary = {**counts, "source_files": source_files}
            if test_mode == "quick":
                message = f"Teste resumido aprovado: {counts['tested_records']} de {counts['expected_records']} notas aptas testadas."
            else:
                message = f"Teste completo aprovado: {counts['tested_records']} notas aptas testadas."
            message += " O acesso e as turmas reais serão conferidos ao executar."
            state["validation"].update(status="passed", checks=checks, test_id=identifier, fingerprint=fingerprint,
                                       message=message,
                                       session_id=result.get("session_id"), summary=summary)
            job.update(message=state["validation"]["message"], stage="finished", summary={**summary, "checks": len(checks)})
        except Exception as exc:
            await close_test_browser()
            state["validation"].update(status="failed", test_id=None, fingerprint=None, message=_safe_message(exc))
            raise
    return start_job("test", operation)


def iter_source_rows(assessment=None):
    for imported in state["imports"]:
        for sheet in imported.get("sheets", []):
            for student in sheet.get("students", []):
                code = student.get("assessment") or sheet.get("assessment") or imported.get("mode")
                if code not in ASSESSMENTS or (assessment and code != assessment):
                    continue
                yield {**student, "assessment": code, "column": code,
                       "discipline": student.get("discipline", sheet.get("discipline", code if code.startswith("AT") else "")),
                       "source_sheet": sheet.get("sheet", ""), "source_file": imported["filename"]}


def assessment_catalog():
    counts = {code: 0 for code in ASSESSMENTS}
    for row in iter_source_rows():
        counts[row["assessment"]] += 1
    inspected = state["portal"].get("assessment_columns", {})
    complete = bool(inspected) and len(inspected) == len(state["portal"].get("classes", [])) and all(v is not None for v in inspected.values())
    seen = {code for columns in inspected.values() if columns for code in columns}
    return [{"code": code, "label": f"{code} · {'Projetos' if code.startswith('AT') else 'Avaliações'} do {code[-1]}º bimestre",
             "source_records": counts[code], "portal_available": (code in seen) if complete else None,
             "available": counts[code] > 0 and (not complete or code in seen)} for code in ASSESSMENTS]


@app.get("/api/assessments")
def get_assessments():
    return assessment_catalog()


@app.get("/api/config")
def get_config():
    return public_config()


@app.put("/api/config")
async def put_config(request: Request):
    busy()
    config = await request.json()
    if not isinstance(config, dict):
        raise HTTPException(400, "A configuração deve ser um objeto JSON.")
    if any(re.search(r"password|senha|cookie|token|secret", str(k), re.I) for k in config):
        # Campos CSS password_selector são permitidos; valores de senha nunca.
        forbidden = [k for k in config if re.search(r"password|senha|cookie|token|secret", str(k), re.I) and not str(k).endswith("selector")]
        if forbidden:
            raise HTTPException(400, "Não salve credenciais na configuração.")
    url = config.get("login_url", DEFAULT_CONFIG.get("login_url"))
    if urlparse(url).scheme != "https" or urlparse(url).hostname != "portal.univap.br":
        raise HTTPException(400, "O acesso configurado precisa pertencer a https://portal.univap.br.")
    config["validated"] = False
    state["config"].update(config)
    if state["adapter"]:
        await state["adapter"].close()
        state["adapter"] = None
    state["portal"] = {"status": "disconnected", "message": "Configuração salva. Reconecte o portal.", "classes": []}
    CONFIG_PATH.write_text(json.dumps(state["config"], ensure_ascii=False, indent=2), encoding="utf-8")
    invalidate()
    return {"message": "Configuração salva neste computador."}


def import_bytes(content, filename, mode):
    if mode not in (*ASSESSMENTS, "AUTO"):
        raise HTTPException(400, "Avaliação não reconhecida. Use A11/AT1 até A14/AT4, ou detecção automática.")
    if not content or len(content) > MAX_BYTES:
        raise HTTPException(400, "Arquivo vazio ou acima do limite de 30 MB.")
    if not filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Use Excel .xlsx. Para planilhas antigas .xls, salve uma cópia .xlsx.")
    try:
        result = analyze_workbook(content, Path(filename).name, mode)
    except Exception as exc:
        raise HTTPException(400, f"Não foi possível ler a planilha: {str(exc)[:350]}") from exc
    result["id"] = hashlib.sha256(content).hexdigest()[:16]
    return result


@app.post("/api/import")
async def upload(files: list[UploadFile] = File(...), mode: str = Form("AUTO")):
    busy()
    if state["demo"]:
        await reset_demo()
    if len(files) > 6:
        raise HTTPException(400, "Envie no máximo seis planilhas por vez.")
    results = []
    for file in files:
        content = await file.read(MAX_BYTES + 1)
        results.append(await asyncio.to_thread(import_bytes, content, file.filename or "planilha.xlsx", mode))
    filenames = {i["filename"] for i in results}
    if mode == "AUTO":
        state["imports"] = [i for i in state["imports"] if i["filename"] not in filenames] + results
    else:
        state["imports"] = [i for i in state["imports"] if i["mode"] != mode and i["filename"] not in filenames] + results
    invalidate()
    return {"imports": results, "message": "Planilha lida. Conecte o portal para comparar e lançar."}


@app.post("/api/imports/remove")
async def remove_import(request: Request):
    busy()
    body = await request.json()
    identifier = body.get("id") if isinstance(body, dict) else None
    if not identifier:
        raise HTTPException(400, "Selecione a planilha que deseja retirar desta execução.")
    state["imports"] = [item for item in state["imports"] if item.get("id") != identifier]
    invalidate()
    return {"message": "Planilha retirada desta execução. O arquivo original permanece no computador."}


async def validate_public_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Informe um link HTTPS de download, sem usuário ou senha na URL.")
    try:
        answers = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError("Não foi possível localizar o endereço da planilha.")
    if not answers or any(not ipaddress.ip_address(a[4][0]).is_global for a in answers):
        raise ValueError("Links de rede interna não são aceitos. Selecione o arquivo Excel no computador.")


async def download_workbook(url):
    parsed = urlparse(url)
    match = re.match(r"/spreadsheets/d/([\w-]+)", parsed.path)
    if parsed.hostname == "docs.google.com" and match:
        url = f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=xlsx"
    async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as client:
        for _ in range(6):
            await validate_public_url(url)
            async with client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = str(response.url.join(response.headers["location"]))
                    continue
                if response.status_code != 200:
                    raise ValueError("O link não liberou o download. Para planilhas privadas, baixe o Excel e selecione o arquivo.")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("A planilha excede 30 MB.")
                if not data.startswith(b"PK"):
                    raise ValueError("O link retornou uma página de acesso, não um Excel. Baixe a planilha e selecione o arquivo.")
                return bytes(data)
    raise ValueError("O link tem redirecionamentos demais. Selecione o arquivo Excel.")


@app.post("/api/import-link")
async def import_link(request: Request):
    busy()
    body = await request.json()
    mode = body.get("mode", "AUTO")
    try:
        content = await download_workbook(str(body.get("url", "")))
        result = await asyncio.to_thread(import_bytes, content, "planilha_por_link.xlsx", mode)
    except httpx.HTTPError:
        raise HTTPException(400, "Não foi possível baixar a planilha. Confira o acesso e a internet, ou selecione o arquivo Excel.")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if state["demo"]:
        await reset_demo()
    state["imports"] = [i for i in state["imports"] if i["filename"] != result["filename"] and (mode == "AUTO" or i["mode"] != mode)] + [result]
    invalidate()
    return {"imports": [result], "message": "Planilha carregada pelo link."}


@app.post("/api/portal/open")
async def open_portal(request: Request):
    body = await request.json()
    username, password = str(body.get("username", "")), str(body.get("password", ""))
    body.clear()
    busy()
    if state["demo"]:
        await reset_demo()
    invalidate()

    async def operation(job):
        if state["adapter"]:
            await state["adapter"].close()
        state["adapter"] = PortalAdapter(state["config"], DATA_DIR)
        state["portal"].update(status="connecting", message="Abrindo navegador do portal…", classes=[])
        result = await state["adapter"].open(username, password)
        state["portal"].update(result or {})
        state["portal"].setdefault("classes", [])
        if username and password and state["portal"]["status"] != "waiting_login":
            job["message"] = "Acesso enviado. Localizando as turmas e disciplinas…"
            try:
                offerings = await state["adapter"].discover()
                state["portal"].update(status="connected", message=f"{len(offerings)} turmas/disciplinas localizadas.", classes=offerings)
            except Exception:
                state["portal"].update(status="opened", message="Portal aberto. Confira a tela de acesso no navegador e clique em localizar turmas.")
        job["message"] = state["portal"].get("message", "Portal aberto. Localize as turmas.")
    return start_job("open", operation)


@app.post("/api/portal/discover")
async def discover():
    if state["demo"]:
        return {"classes": state["portal"]["classes"]}
    if not state["adapter"]:
        raise HTTPException(400, "Abra o portal primeiro.")

    async def operation(job):
        invalidate()
        job["message"] = "Localizando turmas e disciplinas acessíveis à sua conta…"
        offerings = await state["adapter"].discover()
        state["portal"].update(status="connected", message=f"{len(offerings)} turmas/disciplinas localizadas.", classes=offerings)
        job["message"] = state["portal"]["message"]
    return start_job("discover", operation)


def source_records(mode):
    records = {}
    blocked = []
    for row in iter_source_rows(mode):
        ra = registration(row.get("registration"))
        key = source_record_key(row, mode, ra)
        shared = source_is_shared(row, mode)
        row["scope"] = "shared" if shared else "discipline"
        if not shared:
            row["discipline"] = source_discipline(row, mode)
        row["registration"] = ra
        row["student"] = row.get("name", "")
        issues = row.get("issues", [])
        errors = [i.get("message", "Pendência na origem") if isinstance(i, dict) else str(i)
                  for i in issues if not isinstance(i, dict) or i.get("severity") == "error" or i.get("code") == "inactive_student"]
        if not shared and not norm(row.get("discipline")):
            errors.append("A disciplina desta avaliação não foi identificada na planilha.")
        row["warnings"] = [i.get("message", "") for i in issues if isinstance(i, dict) and i.get("severity") == "warning"]
        if not ra or errors or row.get("eligible") is False or norm(row.get("status")) in ("CANCELADO", "TRANCADO", "TRANSFERIDO"):
            blocked.append({**row, "status": "blocked", "reason": "; ".join(errors) or "Matrícula não ativa ou nota pendente na planilha."})
            continue
        try:
            value = grade(row.get("grade"))
            if value is None:
                raise ValueError("Nota ausente")
            row["new"] = float(value)
        except ValueError as exc:
            blocked.append({**row, "status": "blocked", "reason": str(exc)})
            continue
        if key in records:
            prior = records[key]
            if prior is None:
                continue
            if grade(prior["new"]) != value:
                blocked.append({**row, "status": "blocked", "reason": "A mesma matrícula tem notas divergentes nas fontes selecionadas."})
                records[key] = None
        else:
            records[key] = row
    return records, blocked


def source_record_key(row, assessment, ra=None):
    """Keep explicit exported classes/periods distinct while projects remain shared."""
    key = (ra if ra is not None else registration(row.get("registration")),
           assessment if source_is_shared(row, assessment) else norm(source_discipline(row, assessment)))
    if row.get("source_format") == "portal_export" or row.get("year") not in (None, ""):
        key += (str(row.get("year") or ""), str(row.get("period") or ""),
                norm(row.get("class_name")) if row.get("source_format") == "portal_export" else "")
    return key


def source_discipline(row, assessment):
    if assessment.startswith("AT"):
        return row.get("portal_discipline") or row.get("source_discipline") or (
            row.get("discipline", "") if norm(row.get("discipline")) not in ASSESSMENTS else "")
    return row.get("discipline", "")


def source_is_shared(row, assessment):
    return (assessment.startswith("AT") and row.get("source_format") != "portal_export"
            and not norm(source_discipline(row, assessment)))


def source_matches_offering(row, offering):
    """Only apply explicit metadata; older project sheets can have short class labels."""
    source_year = row.get("year")
    if source_year not in (None, ""):
        match = re.search(r"\b(20\d{2})\b", str(source_year))
        if not match or not re.search(rf"\b{match[1]}\b", str(offering.get("year", ""))):
            return False
    if row.get("source_format") == "portal_export":
        source_class = norm(row.get("class_name"))
        if source_class and source_class != norm(offering.get("class_name")):
            return False
    source_period = str(row.get("period") if row.get("period") is not None else "").strip()
    target_period = offering.get("period", offering.get("academic_period"))
    if source_period and target_period not in (None, "") and source_period != str(target_period).strip():
        return False
    if source_period not in ("", "0") and target_period in (None, ""):
        return False
    return True


def matches_discipline(source, offering):
    text = re.sub(r"^A1[1-4]\s*[-:]?\s*", "", norm(source)).strip()
    aliases = {norm(k): norm(v) for k, v in state["config"].get("discipline_aliases", {}).items()}
    if text in aliases:
        return aliases[text] == norm(offering)
    if text == norm(offering):
        return True
    # Siglas são aceitas apenas quando geradas do nome completo efetivamente
    # encontrado no portal e identificam uma única disciplina no acesso atual.
    # Não são usados aproximação, prefixos ou semelhança entre palavras.
    if not re.fullmatch(r"[A-Z0-9]{2,12}", text):
        return False
    ignored = {"A", "AS", "O", "OS", "DE", "DA", "DAS", "DO", "DOS", "E", "PARA", "EM", "COM", "AO", "AOS"}
    romans = {"I": "1", "II": "2", "III": "3", "IV": "4"}

    def code_key(code):
        match = re.fullmatch(r"([A-Z]{2,}?)(IV|III|II|I)", code)
        return match[1] + romans[match[2]] if match else code

    def acronym(title):
        words = re.findall(r"[A-Z0-9]+", norm(title))
        return "".join(romans[word] if word in romans else word if word.isdigit() else word[0]
                       for word in words if word not in ignored)

    candidates = {norm(item["discipline"]) for item in state["portal"].get("classes", [])
                  if acronym(item.get("discipline", "")) == code_key(text)}
    return len(candidates) == 1 and norm(offering) in candidates


@app.post("/api/exit")
async def exit_app():
    busy()
    await close_test_browser()
    if state["adapter"]:
        await state["adapter"].close()
        state["adapter"] = None
    callback = getattr(app.state, "shutdown_callback", None)
    if callback:
        asyncio.get_running_loop().call_later(0.2, callback)
    return {"message": "NotaSync encerrado. Você pode fechar esta janela."}


async def build_plan(class_ids, mode, job, progress_offset=0, progress_total=None):
    if mode not in ASSESSMENTS:
        raise ValueError("Selecione uma avaliação de A11/AT1 até A14/AT4.")
    if not any(iter_source_rows(mode)):
        raise ValueError(f"Selecione a planilha {mode} antes de comparar.")
    offerings = state["portal"]["classes"]
    selected = [x for x in offerings if not class_ids or x["id"] in class_ids]
    if not selected:
        raise ValueError("Localize e selecione as turmas antes de comparar.")
    sources, blocked = source_records(mode)
    changes, unchanged = [], []
    matched = set()
    successful = []
    revision = state["revision"]
    if state["adapter"]:
        state["adapter"].config["grade_column"] = mode
    for index, offering in enumerate(selected):
        job["message"] = f"Lendo {index + 1}/{len(selected)}: {offering['discipline']} — {offering['class_name']}"
        set_progress(job, "compare", progress_offset + index, progress_total or len(selected), unit="turmas")
        if not any(row and source_matches_offering(row, offering)
                   and (source_is_shared(row, mode) or matches_discipline(row["discipline"], offering["discipline"]))
                   for row in sources.values()):
            continue
        year = str(offering.get("year", ""))
        if not re.search(rf"\b{int(state['config']['target_year'])}\b", year):
            blocked.append({"class_name": offering["class_name"], "discipline": offering["discipline"], "status": "blocked", "reason": "Período letivo não confirmado para o ano selecionado."})
            continue
        try:
            columns = state["portal"].get("assessment_columns", {}).get(offering["id"])
            if columns is not None and mode not in columns:
                blocked.append({"class_name": offering["class_name"], "discipline": offering["discipline"], "column": mode, "status": "blocked", "reason": f"A coluna {mode} não existe nesta turma do portal."})
                continue
            roster = copy.deepcopy(state.get("demo_rosters", {}).get(offering["id"], [])) if state["demo"] else await state["adapter"].read_offering(offering)
            if state["demo"]:
                for row in roster:
                    if isinstance(row.get("grades"), dict):
                        row["grade"] = row["grades"].get(mode)
        except Exception as exc:
            blocked.append({"class_name": offering["class_name"], "discipline": offering["discipline"], "status": "blocked", "reason": str(exc).split("Call log:")[0][:500]})
            continue
        counts = {}
        for row in roster:
            ra = registration(row.get("registration"))
            counts[ra] = counts.get(ra, 0) + 1
        if not roster:
            blocked.append({"class_name": offering["class_name"], "discipline": offering["discipline"], "reason": "Nenhum aluno lido; turma não liberada para lançamento.", "status": "blocked"})
            continue
        successful.append(offering["id"])
        for target in roster:
            ra = registration(target.get("registration"))
            options = [(key, row) for key, row in sources.items()
                       if key[0] == ra and row and source_matches_offering(row, offering)
                       and (source_is_shared(row, mode) or matches_discipline(row["discipline"], offering["discipline"]))]
            if not options:
                continue  # aluno sem nota nesta fonte nunca é alterado
            base = {"registration": ra, "student": target.get("name", ""), "name": target.get("name", ""),
                    "class_id": offering["id"], "class_name": offering["class_name"], "discipline": offering["discipline"], "column": mode}
            if len(options) != 1 or not ra or counts[ra] != 1:
                blocked.append({**base, "status": "blocked", "reason": "Matrícula ou disciplina ambígua; nenhum lançamento feito."})
                continue
            key, source = options[0]
            matched.add(key)
            if norm(target.get("status")) not in ("MATRICULADO", "ATIVO", "REGULAR"):
                blocked.append({**base, "status": "blocked", "reason": "Situação da matrícula no portal não está ativa ou não foi identificada."})
                continue
            try:
                current = grade(target.get("grade"))
            except ValueError:
                blocked.append({**base, "status": "blocked", "reason": "A nota atual do portal não pôde ser interpretada."})
                continue
            entry = {**base, "old": float(current) if current is not None else None, "new": source["new"],
                     "scope": source["scope"],
                     "source_sheet": source["source_sheet"], "source_cell": source.get("source_cell"),
                     "source_file": source["source_file"], "warnings": source.get("warnings", [])}
            if current == grade(source["new"]):
                unchanged.append({**entry, "status": "unchanged"})
            else:
                changes.append({**entry, "status": "pending"})
    if any(entry.get("scope") != "shared" for entry in changes + unchanged):
        placements = {}
        for entry in changes + unchanged:
            if entry.get("scope") == "shared":
                continue
            source_id = (entry["registration"], norm(entry["discipline"]), entry["source_file"], entry["source_sheet"], entry.get("source_cell"))
            placements.setdefault(source_id, set()).add(entry["class_id"])
        ambiguous = {key for key, classes in placements.items() if len(classes) > 1}
        if ambiguous:
            def keep_unique(entries):
                kept = []
                for entry in entries:
                    key = (entry["registration"], norm(entry["discipline"]), entry["source_file"], entry["source_sheet"], entry.get("source_cell"))
                    if key in ambiguous:
                        blocked.append({**entry, "status": "blocked",
                                        "reason": "A avaliação corresponde a mais de uma turma. Informe a turma na origem para identificar o lançamento."})
                    else:
                        kept.append(entry)
                return kept
            changes, unchanged = keep_unique(changes), keep_unique(unchanged)
    for key, row in sources.items():
        if row and key not in matched:
            blocked.append({**row, "status": "blocked", "reason": "Matrícula, disciplina, turma ou período da origem não localizado no acesso desta conta; não será lançado."})
    if state["revision"] != revision:
        raise ValueError("A fonte foi alterada durante a comparação. Compare novamente.")
    if state["adapter"] and successful:
        state["adapter"].config["validated"] = True
    plan = {"plan_id": secrets.token_hex(16), "mode": mode, "revision": revision, "consumed": False,
            "created_at": datetime.now(timezone.utc).isoformat(), "changes": changes, "blocked": blocked, "unchanged": unchanged,
            "summary": {"changes": len(changes), "blocked": len(blocked), "unchanged": len(unchanged), "classes": len(successful)},
            "demo": state["demo"]}
    state["plan"] = plan
    job["message"] = f"Comparação concluída: {len(changes)} alterações, {len(unchanged)} já iguais e {len(blocked)} pendências."
    set_progress(job, "compare", progress_offset + len(selected), progress_total or len(selected), unit="turmas")
    return plan


async def build_multi_plan(class_ids, assessments, job):
    revision = state["revision"]
    parts = []
    offering_count = len([item for item in state["portal"]["classes"] if not class_ids or item["id"] in class_ids])
    for index, code in enumerate(assessments):
        job["assessment"] = code
        parts.append(await build_plan(class_ids, code, job, index * offering_count, len(assessments) * offering_count))
    if revision != state["revision"]:
        raise ValueError("A planilha mudou durante a leitura. Inicie novamente.")
    changes = [row for part in parts for row in part["changes"]]
    blocked = [row for part in parts for row in part["blocked"]]
    unchanged = [row for part in parts for row in part["unchanged"]]
    result = {"plan_id": secrets.token_hex(16), "assessments": list(assessments), "mode": assessments[0] if len(assessments) == 1 else "MULTI",
              "revision": revision, "consumed": False, "created_at": datetime.now(timezone.utc).isoformat(),
              "changes": changes, "blocked": blocked, "unchanged": unchanged, "demo": state["demo"],
              "summary": {"changes": len(changes), "blocked": len(blocked), "unchanged": len(unchanged),
                          "classes": max((p["summary"]["classes"] for p in parts), default=0)}}
    state["plan"] = result
    return result


@app.post("/api/portal/plan")
async def plan(request: Request):
    body = await request.json()
    if not state["demo"] and not state["adapter"]:
        raise HTTPException(400, "Conecte o portal primeiro.")
    return start_job("plan", lambda job: build_plan(body.get("class_ids", []), body.get("mode", "AT4"), job))


@app.get("/api/plan")
def get_plan():
    return state["plan"] or {"plan_id": None, "changes": [], "blocked": [], "unchanged": [], "summary": {}}


async def execute_plan(plan, job):
    """Consume a compared revision once, then save and verify every changed grade."""
    if state["plan"] is not plan or plan["revision"] != state["revision"] or plan["consumed"]:
        raise ValueError("A comparação expirou ou já foi usada. Compare novamente.")
    if not state["demo"] and not state["adapter"]:
        raise ValueError("Reconecte o portal e compare novamente.")
    plan["consumed"] = True
    if not plan["changes"]:
        count = len(plan["unchanged"])
        pending = len(plan["blocked"])
        if pending and not count and not plan["summary"].get("classes"):
            raise ValueError("Nenhuma turma/avaliação pôde ser conferida no portal. Nenhuma nota foi lançada; consulte as pendências.")
        job["summary"] = {"saved": 0, "unchanged": count, "pending": pending, "classes": plan["summary"].get("classes", 0)}
        job["message"] = f"Concluído: nenhuma nota para atualizar. {count} já iguais e {pending} pendências."
        set_progress(job, "finished", 0, 0, percent=100, filled=0, verified=0)
        return job["summary"]
    by_offering = {}
    for entry in plan["changes"]:
        by_offering.setdefault((entry["class_id"], entry["column"]), []).append(entry)
    offerings = {x["id"]: x for x in state["portal"]["classes"]}
    completed = 0
    verified_before = 0
    total_notes = len(plan["changes"])
    set_progress(job, "fill", 0, total_notes, percent=0, filled=0, verified=0)
    for batch_key, entries in by_offering.items():
        class_id, assessment = batch_key
        job["message"] = f"Lançando e conferindo {completed + 1}/{len(by_offering)}: {assessment} — {offerings[class_id]['class_name']}"
        if state["demo"]:
            for entry in entries:
                for row in state["demo_rosters"][class_id]:
                    if row["registration"] == entry["registration"]:
                        if isinstance(row.get("grades"), dict):
                            row["grades"][assessment] = entry["new"]
                        else:
                            row["grade"] = entry["new"]
                entry.update(status="simulated", message="Demonstração: nenhum dado enviado ao portal.")
            results = entries
        else:
            try:
                if hasattr(state["adapter"], "config"):
                    state["adapter"].config["grade_column"] = assessment
                async def observed(event):
                    phase = event.get("phase", "fill")
                    within = max(0, min(len(entries), int(event.get("completed", 0))))
                    filled = verified_before + (within if phase == "fill" else len(entries))
                    verified = verified_before + (within if phase == "verify" else 0)
                    percent = 100 * (filled + verified) / (2 * total_notes)
                    set_progress(job, phase, verified, total_notes, filled=filled, verified=verified,
                                 percent=percent, message=job["message"])
                adapter = state["adapter"]
                previous_callback = getattr(adapter, "progress_callback", None)
                adapter.progress_callback = observed
                try:
                    results = await adapter.apply_offering(offerings[class_id], entries)
                finally:
                    adapter.progress_callback = previous_callback
                if not isinstance(results, list) or len(results) != len(entries):
                    raise ValueError("Resposta de gravação incompleta; confira o portal antes de repetir.")
            except Exception as exc:
                message = str(exc).split("Call log:")[0][:500]
                for entry in entries:
                    entry.update(status="unknown", message=message)
                audit(entries)
                # interromper; não repetir envio cujo resultado pode ter sido parcial
                for later_id, later_entries in by_offering.items():
                    if later_id != batch_key and all(e["status"] == "pending" for e in later_entries):
                        for later in later_entries:
                            later.update(status="not_sent", message="Interrompido após falha na turma anterior.")
                        audit(later_entries)
                raise ValueError("Lançamento interrompido. Há resultado não confirmado. Consulte o histórico e compare novamente. " + message)
        for entry, result in zip(entries, results):
            entry.update({k: v for k, v in result.items() if k in ("status", "message", "verified")})
        audit(entries)
        completed += 1
        if not state["demo"] and any(e["status"] not in ("confirmed", "saved", "verified", "success", "unchanged", "already_equal") for e in entries):
            for later_id, later_entries in by_offering.items():
                if later_id != batch_key and all(e["status"] == "pending" for e in later_entries):
                    for later in later_entries:
                        later.update(status="not_sent", message="Interrompido após gravação não confirmada na turma anterior.")
                    audit(later_entries)
            raise ValueError("Uma gravação não foi confirmada. O envio foi interrompido; compare novamente antes de continuar.")
        verified_before += len(entries)
        set_progress(job, "verify", verified_before, total_notes, filled=verified_before,
                     verified=verified_before, percent=100 * verified_before / total_notes)
    saved = sum(entry["status"] in ("confirmed", "saved", "verified", "success", "already_equal", "simulated") for entry in plan["changes"])
    job["summary"] = {"saved": saved, "unchanged": len(plan["unchanged"]), "pending": len(plan["blocked"]), "classes": len({key[0] for key in by_offering})}
    prefix = "Demonstração concluída" if state["demo"] else "Lançamento concluído e conferido"
    job["message"] = f"{prefix}: {saved} notas atualizadas, {len(plan['unchanged'])} já iguais e {len(plan['blocked'])} pendências."
    if state["demo"]:
        job["message"] += " Nenhum portal foi alterado."
    return job["summary"]


@app.post("/api/portal/execute")
async def execute(request: Request):
    busy()
    body = await request.json()
    plan = state["plan"]
    if not plan or plan["plan_id"] != body.get("plan_id") or plan["revision"] != state["revision"] or plan["consumed"]:
        raise HTTPException(409, "A comparação expirou ou já foi usada. Compare novamente.")
    if not plan["changes"]:
        raise HTTPException(400, "Não há alterações para lançar.")
    if not state["demo"] and not state["adapter"]:
        raise HTTPException(400, "Reconecte o portal e compare novamente.")
    require_local_test(plan.get("assessments") or [plan.get("mode")], body.get("test_id"))
    return start_job("execute", lambda job: execute_plan(plan, job))


@app.post("/api/launch")
async def launch(request: Request):
    busy()
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Informe login, senha e avaliação.")
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    selected = body.get("assessments", [body.get("mode", "AT4")])
    test_id = body.get("test_id")
    show_browser = body.get("show_browser", True)
    body.clear()
    if not isinstance(selected, list) or not selected or any(not isinstance(code, str) or code not in ASSESSMENTS for code in selected):
        raise HTTPException(400, "Selecione as avaliações de A11/AT1 até A14/AT4 que deseja lançar.")
    assessments = [code for code in ASSESSMENTS if code in selected]
    if not isinstance(show_browser, bool):
        raise HTTPException(400, "A opção Mostrar navegador deve estar marcada ou desmarcada.")
    for code in assessments:
        if not any(iter_source_rows(code)):
            raise HTTPException(400, f"A avaliação {code} não foi encontrada na planilha. Selecione uma fonte com o cabeçalho correspondente.")
    if not state["demo"] and (not username or not password):
        raise HTTPException(400, "Informe o login e a senha do professor ou coordenador.")
    if not any(any(row for row in source_records(code)[0].values()) for code in assessments):
        raise HTTPException(400, "A planilha não possui notas aptas para lançamento. Confira as pendências da origem.")
    require_local_test(assessments, test_id)
    invalidate(reset_validation=False)

    async def operation(job):
        nonlocal username, password
        adapter = None
        try:
            await close_test_browser()
            if not state["demo"]:
                if state["adapter"]:
                    await state["adapter"].close()
                    state["adapter"] = None
                session_config = copy.deepcopy(state["config"])
                session_config.update(headless=not show_browser, validated=False, grade_column=assessments[0])
                adapter = PortalAdapter(session_config, DATA_DIR)
                state["adapter"] = adapter
                state["portal"] = {"status": "connecting", "message": "Entrando no portal…", "classes": []}
                job.update(stage="login", show_browser=show_browser, message="Entrando no portal…")
                set_progress(job, "login", unit="etapas")
                try:
                    result = await adapter.open(username, password)
                except Exception as exc:
                    raise ValueError(_safe_message(exc, (username, password))) from None
                finally:
                    username = password = ""
                state["portal"].update(result or {})
                if state["portal"].get("status") == "waiting_login":
                    message = ("O portal ainda solicita autenticação. Marque Mostrar navegador e tente novamente."
                               if not show_browser else "O portal ainda solicita autenticação. Confira o acesso no navegador e tente novamente.")
                    state["portal"].update(status="waiting_login", message=message)
                    raise ValueError(message)
                job.update(stage="discover", message="Localizando todas as turmas e disciplinas…")
                set_progress(job, "discover", unit="turmas")
                offerings = await adapter.discover()
                if not offerings:
                    raise ValueError("Nenhuma turma ou disciplina acessível foi localizada para esta conta.")
                state["portal"].update(status="connected", classes=offerings,
                                       message=f"{len(offerings)} turmas/disciplinas localizadas.")
                if hasattr(adapter, "inspect_offering"):
                    catalogue = {}
                    state["portal"]["assessment_columns"] = catalogue
                    for index, offering in enumerate(offerings):
                        job.update(stage="columns", message=f"Conferindo avaliações {index + 1}/{len(offerings)} — {offering['class_name']}")
                        set_progress(job, "columns", index, len(offerings), unit="turmas")
                        try:
                            information = await adapter.inspect_offering(offering)
                            catalogue[offering["id"]] = [code for code in information["assessments"] if code in ASSESSMENTS]
                        except Exception:
                            catalogue[offering["id"]] = None
            else:
                # Demonstration credentials never leave the program.
                username = password = ""
            job.update(stage="compare", message="Comparando as notas de todas as turmas…")
            plan = await build_multi_plan([], assessments, job)
            job.update(stage="save", message="Atualizando e conferindo as notas…")
            await execute_plan(plan, job)
            job["stage"] = "finished"
            state["portal"].update(status="completed", message=job["message"])
        except Exception as exc:
            raise ValueError(_safe_message(exc, (username, password))) from None
        finally:
            username = password = ""
            if adapter and not show_browser:
                try:
                    await adapter.close()
                except Exception:
                    pass
                if state["adapter"] is adapter:
                    state["adapter"] = None
    return start_job("launch", operation, notify_on_finish=not show_browser)


async def reset_demo():
    state["demo"] = False
    state["imports"] = []
    state["audit"] = []
    state["portal"] = {"status": "disconnected", "message": "Conecte o portal.", "classes": []}
    invalidate()


@app.post("/api/demo")
async def demo():
    busy()
    if state["adapter"]:
        await state["adapter"].close()
        state["adapter"] = None
    await reset_demo()
    state["demo"] = True
    students = [
        {"key": "90000001", "registration": "90000001", "name": "Aluno Exemplo A", "class_name": "1F", "grade": 8.5, "status": "Matriculado", "issues": [], "warnings": [], "source_cell": "H4", "components": []},
        {"key": "90000002", "registration": "90000002", "name": "Aluno Exemplo B", "class_name": "1F", "grade": 7, "status": "Matriculado", "issues": [], "warnings": [], "source_cell": "H5", "components": []},
        {"key": "90000003", "registration": "90000003", "name": "Aluno Exemplo C", "class_name": "1F", "grade": 0, "status": "Matriculado", "issues": [], "warnings": [], "source_cell": "H6", "components": []},
    ]
    state["imports"] = [{"id": "demo", "filename": "Exemplo fictício AT4.xlsx", "mode": "AT4", "sheets": [{"sheet": "1F", "class_name": "1F", "discipline": "AT4", "headers": [], "students": students, "issues": []}], "issues": [], "summary": {"students": 3, "records": 3, "valid": 3, "pending": 0}}]
    offerings = [{"id": "demo-redes", "class_name": "1F", "discipline": "Redes de Computadores", "year": "2026"}, {"id": "demo-logica", "class_name": "1F", "discipline": "Lógica de Programação", "year": "2026"}]
    state["portal"] = {"status": "connected", "message": "Demonstração com dados fictícios; sem conexão com a Univap.", "classes": offerings}
    state["demo_rosters"] = {o["id"]: [{"registration": s["registration"], "name": s["name"], "status": "Matriculado", "grade": 7 if n == 1 else 5} for n, s in enumerate(students)] for o in offerings}
    return get_state()


@app.get("/api/report.csv")
def report():
    entries = list(state["audit"])
    if not state["demo"] and AUDIT_PATH.exists():
        entries = []
        for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                pass
    output = io.StringIO()
    columns = {"timestamp": "Data UTC", "student": "Aluno", "registration": "Matrícula", "class_name": "Turma", "discipline": "Disciplina", "column": "Avaliação", "old": "Nota anterior", "new": "Nota lançada", "status": "Situação", "message": "Detalhe", "source_file": "Planilha", "source_cell": "Célula"}
    writer = csv.writer(output, delimiter=";")
    writer.writerow(columns.values())
    for row in entries:
        values = []
        for key in columns:
            value = str(row.get(key, "") if row.get(key) is not None else "")
            if value.startswith(("=", "+", "-", "@", "\t", "\r")):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="NotaSync_historico.csv"'})


from .training import configure_training, router as training_router
configure_training(TOKEN)
app.include_router(training_router)
