"""Build the complete private CPython/Windows runtime before making the installer.

The installed machine never runs pip. Every wheel is pinned, checked against its
PyPI SHA-256 record, and installed into a Windows/CPython 3.13 target on the build
machine. The original embeddable Python archive is checked against its published
SHA-256 before expansion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from zipfile import ZipFile, ZIP_DEFLATED


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VENDOR = HERE / "vendor"
PYTHON_ARCHIVE = VENDOR / "python-3.13.17-embed-amd64.zip"
PYTHON_SHA256 = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"
LOCK = ROOT / "requirements-windows.lock"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_expand(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination.resolve()
    with ZipFile(archive) as package:
        bad = package.testzip()
        if bad:
            raise ValueError(f"Corrupt archive: {archive.name}: {bad}")
        for item in package.infolist():
            name = item.filename.replace("\\", "/")
            output = (target / name).resolve()
            if name.startswith("/") or ":" in name or not output.is_relative_to(target):
                raise ValueError(f"Unsafe archive path: {name}")
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"Archive symlink rejected: {name}")
        package.extractall(target)


def locked_projects() -> dict[str, str]:
    result = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, separator, version = line.partition("==")
        if not separator or not name or not version or any(c in version for c in ";<>= "):
            raise ValueError(f"Every dependency must have one exact version: {line}")
        result[name.lower().replace("_", "-")] = version
    return result


def metadata_for_wheel(path: Path) -> tuple[str, str]:
    with ZipFile(path) as wheel:
        files = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(files) != 1:
            raise ValueError(f"Invalid wheel metadata: {path.name}")
        fields = {}
        for line in wheel.read(files[0]).decode("utf-8").splitlines():
            if not line:
                break
            key, sep, value = line.partition(": ")
            if sep and key in ("Name", "Version"):
                fields[key] = value
        return fields["Name"].lower().replace("_", "-"), fields["Version"]


def verify_wheels(directory: Path) -> list[dict]:
    locked = locked_projects()
    result = []
    covered = set()
    for path in sorted(directory.glob("*.whl")):
        name, version = metadata_for_wheel(path)
        if locked.get(name) != version:
            continue
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=45) as response:
            records = json.load(response)["urls"]
        matched = [record for record in records if record["filename"] == path.name]
        if len(matched) != 1:
            raise ValueError(f"Wheel is not published in the exact PyPI release: {path.name}")
        digest = sha256(path)
        if digest != matched[0]["digests"]["sha256"]:
            raise ValueError(f"PyPI SHA-256 mismatch: {path.name}")
        covered.add(name)
        result.append({"project": name, "version": version, "file": path.name,
                       "sha256": digest, "size": path.stat().st_size,
                       "url": matched[0]["url"]})
    if covered != set(locked):
        raise ValueError("Missing verified wheels: " + ", ".join(sorted(set(locked) - covered)))
    return result


def run(args: list[str]) -> None:
    subprocess.run(args, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--runtime-dir", type=Path, default=VENDOR / "runtime")
    parser.add_argument("--output", type=Path, default=VENDOR / "runtime_libs.zip")
    arguments = parser.parse_args()
    wheels = VENDOR / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    target_options = ["--platform", "win_amd64", "--python-version", "313",
                      "--implementation", "cp", "--abi", "cp313", "--only-binary=:all:"]
    if not arguments.skip_download:
        run([sys.executable, "-m", "pip", "download", *target_options,
             "--dest", str(wheels), "--index-url", "https://pypi.org/simple",
             "--disable-pip-version-check", "-r", str(LOCK)])
    records = verify_wheels(wheels)
    if not PYTHON_ARCHIVE.is_file() or sha256(PYTHON_ARCHIVE) != PYTHON_SHA256:
        raise ValueError("The official Python runtime archive is absent or failed SHA-256 validation")

    with tempfile.TemporaryDirectory(prefix="notasync-runtime-") as temporary:
        build = Path(temporary) / "runtime"
        site = build / "Lib" / "site-packages"
        safe_expand(PYTHON_ARCHIVE, build)
        run([sys.executable, "-m", "pip", "install", *target_options,
             "--no-deps", "--no-index", "--find-links", str(wheels), "--no-compile",
             "--no-warn-script-location", "--disable-pip-version-check",
             "--target", str(site), "-r", str(LOCK)])
        # Generated Linux console launchers are deliberately not deployed. The
        # application and Playwright run as modules in the private interpreter.
        shutil.rmtree(site / "bin", ignore_errors=True)
        (build / "python313._pth").write_text(
            "python313.zip\n.\nLib\\site-packages\n..\\app\nimport site\n", encoding="utf-8")
        required = [build / "python.exe", build / "pythonw.exe", build / "python313.dll",
                    build / "vcruntime140.dll", build / "vcruntime140_1.dll",
                    site / "playwright" / "driver" / "node.exe",
                    site / "fastapi" / "__init__.py", site / "uvicorn" / "__init__.py"]
        for path in required:
            if not path.is_file():
                raise ValueError(f"Incomplete runtime: {path.relative_to(build)}")
        extensions = list(site.rglob("*.pyd"))
        if not extensions or any(".so" == p.suffix for p in site.rglob("*")):
            raise ValueError("The runtime contains incorrect native extensions")
        for native in [build / "python.exe", site / "playwright" / "driver" / "node.exe", *extensions]:
            data = native.read_bytes()
            if data[:2] != b"MZ":
                raise ValueError(f"Invalid Windows PE: {native}")
            offset = int.from_bytes(data[0x3C:0x40], "little")
            if data[offset:offset + 4] != b"PE\0\0" or int.from_bytes(data[offset + 4:offset + 6], "little") != 0x8664:
                raise ValueError(f"Expected Windows AMD64 native file: {native}")

        integrity = {path.relative_to(build).as_posix(): sha256(path)
                     for path in sorted(build.rglob("*")) if path.is_file()}
        (build / "runtime_integrity.json").write_text(
            json.dumps({"algorithm": "sha256", "files": integrity}, indent=2), encoding="utf-8")

        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(arguments.output, "w", ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(site.rglob("*")):
                if path.is_file():
                    archive.write(path, Path("Lib") / "site-packages" / path.relative_to(site))
        with ZipFile(arguments.output) as archive:
            if archive.testzip():
                raise ValueError("The dependency payload failed CRC validation")
        if arguments.runtime_dir.exists():
            shutil.rmtree(arguments.runtime_dir)
        shutil.copytree(build, arguments.runtime_dir)

    manifest = {"python": {"version": "3.13.17", "archive": PYTHON_ARCHIVE.name,
                           "sha256": PYTHON_SHA256}, "target": "CPython 3.13 / Windows AMD64",
                "wheels": records, "libraries_archive": {"file": arguments.output.name,
                "sha256": sha256(arguments.output), "size": arguments.output.stat().st_size},
                "runtime_files": sum(p.is_file() for p in arguments.runtime_dir.rglob("*"))}
    (VENDOR / "runtime_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"runtime": str(arguments.runtime_dir), "archive": str(arguments.output),
                      "archive_bytes": arguments.output.stat().st_size,
                      "verified_wheels": len(records), "runtime_files": manifest["runtime_files"]}))


if __name__ == "__main__":
    main()
