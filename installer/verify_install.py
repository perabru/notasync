"""Argument-safe installation verification; never contains portal credentials."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import traceback

EXPECTED_PYTHON = (3, 13, 17)
DOWNLOAD_TIMEOUT = 1200
TITLE = "NotaSync - verificação local"
LOGGER = logging.getLogger("NotaSync.instalacao")


class InstallError(RuntimeError):
    pass


class BrowserUnavailable(InstallError):
    pass


def emit(message: str, **values):
    """ASCII JSON is readable even under Windows console code pages."""
    LOGGER.info(message)
    print(json.dumps({"message": message, **values}, ensure_ascii=True), flush=True)


def setup_environment():
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NotaSync"
    local.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(local / "browsers"))
    os.environ.setdefault("NOTASYNC_DATA_DIR", str(local))
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    log = local / "instalacao.log"
    LOGGER.setLevel(logging.INFO)
    if not LOGGER.handlers:
        handler = logging.FileHandler(log, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOGGER.addHandler(handler)
    return log


def verify_integrity(runtime: Path):
    manifest_path = runtime / "runtime_integrity.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError("O manifesto do ambiente está ausente ou inválido.") from exc
    files = manifest.get("files")
    if manifest.get("algorithm") != "sha256" or not isinstance(files, dict) or not files:
        raise InstallError("O manifesto de integridade não contém os componentes esperados.")
    root = runtime.resolve()
    for relative, expected in files.items():
        if not isinstance(relative, str) or not isinstance(expected, str) or len(expected) != 64:
            raise InstallError("O manifesto de integridade contém um registro inválido.")
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise InstallError("O manifesto contém um caminho fora do ambiente do programa.")
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise InstallError("Um componente do ambiente não foi extraído: " + relative) from exc
        if digest.hexdigest() != expected.lower():
            raise InstallError("Um componente foi alterado ou extraído incompleto: " + relative)
    emit("Integridade dos componentes conferida.", files=len(files))
    return len(files)


def verify_imports():
    for module in ("ssl", "fastapi", "uvicorn", "httpx", "multipart", "playwright.sync_api", "notasync.app"):
        try:
            imported = importlib.import_module(module)
        except Exception as exc:
            raise InstallError("Não foi possível carregar o componente " + module + ".") from exc
        if module == "notasync.app":
            ui = Path(imported.__file__).parent / "static" / "index.html"
            if not ui.is_file() or ui.stat().st_size == 0:
                raise InstallError("A interface do NotaSync não foi extraída corretamente.")
    emit("Bibliotecas e interface carregadas.")


def verify_runtime(runtime=None):
    if tuple(sys.version_info[:3]) != EXPECTED_PYTHON:
        raise InstallError("O ambiente Python não corresponde à versão incluída no instalador.")
    if struct.calcsize("P") != 8:
        raise InstallError("O ambiente do NotaSync precisa ser de 64 bits.")
    verify_integrity(Path(runtime) if runtime is not None else Path(sys.executable).parent)
    verify_imports()
    emit("Ambiente do NotaSync pronto.", python=".".join(map(str, EXPECTED_PYTHON)))


def edge_paths():
    roots = [os.environ.get(name) for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    return [path for root in roots if root
            for path in [Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"]
            if path.is_file()]


def _test_browser(browser_type, **options):
    browser = None
    try:
        browser = browser_type.launch(headless=True, timeout=30000, **options)
        page = browser.new_page()
        page.set_content("<html><head><title>" + TITLE + "</title></head><body>NotaSync</body></html>")
        if page.title() != TITLE:
            raise InstallError("O navegador abriu, mas não concluiu o teste local.")
    finally:
        if browser is not None:
            browser.close()


def find_usable_browser():
    from playwright.sync_api import sync_playwright
    failures = []
    with sync_playwright() as playwright:
        if edge_paths():
            try:
                _test_browser(playwright.chromium, channel="msedge")
                emit("Microsoft Edge conferido; não é necessário baixar outro navegador.", browser="msedge")
                return "msedge"
            except Exception as exc:
                LOGGER.warning("Edge não concluiu a verificação local: %s", exc)
                failures.append("Edge não utilizável nesta configuração")
        try:
            _test_browser(playwright.chromium)
            emit("Navegador de automação conferido.", browser="chromium")
            return "chromium"
        except Exception as exc:
            LOGGER.warning("Chromium não concluiu a verificação local: %s", exc)
            failures.append("Chromium ainda não utilizável")
    raise BrowserUnavailable("; ".join(failures) + ".")


def install_browser():
    emit("Preparando o navegador de automação. Aguarde o download.")
    command = [sys.executable, "-m", "playwright", "install", "chromium"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace")

    def stream_output():
        for line in process.stdout:
            text = line.rstrip()
            if text:
                emit(text, component="browser-download")

    reader = threading.Thread(target=stream_output, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=DOWNLOAD_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise InstallError("O download do navegador excedeu o tempo de espera. Confira a conexão da escola.") from exc
    finally:
        reader.join(timeout=5)
        if process.stdout:
            process.stdout.close()
    if code != 0:
        raise InstallError("O navegador não pôde ser preparado. Confira a internet ou o Microsoft Edge do computador.")


def verify_browser(allow_install=False):
    try:
        return find_usable_browser()
    except BrowserUnavailable:
        if not allow_install:
            raise
    install_browser()
    return find_usable_browser()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Conferir a instalação do NotaSync")
    parser.add_argument("--stage", required=True, choices=("runtime", "browser", "verify"))
    parser.add_argument("--install", action="store_true", help="Permitir preparar o navegador ausente")
    args = parser.parse_args(argv)
    log = setup_environment()
    try:
        emit("Iniciando conferência da instalação.", stage=args.stage)
        if args.stage == "runtime":
            verify_runtime()
        elif args.stage == "browser":
            verify_browser(allow_install=args.install)
        else:
            verify_imports()
            verify_browser(allow_install=False)
        emit("Etapa concluída.", stage=args.stage, status="ready")
        return 0
    except BrowserUnavailable as exc:
        LOGGER.error("%s\n%s", exc, traceback.format_exc())
        emit("Nenhum navegador utilizável foi localizado. " + str(exc), stage=args.stage, status="error", log=str(log))
        return 2 if not args.install else 1
    except Exception as exc:
        LOGGER.error("%s\n%s", exc, traceback.format_exc())
        emit(str(exc) or "A conferência não foi concluída.", stage=args.stage, status="error", log=str(log))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
