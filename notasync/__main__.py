"""Start the local app once and reopen its window on subsequent launches."""
import asyncio
import errno
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import threading
import webbrowser


def _data_directory():
    configured = os.environ.get("NOTASYNC_DATA_DIR")
    if configured:
        return Path(configured)
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "NotaSync"
    return Path.home() / ".local" / "share" / "NotaSync"


def _lock_instance(path):
    """The OS releases this lock even if the application is terminated."""
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if not handle.tell():
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return None
        raise
    return handle


def _release_instance(handle):
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _already_running(metadata):
    """Only open a verified loopback server; stale ports are never trusted."""
    connection = None
    try:
        details = json.loads(metadata.read_text(encoding="utf-8"))
        port = details["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return False
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/api/state")
        response = connection.getresponse()
        # Imports can be large; check the stable prefix without loading records.
        prefix = response.read(32)
        if response.status != 200 or "application/json" not in response.getheader("Content-Type", "") or not prefix.startswith(b'{"imports":'):
            return False
        webbrowser.open(f"http://127.0.0.1:{port}")
        return True
    except (OSError, ValueError, KeyError, http.client.HTTPException):
        return False
    finally:
        if connection:
            connection.close()


def _startup_notice():
    message = "O NotaSync já está iniciando ou encerrando. Aguarde alguns instantes e abra o atalho novamente."
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "NotaSync", 0x40)
    else:
        print(message)


def main():
    directory = _data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    metadata = directory / "instancia.json"
    lock = _lock_instance(directory / "instancia.lock")
    if lock is None:
        if not _already_running(metadata):
            _startup_notice()
        return

    sock = None
    app = None
    port = None
    temporary = metadata.with_name(f"{metadata.name}.{os.getpid()}.tmp")
    try:
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        import uvicorn
        from .app import app

        # Holding the listening socket prevents another app taking this port.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        temporary.write_text(json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8")
        os.replace(temporary, metadata)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning", loop="asyncio")
        server = uvicorn.Server(config)
        app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
        print(f"NotaSync em execução: {url}\nUse Encerrar aplicativo para fechar o programa.")
        if "--no-browser" not in sys.argv:
            timer = threading.Timer(1.0, lambda: webbrowser.open(url))
            timer.daemon = True
            timer.start()

        # Playwright subprocesses require the Windows Proactor event loop.
        if os.name == "nt":
            with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
                runner.run(server.serve(sockets=[sock]))
        else:
            asyncio.run(server.serve(sockets=[sock]))
    finally:
        if app is not None:
            app.state.shutdown_callback = None
        if sock is not None:
            sock.close()
        try:
            details = json.loads(metadata.read_text(encoding="utf-8"))
            if details.get("pid") == os.getpid() and details.get("port") == port:
                metadata.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        temporary.unlink(missing_ok=True)
        _release_instance(lock)


if __name__ == "__main__":
    main()
