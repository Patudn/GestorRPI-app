# -*- mode: python ; coding: utf-8 -*-
"""
GestorRPI.spec — PyInstaller spec para Mac (Apple Silicon / Intel)
Incluye Playwright + Chromium bundleados para que el usuario no instale nada.
"""

import os
import sys
from pathlib import Path

# ── Rutas clave ───────────────────────────────────────────────────────────────
venv_site = str(Path(sys.executable).parent.parent / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages")

PLAYWRIGHT_PKG   = os.path.join(venv_site, "playwright")
CHROMIUM_DIR     = os.path.expanduser("~/Library/Caches/ms-playwright/chromium-1208")

# ── Datos a incluir ───────────────────────────────────────────────────────────
datas = [
    # Playwright driver (node binary + package)
    (os.path.join(PLAYWRIGHT_PKG, "driver"), "playwright/driver"),
    # ⚠️  Chromium NO se bundlea adentro del .app — va en la carpeta de distribución
    # Ver post-build script que lo copia junto al .app
    # Módulos propios
    ("firebase_auth.py",  "."),
    ("auth_routes.py",    "."),
]

# ── Hidden imports necesarios ─────────────────────────────────────────────────
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
    upx=False,         # UPX puede romper binarios de Playwright
    console=False,     # Sin ventana de terminal
    icon=None,
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

app = BUNDLE(
    coll,
    name="GestorRPI.app",
    icon=None,
    bundle_identifier="com.patudn.gestorrpi",
    info_plist={
        "CFBundleName":              "GestorRPI",
        "CFBundleDisplayName":       "GestorRPI",
        "CFBundleShortVersionString": "3.0.0",
        "CFBundleVersion":           "3.0.0",
        "NSHighResolutionCapable":   True,
        "LSUIElement":               False,   # Aparece en el Dock
    },
)
