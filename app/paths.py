from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ApplicationPaths:
    """Locations used by source runs, portable builds, and installed builds."""

    bundle_root: Path
    runtime_root: Path
    frozen: bool
    installed: bool


def _installed_data_root() -> Path:
    """Return a writable machine-wide data location for the installer edition."""

    program_data = str(os.environ.get("PROGRAMDATA", "")).strip()
    if program_data:
        return Path(program_data).resolve() / "ARCOBALENO"

    local_app_data = str(os.environ.get("LOCALAPPDATA", "")).strip()
    if local_app_data:
        return Path(local_app_data).resolve() / "ARCOBALENO"

    return Path.home().resolve() / "ARCOBALENO"


def detect_application_paths(anchor_file: str | Path) -> ApplicationPaths:
    """Resolve bundled assets separately from writable event data.

    Source mode keeps everything in the project folder. A portable PyInstaller
    build reads bundled templates/static files from ``sys._MEIPASS`` and stores
    config, events, exports, logs, and backups beside the executable.

    The Windows installer places an ``installed.marker`` file beside the EXE.
    Installed builds then keep writable data under ``%PROGRAMDATA%\\ARCOBALENO``
    so normal users can run the server while upgrades safely replace app files.
    """

    frozen = bool(getattr(sys, "frozen", False))
    installed = False

    if frozen:
        executable_root = Path(sys.executable).resolve().parent
        bundle_value = getattr(sys, "_MEIPASS", None)
        bundle_root = Path(bundle_value).resolve() if bundle_value else executable_root
        installed = (executable_root / "installed.marker").exists()
        runtime_root = _installed_data_root() if installed else executable_root
    else:
        bundle_root = Path(anchor_file).resolve().parent
        runtime_root = bundle_root

    return ApplicationPaths(
        bundle_root=bundle_root,
        runtime_root=runtime_root,
        frozen=frozen,
        installed=installed,
    )
