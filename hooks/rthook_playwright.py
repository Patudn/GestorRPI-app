"""
Runtime hook: apunta Playwright al Chromium que está junto al ejecutable.

Estructura Mac:
  GestorRPI_Mac/
    GestorRPI.app/Contents/MacOS/GestorRPI
    browsers/chromium-XXXX/

Estructura Windows:
  GestorRPI_Win/
    GestorRPI.exe
    _internal/
    browsers/chromium-XXXX/
"""
import os
import sys
import platform

if hasattr(sys, "_MEIPASS"):
    system = platform.system()

    if system == "Darwin":
        # _MEIPASS → Contents/MacOS/_internal  (PyInstaller 6.x)
        # Subimos hasta la carpeta de distribución GestorRPI_Mac/
        app_contents = os.path.dirname(sys._MEIPASS)               # Contents/MacOS
        app_bundle   = os.path.dirname(os.path.dirname(app_contents))  # GestorRPI.app
        dist_folder  = os.path.dirname(app_bundle)                  # GestorRPI_Mac/
        browsers_dir = os.path.join(dist_folder, "browsers")
        fallback_dir = os.path.join(os.path.expanduser("~"), "Library", "Caches", "ms-playwright")

    elif system == "Windows":
        # sys.executable → GestorRPI_Win\GestorRPI.exe
        exe_dir      = os.path.dirname(sys.executable)              # GestorRPI_Win\
        browsers_dir = os.path.join(exe_dir, "browsers")
        fallback_dir = os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "ms-playwright"
        )

    else:
        browsers_dir = ""
        fallback_dir = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")

    if browsers_dir and os.path.isdir(browsers_dir):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_dir
    elif fallback_dir and os.path.isdir(fallback_dir):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = fallback_dir
