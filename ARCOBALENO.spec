# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

PROJECT_ROOT = Path(SPEC).resolve().parent
ICON_FILE = PROJECT_ROOT / "assets" / "ARCOBALENO.ico"
VERSION_FILE = PROJECT_ROOT / "windows_version_info.txt"

heif_datas, heif_binaries, heif_hiddenimports = collect_all("pillow_heif")
qr_datas, qr_binaries, qr_hiddenimports = collect_all("qrcode")

added_data = [
    (str(PROJECT_ROOT / "templates"), "templates"),
    (str(PROJECT_ROOT / "static"), "static"),
]
added_data += heif_datas + qr_datas

added_binaries = heif_binaries + qr_binaries
hidden_imports = heif_hiddenimports + qr_hiddenimports

analysis = Analysis(
    [str(PROJECT_ROOT / "launcher.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=added_binaries,
    datas=added_data,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="ARCOBALENO",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    icon=str(ICON_FILE),
    version=str(VERSION_FILE),
)

bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ARCOBALENO",
)
