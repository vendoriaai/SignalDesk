# PyInstaller spec for the SignalDesk app (roadmap Phase 4.23).
# Build:  pyinstaller packaging/signaldesk.spec
# Output: dist/signaldesk<onefile exe/dir per platform>
#
# Bundles: backend (FastAPI/uvicorn), agent stack, built React UI
# (signaldesk/ui/dist). Keys stay in the OS keychain at runtime.
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
ROOT = Path(SPECPATH).parent  # noqa: F821  (SPECPATH set by pyinstaller)
PKG = ROOT / "signaldesk"

datas = [
    (str(PKG / "ui" / "dist"), "signaldesk/ui/dist"),
]

hiddenimports = [
    # every signaldesk submodule: sandbox child processes import them at runtime
    # (static analysis can't see the generated scripts)
    *collect_submodules("signaldesk"),
    # uvicorn internals are dynamic-imported
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.wsproto_impl", "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    # sqlmodel/sqlalchemy sqlite dialect
    "sqlalchemy.dialects.sqlite",
    # keyring backends per-OS
    "keyring.backends.Windows", "keyring.backends.macOS", "keyring.backends.SecretService",
    # tools lazy imports
    "yfinance", "httpx", "curl_cffi",
    # desktop shell
    "webview",
]

a = Analysis(
    [str(PKG / "cli.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "ruff", "matplotlib.tests", "numpy.tests", "pandas.tests",
              # UI toolkit bindings we don't use (pywebview uses native Edge WKWebView etc.)
              "PyQt5", "PyQt6", "PySide2", "PySide6", "tkinter",
              "sphinx", "IPython", "jupyter", "notebook",
              ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="signaldesk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="signaldesk",
)
