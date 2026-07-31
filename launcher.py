from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

from app import __version__
from app.paths import detect_application_paths

APPLICATION_PATHS = detect_application_paths(__file__)
PROJECT_ROOT = APPLICATION_PATHS.bundle_root
RUNTIME_ROOT = APPLICATION_PATHS.runtime_root
VENV_DIR = RUNTIME_ROOT / ".venv"
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
REQUIREMENTS_MARKER = VENV_DIR / ".arcobaleno_requirements.sha256"
RUNTIME_FLAG = "--arcobaleno-runtime"

RUNTIME_DIRECTORIES = (
    RUNTIME_ROOT / "events" / "current",
    RUNTIME_ROOT / "events" / "archive",
    RUNTIME_ROOT / "exports",
    RUNTIME_ROOT / "logs",
    RUNTIME_ROOT / "backups",
    RUNTIME_ROOT / "fonts",
)


def _banner() -> None:
    print()
    print("=" * 64)
    edition = "Installed Event Server" if APPLICATION_PATHS.installed else "Portable Event Photo Experience"
    print(f"  ARCOBALENO {__version__.upper()}  |  {edition}")
    print("=" * 64)
    print()


def _status(message: str) -> None:
    print(f"[ARCOBALENO] {message}", flush=True)


def _ensure_runtime_directories() -> None:
    for directory in RUNTIME_DIRECTORIES:
        directory.mkdir(parents=True, exist_ok=True)


def _bootstrap_log(error: BaseException) -> None:
    try:
        log_dir = RUNTIME_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "bootstrap-errors.log"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("\n" + "=" * 80 + "\n")
            handle.write(datetime.now().isoformat(timespec="seconds") + "\n")
            handle.write("".join(traceback.format_exception(error)))
    except OSError:
        pass


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _python_is_working(python_path: Path) -> bool:
    if not python_path.exists():
        return False
    try:
        completed = subprocess.run(
            [str(python_path), "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _create_virtual_environment() -> None:
    python_path = _venv_python()
    if VENV_DIR.exists() and not _python_is_working(python_path):
        _status("A damaged virtual environment was found. Rebuilding it...")
        shutil.rmtree(VENV_DIR, ignore_errors=True)

    if _python_is_working(python_path):
        _status("Virtual environment ready.")
        return

    _status("Creating the private ARCOBALENO environment...")
    completed = subprocess.run(
        [sys.executable, "-m", "venv", str(VENV_DIR)],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if completed.returncode != 0 or not _python_is_working(python_path):
        raise RuntimeError(
            "Python could not create the ARCOBALENO virtual environment. "
            "Try repairing or reinstalling Python with the 'venv' component enabled."
        )
    _status("Virtual environment created.")


def _requirements_digest() -> str:
    if not REQUIREMENTS_FILE.exists():
        raise FileNotFoundError(f"Missing dependency file: {REQUIREMENTS_FILE}")
    return hashlib.sha256(REQUIREMENTS_FILE.read_bytes()).hexdigest()


def _install_dependencies_if_needed() -> None:
    expected_digest = _requirements_digest()
    current_digest = ""
    if REQUIREMENTS_MARKER.exists():
        current_digest = REQUIREMENTS_MARKER.read_text(encoding="utf-8").strip()

    if current_digest == expected_digest:
        _status("Dependencies ready.")
        return

    _status("Installing required packages. The first launch may take a few minutes...")
    python_path = _venv_python()
    completed = subprocess.run(
        [
            str(python_path),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--requirement",
            str(REQUIREMENTS_FILE),
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Dependency installation failed. Check the internet connection, then "
            "double-click START_ARCOBALENO.bat again."
        )

    REQUIREMENTS_MARKER.write_text(expected_digest, encoding="utf-8")
    _status("Dependencies installed.")


def _run_inside_virtual_environment() -> int:
    command = [str(_venv_python()), str(Path(__file__).resolve()), RUNTIME_FLAG]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    return completed.returncode


def _run_application() -> int:
    from app.runtime import run

    return run(PROJECT_ROOT, RUNTIME_ROOT)


def main() -> int:
    _banner()
    _ensure_runtime_directories()

    if RUNTIME_FLAG in sys.argv:
        return _run_application()

    if APPLICATION_PATHS.frozen:
        if APPLICATION_PATHS.installed:
            _status("Starting installed ARCOBALENO server...")
        else:
            _status("Starting packaged ARCOBALENO...")
        return _run_application()

    _create_virtual_environment()
    _install_dependencies_if_needed()
    _status("Starting ARCOBALENO...")
    return _run_inside_virtual_environment()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[ARCOBALENO] Server stopped by operator.")
        raise SystemExit(0)
    except BaseException as error:
        _bootstrap_log(error)
        print()
        print("[ARCOBALENO] STARTUP FAILED")
        print(f"[ARCOBALENO] {error}")
        print(f"[ARCOBALENO] Details were written to: {RUNTIME_ROOT / 'logs' / 'bootstrap-errors.log'}")
        raise SystemExit(1)
