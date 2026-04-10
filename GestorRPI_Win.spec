# -*- mode: python ; coding: utf-8 -*-
"""
GestorRPI_Win.spec — PyInstaller spec para Windows (x64)
Build via GitHub Actions en windows-latest.
"""

import os
import sys
from pathlib import Path

# ── Rutas clave ───────────────────────────────────────────────────────────────
venv_site = str(Path(sys.executable).parent.parent / "lib" /
                f"python{sys.version_info.major}.{sys.version_info.minor}" /
                "site-packages")

# En el runner de GitHub Actions el venv queda en otra ubicación; usamos site-packages del Python activo
import site
venv_site = site.getsitepackages()[0] if site.getsitepackages() else venv_site

PLAYWRIGHT_PKG = os.path.join(venv_site, "playwright")

# ── Datos a incluir ───────────────────────────────────────────────────────────
datas = [
    # Playwright driver (node binary + package)
    (os.path.join(PLAYWRIGHT_PKG, "driver"), "playwright/driver"),
    # ⚠️  Chromium NO se bundlea adentro del .exe — va en browsers\ junto al exe
    # Ver el workflow de GitHub Actions que lo copia post-build
    ("firebase_auth.py", "."),
    ("auth_routes.py",   "."),
]

# ── Hidden imports ─────────────────────────────────────────────────────────────
hidden_imports = [
    "playwright",
    "playwright.async_api",
    "playwright.sync_api",
    "playwright._impl._api_types",
    "firebase_admin",
    "firebase_admin.auth",
    "firebase_admin.credentials",
    "firebase_admin.firestore",
    "flask",
    "flask.templating",
    "werkzeug",
    "werkzeug.serving",
    "jinja2",
    "pdfplumber",
    "pdfminer",
    "pdfminer.high_level",
    "platformdirs",
    "requests",
    "sqlite3",
    "threading",
    "webbrowser",
]

a = Analysis(
    ["gestor_rpi.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["hooks/rthook_playwright.py"],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GestorRPI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX puede romper binarios de Playwright
    console=False,      # Sin ventana de terminal (windowed app)
    icon=None,          # TODO: agregar icon.ico cuando esté disponible
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GestorRPI",
)
