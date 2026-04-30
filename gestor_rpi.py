# -*- coding: utf-8 -*-
"""
Gestor RPI Integral - v3.0
Autor: Francisco Di Nardo (PatuDN)
Sistema unificado: Flask (UI web) + SQLite (base de datos) + Playwright (automatización RPI)
"""

import os
import sys
import re
import json
import asyncio
import sqlite3
import pdfplumber
import webbrowser
import threading
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, jsonify, session as flask_session
from playwright.async_api import async_playwright
from firebase_auth import get_valid_token, check_subscription

# =====================================================
# CONFIGURACIÓN DE RUTAS (sin .env — datos por usuario)
# =====================================================
try:
    from platformdirs import user_data_dir, user_documents_dir
    USER_DATA_DIR = user_data_dir("GestorRPI", "PatuDN")
    INFORMES_DIR  = os.path.join(user_documents_dir(), "GestorRPI", "informes")
except ImportError:
    # Fallback si platformdirs no está instalado
    home = os.path.expanduser("~")
    USER_DATA_DIR = os.path.join(home, ".gestorrpi")
    INFORMES_DIR  = os.path.join(home, "Documents", "GestorRPI", "informes")

CONFIG_FILE   = os.path.join(USER_DATA_DIR, "config.json")
DB_PATH       = os.path.join(USER_DATA_DIR, "tramites.db")
LOG_FILE      = os.path.join(USER_DATA_DIR, "descargados.txt")
DOWNLOAD_PATH = INFORMES_DIR
ERROR_PATH    = os.path.join(USER_DATA_DIR, "errores")
LOGS_PATH     = os.path.join(USER_DATA_DIR, "logs")

for path in [USER_DATA_DIR, DOWNLOAD_PATH, ERROR_PATH, LOGS_PATH]:
    os.makedirs(path, exist_ok=True)

SOLICITANTES_BASE = []
RANKING_REFRESH_CADA = 10

_sol_cache: list = []
_sol_saves_desde_refresh: int = 0

# ── Credenciales RPI del usuario (config.json, no .env) ──────────────────────

def load_rpi_credentials():
    """Lee USUARIO y PASSWORD del RPI desde config.json del usuario."""
    if not os.path.exists(CONFIG_FILE):
        return None, None
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
        return data.get("usuario"), data.get("password")
    except Exception:
        return None, None

def save_rpi_credentials(usuario: str, password: str, proxy_url: str = "", proxy_usuario: str = "", proxy_password: str = ""):
    """Guarda credenciales del RPI (y proxy opcional) en config.json."""
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    data = {"usuario": usuario, "password": password}
    if proxy_url:
        data["proxy"] = {"url": proxy_url, "usuario": proxy_usuario, "password": proxy_password}
    else:
        # Preservar proxy existente si no se envió nada nuevo
        try:
            with open(CONFIG_FILE, "r") as f:
                existing = json.load(f)
            if "proxy" in existing:
                data["proxy"] = existing["proxy"]
        except Exception:
            pass
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f)


def load_proxy_config() -> dict:
    """Lee configuración de proxy desde config.json."""
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f).get("proxy", {})
    except Exception:
        return {}

def delete_rpi_credentials():
    """Borra config.json (permite reconfigurar o desinstalar)."""
    if os.path.exists(CONFIG_FILE):
        os.remove(CONFIG_FILE)

USUARIO, PASSWORD = load_rpi_credentials()

# Estado global del proceso Playwright
estado_proceso = {
    "corriendo": False,
    "log": [],
    "progreso": 0,
    "total": 0,
    "fase": "",
    "esperando_confirmacion": False,
    "orden_confirmacion": "",
    "dialogo_portal": "",
}

_confirmacion_event = threading.Event()

# =====================================================
# BASE DE DATOS SQLite
# =====================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tramites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ORDEN TEXT,
                TIPO_SOLICITUD TEXT,
                APELLIDO TEXT,
                NOMBRE TEXT,
                DNI TEXT,
                CUIT TEXT,
                PARTIDO TEXT,
                NRO_INSCRIPCION TEXT,
                UF_UC TEXT,
                C TEXT, S TEXT, CH TEXT, CH2 TEXT,
                QTA TEXT, QTA2 TEXT,
                F TEXT, F2 TEXT,
                M TEXT, M2 TEXT,
                P TEXT, P2 TEXT, SP TEXT,
                SOLICITANTE TEXT,
                ESTADO TEXT DEFAULT 'PENDIENTE',
                NRO_TRAMITE TEXT,
                FECHA_CARGA TEXT,
                NOTAS TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col_sql in [
            "ALTER TABLE tramites ADD COLUMN NOTAS TEXT DEFAULT ''",
            "ALTER TABLE tramites ADD COLUMN FECHA_COMPLETADO TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(col_sql)
            except:
                pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS solicitantes (
                nombre TEXT PRIMARY KEY,
                usos   INTEGER DEFAULT 0,
                ultimo_uso TEXT DEFAULT ''
            )
        """)
        for nombre in SOLICITANTES_BASE:
            conn.execute(
                "INSERT OR IGNORE INTO solicitantes (nombre, usos) VALUES (?, 0)",
                (nombre.upper().strip(),)
            )
        total_existente = conn.execute("SELECT COUNT(*) FROM solicitantes WHERE usos > 0").fetchone()[0]
        if total_existente == 0:
            conn.execute("""
                INSERT INTO solicitantes (nombre, usos)
                SELECT UPPER(TRIM(SOLICITANTE)), COUNT(DISTINCT ORDEN)
                FROM tramites
                WHERE SOLICITANTE IS NOT NULL AND SOLICITANTE != ''
                GROUP BY UPPER(TRIM(SOLICITANTE))
                ON CONFLICT(nombre) DO UPDATE SET usos = excluded.usos
            """)
        conn.commit()

def db_to_dict(row):
    return dict(row)

def _calcular_ranking() -> list:
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT nombre FROM solicitantes
                ORDER BY usos DESC, nombre ASC LIMIT 20
            """).fetchall()
            return [r[0] for r in rows if r[0]]
    except:
        return list(SOLICITANTES_BASE)


def obtener_solicitantes() -> list:
    global _sol_cache
    if not _sol_cache:
        _sol_cache = _calcular_ranking()
    return _sol_cache


def registrar_uso_solicitante(nombre: str):
    global _sol_cache, _sol_saves_desde_refresh
    nombre = nombre.upper().strip()
    if not nombre:
        return
    try:
        hoy = datetime.now().strftime("%d/%m/%Y")
        with get_db() as conn:
            conn.execute("""
                INSERT INTO solicitantes (nombre, usos, ultimo_uso) VALUES (?, 1, ?)
                ON CONFLICT(nombre) DO UPDATE SET usos = usos + 1, ultimo_uso = ?
            """, (nombre, hoy, hoy))
            conn.commit()
    except:
        pass
    _sol_saves_desde_refresh += 1
    if _sol_saves_desde_refresh >= RANKING_REFRESH_CADA or not _sol_cache:
        _sol_cache = _calcular_ranking()
        _sol_saves_desde_refresh = 0


def campo_solicitante():
    return '<input type="text" name="solicitante" placeholder="Nombre del solicitante" style="width:200px" oninput="mayus(this)">'


def normalizar_texto(texto: str) -> str:
    import unicodedata
    texto = texto.upper().strip()
    texto = texto.replace("Ñ", "__ENIE__")
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = texto.replace("__ENIE__", "Ñ")
    return texto


def log_proceso(msg):
    estado_proceso["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
    print(msg)


def guardar_log_sesion():
    nombre = datetime.now().strftime("carga_%Y-%m-%d_%H-%M.txt")
    ruta = os.path.join(LOGS_PATH, nombre)
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            f.write("\n".join(estado_proceso["log"]))
        log_proceso(f"📄 Log guardado: logs/{nombre}")
    except Exception as e:
        print(f"No se pudo guardar el log: {e}")

# =====================================================
# FLASK APP
# =====================================================
app = Flask(__name__)
app.secret_key = os.urandom(24)  # para flask_session

# ── Registrar rutas de autenticación ──────────────────────────────────────────
from auth_routes import auth_bp
app.register_blueprint(auth_bp)

# ── Middleware: verificar auth → suscripción → config RPI ─────────────────────
RUTAS_PUBLICAS = {"/login", "/logout", "/registro", "/suscripcion", "/setup", "/borrar-config"}

@app.before_request
def verificar_acceso():
    """
    Orden de verificaciones:
      1. Auth Firebase (token válido)
      2. Suscripción activa en Firestore
      3. Credenciales RPI configuradas (config.json)
    """
    if request.path in RUTAS_PUBLICAS or request.path.startswith("/static"):
        return

    id_token, sess = get_valid_token()
    if not id_token or not sess:
        return redirect(url_for("auth.login"))

    flask_session["user_email"] = sess.get("email", "")
    flask_session["user_id"]    = sess.get("localId", "")

    endpoints_libres = {"/estado_proceso", "/favicon.ico", "/iniciar_pago", "/suscripcion_ok"}
    if request.path not in endpoints_libres:
        if not check_subscription(id_token, sess.get("localId", "")):
            return redirect(url_for("auth.suscripcion"))

    # Si suscripción OK pero no hay credenciales RPI → setup
    if request.path not in endpoints_libres and not os.path.exists(CONFIG_FILE):
        return redirect("/setup")

# =====================================================
# CSS Y JS COMPARTIDO
# =====================================================

CSS_JS = r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GestorRPI</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg:       #07080f;
  --s1:       #0d0f1a;
  --s2:       #111525;
  --s3:       #161c2e;
  --b1:       #1e2740;
  --b2:       #252f4a;
  --text:     #e8edf8;
  --text2:    #8a9bbf;
  --muted:    #3a4a6b;
  --amber:    #f59e0b;
  --amber-d:  #d97706;
  --amber-gl: rgba(245,158,11,0.15);
  --green:    #22c55e;
  --red:      #ef4444;
  --blue:     #3b82f6;
  --purple:   #a855f7;
  --warn:     #f97316;

  /* aliases para compatibilidad con templates existentes */
  --surface:  #0d0f1a;
  --surface2: #111525;
  --surface3: #161c2e;
  --border:   #1e2740;
  --border2:  #252f4a;
  --accent:   #f59e0b;
  --accent2:  #3b82f6;
  --accent3:  #22c55e;
  --danger:   #ef4444;
  --mono:     'JetBrains Mono', monospace;
  --sans:     'Space Grotesk', system-ui, sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: var(--sans);
  background: var(--bg);
  color: var(--text);
  font-size: 14px;
  min-height: 100vh;
}

/* ── TOPBAR ─────────────────────────────────────────── */
.topbar {
  height: 52px;
  display: flex;
  align-items: center;
  background: var(--s1);
  border-bottom: 1px solid var(--b1);
  padding: 0 1.5rem;
  position: sticky;
  top: 0;
  z-index: 100;
}
.brand {
  font-family: var(--mono);
  font-size: .8rem;
  font-weight: 600;
  color: var(--amber);
  letter-spacing: .14em;
  padding-right: 1.4rem;
  border-right: 1px solid var(--b1);
  margin-right: .3rem;
  white-space: nowrap;
}
.topbar a {
  display: flex;
  align-items: center;
  height: 52px;
  padding: 0 .95rem;
  font-size: .72rem;
  font-family: var(--mono);
  color: var(--muted);
  border-bottom: 2px solid transparent;
  text-decoration: none;
  letter-spacing: .05em;
  font-weight: 500;
  transition: color .15s, background .15s;
}
.topbar a:hover { color: var(--text); background: var(--s2); }
.topbar a.active { color: var(--amber); border-bottom-color: var(--amber); }

/* ── PAGE ───────────────────────────────────────────── */
.page { max-width: 980px; margin: 0 auto; padding: 1.4rem 1.5rem; }

/* ── CARD ───────────────────────────────────────────── */
.card {
  background: var(--s1);
  border: 1px solid var(--b1);
  border-radius: 10px;
  overflow: hidden;
  margin-bottom: 1.2rem;
}
.card-header {
  padding: .7rem 1.2rem;
  background: var(--s2);
  border-bottom: 1px solid var(--b1);
  display: flex;
  align-items: center;
  gap: .5rem;
  font-family: var(--mono);
  font-size: .68rem;
  font-weight: 500;
  color: var(--text2);
  letter-spacing: .09em;
  text-transform: uppercase;
}
.card-header::before { content: "//"; color: var(--amber); margin-right: .1rem; }
.card-body { padding: 1.4rem 1.5rem; }

/* ── FORM ───────────────────────────────────────────── */
.row { display: flex; align-items: center; margin-bottom: .85rem; }
.row.top { align-items: flex-start; }
.lbl {
  width: 220px;
  min-width: 220px;
  text-align: right;
  margin-right: 1rem;
  color: var(--text2);
  padding-top: .48rem;
  font-size: .8rem;
  line-height: 1.4;
}
.req { color: var(--amber); margin-left: 2px; }

input[type=text], input[type=number], input[type=email],
input[type=password], select {
  padding: .55rem .88rem;
  background: var(--s3);
  border: 1.5px solid var(--b2);
  border-radius: 7px;
  font-size: .88rem;
  color: var(--text);
  font-family: var(--sans);
  outline: none;
  transition: border-color .15s, box-shadow .15s;
}
input[type=text]::placeholder { color: var(--muted); }
input:focus, select:focus {
  border-color: var(--amber);
  box-shadow: 0 0 0 3px rgba(245,158,11,.1);
}
input.valid   { border-color: var(--green) !important; }
input.invalid { border-color: var(--red) !important; }
select option { background: var(--s2); color: var(--text); }

.vmsg { font-size: .68rem; margin-left: .6rem; font-family: var(--mono); }
.vmsg.ok  { color: var(--green); }
.vmsg.err { color: var(--red); }

/* Nomenclatura catastral */
.nom { display: flex; flex-wrap: wrap; gap: .4rem .3rem; align-items: center; }
.nom input { width: 38px; text-align: center; padding: .5rem .3rem; font-family: var(--mono); font-size: .82rem; }
.nom .nl { font-family: var(--mono); font-size: .63rem; color: var(--amber); font-weight: 500; }
.nom .ns { color: var(--muted); font-size: .75rem; }

/* Titulares */
.titular-row {
  display: flex;
  gap: .5rem;
  margin-bottom: .5rem;
  align-items: center;
  margin-left: 236px;
}
.titular-row input { flex: 1; }
.btn-rm {
  background: rgba(239,68,68,.08);
  border: 1px solid rgba(239,68,68,.25);
  color: #f87171;
  cursor: pointer;
  padding: .4rem .75rem;
  font-size: .72rem;
  border-radius: 6px;
  transition: all .15s;
}
.btn-rm:hover { background: var(--red); color: #fff; }
.btn-add-t {
  background: none;
  border: 1.5px dashed var(--b2);
  color: var(--text2);
  cursor: pointer;
  padding: .5rem 1rem;
  font-size: .72rem;
  font-family: var(--mono);
  border-radius: 7px;
  margin-left: 236px;
  margin-top: .3rem;
  transition: all .15s;
  letter-spacing: .04em;
}
.btn-add-t:hover { border-color: var(--blue); color: var(--blue); }

/* Buttons */
.btn {
  padding: .55rem 1.1rem;
  border-radius: 7px;
  font-size: .76rem;
  font-family: var(--mono);
  cursor: pointer;
  border: 1.5px solid;
  transition: all .15s;
  letter-spacing: .03em;
  font-weight: 500;
  text-decoration: none;
  display: inline-block;
}
.btn-primary   { background: var(--blue);  border-color: var(--blue);  color: #fff; }
.btn-primary:hover { background: #2563eb; }
.btn-success   { background: var(--green); border-color: var(--green); color: #000; }
.btn-danger    { background: none; border-color: rgba(239,68,68,.3); color: #f87171; }
.btn-danger:hover { background: var(--red); color: #fff; }
.btn-secondary { background: var(--s2); border-color: var(--b2); color: var(--text2); }
.btn-secondary:hover { border-color: var(--text2); color: var(--text); }

.btn-save {
  width: 100%;
  margin-top: 1.2rem;
  padding: .9rem;
  background: var(--amber);
  border: none;
  color: var(--bg);
  border-radius: 9px;
  font-size: .78rem;
  font-weight: 700;
  font-family: var(--mono);
  letter-spacing: .09em;
  text-transform: uppercase;
  cursor: pointer;
  transition: all .2s;
  box-shadow: 0 4px 16px rgba(245,158,11,.2);
}
.btn-save:hover {
  background: var(--amber-d);
  transform: translateY(-1px);
  box-shadow: 0 6px 22px rgba(245,158,11,.3);
}

/* Alerts */
.alert {
  padding: .75rem 1rem;
  margin-bottom: 1rem;
  font-size: .78rem;
  border-radius: 7px;
  font-family: var(--mono);
  display: flex;
  align-items: center;
  gap: .5rem;
}
.alert-ok   { background: rgba(34,197,94,.07);   border: 1px solid rgba(34,197,94,.2);   color: #4ade80; }
.alert-err  { background: rgba(239,68,68,.07);   border: 1px solid rgba(239,68,68,.2);   color: #f87171; }
.alert-warn { background: rgba(245,158,11,.07);  border: 1px solid rgba(245,158,11,.2);  color: var(--amber); }

/* Table */
table { width: 100%; border-collapse: collapse; font-size: .8rem; }
th {
  background: var(--s2);
  color: var(--muted);
  padding: .6rem .9rem;
  text-align: left;
  font-size: .63rem;
  font-family: var(--mono);
  letter-spacing: .09em;
  text-transform: uppercase;
  border-bottom: 1px solid var(--b1);
  white-space: nowrap;
}
td {
  padding: .65rem .9rem;
  border-bottom: 1px solid rgba(30,39,64,.5);
  color: var(--text);
}
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--s2); }

/* Badges — todos los existentes + nuevos */
.badge {
  display: inline-block;
  padding: .2rem .55rem;
  font-size: .63rem;
  font-family: var(--mono);
  font-weight: 600;
  border-radius: 4px;
  border: 1px solid;
  white-space: nowrap;
  letter-spacing: .04em;
}
.b-755    { background: rgba(59,130,246,.1);  color: #60a5fa;  border-color: rgba(59,130,246,.25); }
.b-752    { background: rgba(245,158,11,.1);  color: #f59e0b;  border-color: rgba(245,158,11,.25); }
.b-754    { background: rgba(168,85,247,.1);  color: #c084fc;  border-color: rgba(168,85,247,.25); }
.b-753ph  { background: rgba(34,197,94,.1);   color: #4ade80;  border-color: rgba(34,197,94,.25);  }
.b-pend   { background: rgba(245,158,11,.1);  color: #f59e0b;  border-color: rgba(245,158,11,.2);  }
.b-carg   { background: rgba(59,130,246,.1);  color: #60a5fa;  border-color: rgba(59,130,246,.2);  }
.b-comp   { background: rgba(34,197,94,.1);   color: #4ade80;  border-color: rgba(34,197,94,.2);   }
.b-sinnro { background: rgba(168,85,247,.1);  color: #c084fc;  border-color: rgba(168,85,247,.25); }
.b-error  { background: rgba(239,68,68,.1);   color: #f87171;  border-color: rgba(239,68,68,.25);  }

/* Menu cards (dashboard) */
.menu-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: .5rem; }
.menu-card {
  background: var(--s2);
  border: 1px solid var(--b1);
  border-radius: 10px;
  padding: 1.5rem 1.4rem;
  cursor: pointer;
  text-align: left;
  width: 100%;
  transition: all .2s;
  position: relative;
  color: var(--text);
}
.menu-card:hover {
  border-color: var(--amber);
  background: var(--s3);
  transform: translateY(-2px);
  box-shadow: 0 6px 24px var(--amber-gl);
}
.menu-card:disabled { opacity: .3; cursor: not-allowed; pointer-events: none; }
.menu-card .num {
  font-family: var(--mono);
  font-size: .63rem;
  color: var(--amber);
  letter-spacing: .12em;
  text-transform: uppercase;
  margin-bottom: .9rem;
  font-weight: 500;
}
.menu-card .title { font-size: 1rem; font-weight: 600; margin-bottom: .4rem; letter-spacing: -.02em; }
.menu-card .desc  { font-size: .78rem; color: var(--text2); line-height: 1.55; }
.menu-card .icon  {
  position: absolute;
  top: 1.1rem; right: 1.2rem;
  width: 28px; height: 28px;
  border-radius: 50%;
  background: rgba(245,158,11,.1);
  border: 1px solid rgba(245,158,11,.2);
  display: flex; align-items: center; justify-content: center;
  font-size: .75rem; color: var(--amber);
  transition: all .2s;
}
.menu-card:hover .icon { background: var(--amber); color: var(--bg); }

/* Menu card variante danger (para acciones destructivas) */
.menu-card-danger { border-color: rgba(239,68,68,.25) !important; }
.menu-card-danger:hover {
  border-color: var(--red) !important;
  background: rgba(239,68,68,.06) !important;
  box-shadow: 0 6px 24px rgba(239,68,68,.12) !important;
}
.menu-card-danger .num { color: #f87171 !important; }
.menu-card-danger .icon { background: rgba(239,68,68,.1) !important; border-color: rgba(239,68,68,.2) !important; color: #f87171 !important; }
.menu-card-danger:hover .icon { background: var(--red) !important; color: #fff !important; }

/* Stats */
.stats { display: flex; gap: .9rem; flex-wrap: wrap; margin-bottom: 1.2rem; }
.stat-card {
  background: var(--s2);
  border: 1px solid var(--b1);
  border-radius: 8px;
  padding: 1rem 1.3rem;
  flex: 1;
  min-width: 110px;
  text-align: center;
}
.stat-card .num {
  font-size: 1.9rem;
  font-family: var(--mono);
  color: var(--amber);
  line-height: 1;
  font-weight: 600;
  letter-spacing: -.04em;
}
.stat-card .lab {
  font-size: .62rem;
  color: var(--muted);
  margin-top: .4rem;
  font-family: var(--mono);
  letter-spacing: .06em;
  text-transform: uppercase;
}

/* Console / Log */
.console {
  background: #050609;
  color: #8a9bbf;
  font-family: var(--mono);
  font-size: .75rem;
  padding: 1rem 1.2rem;
  height: 340px;
  overflow-y: auto;
  border: 1px solid var(--b1);
  border-radius: 8px;
  line-height: 1.8;
}
.console::-webkit-scrollbar { width: 3px; }
.console::-webkit-scrollbar-thumb { background: var(--b2); }
.line-ok   { color: #4ade80; }
.line-err  { color: #f87171; }
.line-warn { color: var(--amber); }
.line-info { color: #60a5fa; }

/* Progress */
.progress-bar  { height: 3px; background: var(--b1); margin: .6rem 0; border-radius: 50px; overflow: hidden; }
.progress-fill { height: 100%; border-radius: 50px; background: linear-gradient(90deg, var(--amber-d), var(--amber)); transition: width .4s ease; }
.prog-label    { font-family: var(--mono); font-size: .7rem; color: var(--muted); margin-bottom: .4rem; }

/* Separators */
hr.sep { border: none; border-top: 1px solid var(--b1); margin: .9rem 0; }
.sec-label {
  font-family: var(--mono);
  font-size: .63rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .1em;
  margin-bottom: .7rem;
  margin-left: 236px;
}

::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-thumb { background: var(--b2); border-radius: 4px; }

/* ── TOPBAR DROPDOWN ────────────────────────────────── */
.tb-dropdown { position: relative; height: 52px; display: flex; align-items: center; }
.tb-drop-btn {
  background: none;
  border: none;
  color: var(--muted);
  padding: 0 .95rem;
  height: 52px;
  font-size: .72rem;
  font-family: var(--mono);
  letter-spacing: .05em;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: all .15s;
  display: flex;
  align-items: center;
  gap: 4px;
  font-weight: 500;
}
.tb-drop-btn:hover,
.tb-dropdown:hover .tb-drop-btn { color: var(--text); background: var(--s2); }
.tb-drop-btn.active { color: var(--amber); border-bottom-color: var(--amber); }
.tb-drop-menu {
  display: none;
  position: absolute;
  top: 52px; left: 0;
  background: var(--s2);
  border: 1px solid var(--b2);
  border-top: 2px solid var(--amber);
  min-width: 280px;
  z-index: 200;
  box-shadow: 0 12px 32px rgba(0,0,0,.5);
}
.tb-dropdown:hover .tb-drop-menu { display: block; }
.tb-drop-menu a {
  display: block;
  padding: .85rem 1.2rem;
  color: var(--text2);
  text-decoration: none;
  font-size: .72rem;
  font-family: var(--mono);
  border-bottom: 1px solid var(--b1);
  transition: all .12s;
  height: auto;
  letter-spacing: .03em;
}
.tb-drop-menu a:last-child { border-bottom: none; }
.tb-drop-menu a:hover { background: var(--s3); color: var(--amber); padding-left: 1.5rem; }
.tb-drop-menu a.active { color: var(--amber); background: rgba(245,158,11,.06); }
</style>

<script>
function validarCUIT(input, msgId) {
    let val = input.value.replace(/\D/g, '');
    if (val.length > 2) val = val.slice(0,2) + '-' + val.slice(2);
    if (val.length > 11) val = val.slice(0,11) + '-' + val.slice(11);
    input.value = val.slice(0, 13);
    const digits = val.replace(/\D/g, '');
    const msg = document.getElementById(msgId);
    if (!digits) { resetV(input); if(msg) msg.textContent=''; return true; }
    if (digits.length !== 11) {
        setInv(input); if(msg){ msg.textContent='\u2717 11 d\u00edgitos requeridos'; msg.className='vmsg err'; } return false;
    }
    const mult = [5,4,3,2,7,6,5,4,3,2];
    let suma = 0;
    for (let i=0; i<10; i++) suma += parseInt(digits[i]) * mult[i];
    const resto = 11 - (suma % 11);
    const dv = resto===11 ? 0 : resto===10 ? 9 : resto;
    if (dv === parseInt(digits[10])) {
        setVal(input); if(msg){ msg.textContent='\u2713 V\u00e1lido'; msg.className='vmsg ok'; } return true;
    } else {
        setInv(input); if(msg){ msg.textContent='\u2717 D\u00edgito verificador incorrecto'; msg.className='vmsg err'; } return false;
    }
}
function validarDNI(input, msgId) {
    input.value = input.value.replace(/\D/g, '');
    const val = input.value;
    const msg = document.getElementById(msgId);
    if (!val) { resetV(input); if(msg) msg.textContent=''; return true; }
    if (val.length >= 7 && val.length <= 8) {
        setVal(input); if(msg){ msg.textContent='\u2713 V\u00e1lido'; msg.className='vmsg ok'; } return true;
    } else {
        setInv(input); if(msg){ msg.textContent='\u2717 7 u 8 d\u00edgitos'; msg.className='vmsg err'; } return false;
    }
}
function soloNum(input) { input.value = input.value.replace(/\D/g, ''); }
function mayus(input) { input.value = input.value.toUpperCase(); }
function setVal(el) { el.classList.remove('invalid'); el.classList.add('valid'); }
function setInv(el) { el.classList.remove('valid'); el.classList.add('invalid'); }
function resetV(el) { el.classList.remove('valid','invalid'); }

let titCount = 1;
function agregarTitular() {
    titCount++;
    const box = document.getElementById('titulares-box');
    const d = document.createElement('div');
    d.className = 'titular-row'; d.id = 'tit-' + titCount;
    d.innerHTML = `<input type="text" name="apellido[]" placeholder="Apellido" oninput="mayus(this)" style="width:180px">
                   <input type="text" name="nombre[]" placeholder="Nombre" oninput="mayus(this)" style="width:200px">
                   <button type="button" class="btn-rm" onclick="quitarTitular(${titCount})">\u2715</button>`;
    box.appendChild(d);
}
function quitarTitular(id) {
    const el = document.getElementById('tit-' + id);
    if (el) el.remove();
}
function chkForm755() {
    const c = document.getElementById('f-cuit'), d = document.getElementById('f-dni');
    if (c && c.value && c.classList.contains('invalid')) { alert('CUIT inv\u00e1lido'); return false; }
    if (d && d.value && d.classList.contains('invalid')) { alert('DNI inv\u00e1lido'); return false; }
    if (c && !c.value && d && !d.value) { alert('Ingres\u00e1 DNI o CUIT'); return false; }
    return true;
}
function chkForm754() {
    const c = document.getElementById('f-cuit754');
    if (c && c.classList.contains('invalid')) { alert('CUIT inv\u00e1lido'); return false; }
    return true;
}
/* ── Solicitante con "Otro" ─────────────────────────── */
function toggleOtroSolicitante(sel) {
    const box = document.getElementById('otro-sol-box');
    const inp = document.getElementById('otro-sol-input');
    if (sel.value === '__OTRO__') { box.style.display = 'inline-block'; inp.focus(); }
    else { box.style.display = 'none'; inp.value = ''; }
}
function prepararSolicitante(form) {
    const sel = form.querySelector('select[name="solicitante"]');
    if (!sel || sel.value !== '__OTRO__') return true;
    const inp = document.getElementById('otro-sol-input');
    const val = inp.value.trim().toUpperCase();
    if (!val) { alert('Ingres\u00e1 el nombre del solicitante'); inp.focus(); return false; }
    sel.value = val;
    if (!sel.querySelector('option[value="' + val + '"]')) {
        const opt = document.createElement('option'); opt.value = val; opt.text = val; sel.appendChild(opt);
    }
    return true;
}
</script>
"""


def topbar(activo=""):
    cargar_activo = activo in ("/form755", "/form752", "/form754", "/form753ph")
    cargar_cls = "active" if cargar_activo else ""

    html = '''<div class="topbar">
  <span class="brand">GESTOR·RPI</span>
  <a href="/" class="''' + ("active" if activo == "/" else "") + '''">INICIO</a>
  <div class="tb-dropdown">
    <button class="tb-drop-btn ''' + cargar_cls + '''">CARGAR ▾</button>
    <div class="tb-drop-menu">
      <a href="/form755" class="''' + ("active" if activo == "/form755" else "") + '''">755 — Índice de Titulares</a>
      <a href="/form752" class="''' + ("active" if activo == "/form752" else "") + '''">752 — Informe de Dominio FR</a>
      <a href="/form754" class="''' + ("active" if activo == "/form754" else "") + '''">754 — Copia de Dominio FR</a>
      <a href="/form753ph" class="''' + ("active" if activo == "/form753ph" else "") + '''">753 PH — Inhibición Persona Humana</a>
    </div>
  </div>
  <a href="/pendientes"   class="''' + ("active" if activo == "/pendientes"   else "") + '''">PEDIDOS</a>
  <a href="/estadisticas" class="''' + ("active" if activo == "/estadisticas" else "") + '''">ESTADÍSTICAS</a>
  <a href="/productos"   class="''' + ("active" if activo == "/productos"   else "") + '''">PRODUCTOS</a>
  <div style="margin-left:auto;display:flex;align-items:center;gap:0;">
    <a href="/setup"
       style="font-size:.68rem;font-family:var(--mono);letter-spacing:.05em;color:var(--muted);
              padding:0 1rem;height:52px;display:flex;align-items:center;
              border-bottom:2px solid transparent;transition:color .15s,background .15s;"
       class="''' + ("active" if activo == "/setup" else "") + '''"
       onmouseover="this.style.color='var(--text2)'"
       onmouseout="this.style.color=''">
      CONFIG
    </a>
    <form method="POST" action="/borrar-config" style="margin:0;"
          onsubmit="return confirm('¿Borrar credenciales RPI? Tendrás que volver a configurarlas.')">
      <button type="submit"
              style="background:none;border:none;color:var(--muted);
                     font-size:.68rem;font-family:var(--mono);letter-spacing:.05em;
                     cursor:pointer;padding:0 1rem;height:52px;
                     border-bottom:2px solid transparent;transition:color .15s;"
              onmouseover="this.style.color='#f87171'"
              onmouseout="this.style.color=''">
        BORRAR CONFIG
      </button>
    </form>
    <a href="/logout"
       onclick="return confirm('¿Cerrar sesión?')"
       style="font-size:.68rem;font-family:var(--mono);letter-spacing:.05em;color:var(--muted);
              padding:0 1rem;height:52px;display:flex;align-items:center;
              border-bottom:2px solid transparent;transition:color .15s;text-decoration:none;"
       onmouseover="this.style.color='#f87171'"
       onmouseout="this.style.color=''">
      SALIR
    </a>
  </div>
</div>'''
    return html



def nom_cat_fields(prefix=""):
    return f"""
    <div class="nom">
        <span class="nl">C</span><input type="text" name="c" style="width:36px">
        <span class="ns">&nbsp;</span>
        <span class="nl">S</span><input type="text" name="s" style="width:36px">
        <span class="ns">&nbsp;</span>
        <span class="nl">Ch</span><input type="text" name="ch" style="width:36px"><span class="ns">-</span><input type="text" name="ch2" style="width:36px">
        <span class="ns">&nbsp;</span>
        <span class="nl">Qta</span><input type="text" name="qta" style="width:36px"><span class="ns">-</span><input type="text" name="qta2" style="width:36px">
        <span class="ns">&nbsp;</span>
        <span class="nl">F</span><input type="text" name="f" style="width:36px"><span class="ns">-</span><input type="text" name="f2" style="width:36px">
        <span class="ns">&nbsp;</span>
        <span class="nl">M</span><input type="text" name="m" style="width:36px"><span class="ns">-</span><input type="text" name="m2" style="width:36px">
        <span class="ns">&nbsp;</span>
        <span class="nl">P</span><input type="text" name="p" style="width:36px"><span class="ns">-</span><input type="text" name="p2" style="width:36px">
        <span class="ns">&nbsp;</span>
        <span class="nl">SP</span><input type="text" name="sp" style="width:36px">
    </div>"""

# =====================================================
# LÓGICA DE GUARDADO EN SQLite
# =====================================================
def guardar_tramite(f, tipo):
    apellidos = f.getlist('apellido[]')
    nombres = f.getlist('nombre[]')
    dni_raw = "".join(filter(str.isdigit, f.get("dni", "")))
    cuit_raw = "".join(filter(str.isdigit, f.get("cuit", "")))
    orden = f.get("orden", "").strip()
    sol_nombre = f.get("solicitante", "").upper().strip()

    registrar_uso_solicitante(sol_nombre)

    with get_db() as conn:
        for i, ape in enumerate(apellidos):
            if not ape.strip():
                continue
            nom = nombres[i] if i < len(nombres) else ""
            if i == 0:
                conn.execute("""
                    INSERT INTO tramites
                    (ORDEN, TIPO_SOLICITUD, APELLIDO, NOMBRE, DNI, CUIT,
                     PARTIDO, NRO_INSCRIPCION, UF_UC,
                     C, S, CH, CH2, QTA, QTA2, F, F2, M, M2, P, P2, SP,
                     SOLICITANTE, ESTADO)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    orden, tipo, normalizar_texto(ape), normalizar_texto(nom),
                    dni_raw, cuit_raw,
                    f.get("partido","").strip(), f.get("matricula","").strip(), f.get("uf","").strip(),
                    f.get("c","").strip(), f.get("s","").strip(),
                    f.get("ch","").strip(), f.get("ch2","").strip(),
                    f.get("qta","").strip(), f.get("qta2","").strip(),
                    f.get("f","").strip(), f.get("f2","").strip(),
                    f.get("m","").strip(), f.get("m2","").strip(),
                    f.get("p","").strip(), f.get("p2","").strip(),
                    f.get("sp","").strip(),
                    sol_nombre, "PENDIENTE"
                ))
            else:
                conn.execute("""
                    INSERT INTO tramites (ORDEN, TIPO_SOLICITUD, APELLIDO, NOMBRE, ESTADO)
                    VALUES (?,?,?,?,?)
                """, (orden, tipo, normalizar_texto(ape), normalizar_texto(nom), "PENDIENTE"))
        conn.commit()

# =====================================================
# RUTAS FLASK
# =====================================================

@app.route("/")
def index():
    # Contar pendientes para mostrar en el menú
    try:
        with get_db() as conn:
            pendientes = conn.execute("SELECT COUNT(DISTINCT ORDEN) FROM tramites WHERE ESTADO='PENDIENTE'").fetchone()[0]
    except:
        pendientes = 0

    html = CSS_JS + topbar("/") + f"""
    <div class="page">
        <div class="card">
            <div class="card-header"><div class="hbar"></div>GESTOR RPI — Panel Principal</div>
            <div class="card-body">
                {"" if pendientes == 0 else f'<div class="alert alert-warn">⏳ Hay <strong>{pendientes}</strong> órdenes pendientes de cargar al RPI.</div>'}
                <div class="menu-grid">
                    <button class="menu-card" onclick="iniciarProceso('cargar_base')">
                        <div class="icon">📝</div>
                        <div class="num">1</div>
                        <div class="title">Cargar Base de Datos</div>
                        <div class="desc">Completar el formulario de pedidos antes de solicitar al RPI</div>
                    </button>
                    <button class="menu-card" onclick="iniciarProceso('solicitar')" {"" if pendientes > 0 else 'disabled'}>
                        <div class="icon">📤</div>
                        <div class="num">2</div>
                        <div class="title">Solicitar Informes al RPI</div>
                        <div class="desc">Cargar los pedidos pendientes al portal del RPI</div>
                    </button>
                    <button class="menu-card" onclick="iniciarProceso('solicitar_descargar')" {"" if pendientes > 0 else 'disabled'}>
                        <div class="icon">🔄</div>
                        <div class="num">3</div>
                        <div class="title">Solicitar + Descargar</div>
                        <div class="desc">Cargar pedidos y descargar los informes ya procesados</div>
                    </button>
                    <button class="menu-card" onclick="abrirDescarga()">
                        <div class="icon">📥</div>
                        <div class="num">4</div>
                        <div class="title">Descargar Informes</div>
                        <div class="desc">Descargar los informes ya procesados por el RPI</div>
                    </button>
                </div>
            </div>
        </div>

        <!-- PANEL DE DESCARGA con fechas -->
        <div class="card" id="panel-descarga" style="display:none">
            <div class="card-header"><div class="hbar"></div>Configurar Descarga</div>
            <div class="card-body">
                <div class="row">
                    <div class="lbl">Fecha Desde</div>
                    <input type="text" id="f-desde" placeholder="DD/MM/AAAA" style="width:130px">
                </div>
                <div class="row">
                    <div class="lbl">Fecha Hasta</div>
                    <input type="text" id="f-hasta" placeholder="DD/MM/AAAA (vacío = hoy)" style="width:130px">
                </div>
                <div style="margin-left:224px;margin-top:10px;display:flex;gap:8px;">
                    <button class="btn btn-primary" onclick="confirmarDescarga()">Iniciar Descarga</button>
                    <button class="btn btn-secondary" onclick="document.getElementById('panel-descarga').style.display='none'">Cancelar</button>
                </div>
            </div>
        </div>

        <!-- MODAL: Elegir modo de carga -->
        <div id="modal-modo" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:500;align-items:center;justify-content:center;">
            <div style="background:var(--surface);border:1px solid var(--border);padding:32px;max-width:480px;width:90%;">
                <div style="font-family:var(--mono);font-size:10px;color:var(--accent);letter-spacing:2px;margin-bottom:16px;">// MODO DE EJECUCIÓN</div>
                <div style="font-size:15px;font-weight:500;margin-bottom:8px;">¿Cómo querés ejecutar la carga?</div>
                <input type="hidden" id="modal-accion">
                <div style="display:flex;gap:10px;flex-direction:column;">
                    <button class="btn btn-primary" style="padding:14px 12px;text-align:left;" onclick="confirmarModo(false, false)">
                        ⚡ <strong>Automático</strong>
                        <span style="display:block;font-size:11px;font-weight:400;opacity:0.8;margin-top:3px;">Navegador en segundo plano — carga y envía todo solo</span>
                    </button>
                    <button class="btn btn-secondary" style="padding:14px 12px;text-align:left;" onclick="confirmarModo(true, false)">
                        👁 <strong>Visible</strong>
                        <span style="display:block;font-size:11px;font-weight:400;opacity:0.8;margin-top:3px;">Ver el navegador — el script hace todo automáticamente</span>
                    </button>
                    <button class="btn btn-secondary" style="padding:14px 12px;text-align:left;border-color:var(--warn);color:var(--warn);" onclick="confirmarModo(true, true)">
                        ✋ <strong>Con revisión</strong>
                        <span style="display:block;font-size:11px;font-weight:400;opacity:0.8;margin-top:3px;color:var(--text2);">El script llena los datos y vos hacés click en Enviar en cada formulario</span>
                    </button>
                </div>
                <button onclick="document.getElementById('modal-modo').style.display='none'"
                    style="margin-top:14px;width:100%;background:none;border:none;color:var(--muted);cursor:pointer;font-size:12px;font-family:var(--mono);">
                    cancelar
                </button>
            </div>
        </div>

        <!-- LOG DE PROCESO -->
        <div class="card" id="panel-log" style="display:none">
            <div class="card-header" id="log-header">Proceso en curso...</div>
            <div class="card-body">
                <div class="progress-bar"><div class="progress-fill" id="prog-fill" style="width:0%"></div></div>
                <div style="font-size:11px;color:#888;margin-bottom:8px;" id="prog-label">Iniciando...</div>
                <div class="console" id="console-log"></div>

                <!-- Panel de confirmación manual -->
                <div id="panel-confirmar" style="display:none;margin-top:16px;padding:18px 20px;
                     background:rgba(249,144,0,0.08);border:2px solid #f90;border-radius:3px;">
                    <div style="font-family:var(--mono);font-size:10px;color:#f90;letter-spacing:2px;margin-bottom:10px;">
                        // REVISIÓN REQUERIDA
                    </div>
                    <div id="dialogo-portal-box" style="display:none;margin-bottom:14px;
                         padding:10px 14px;background:rgba(232,74,74,0.12);border:1px solid rgba(232,74,74,0.5);border-radius:2px;">
                        <span style="font-family:var(--mono);font-size:10px;color:var(--danger);letter-spacing:1px;">⚠ PORTAL DICE:</span>
                        <div id="dialogo-portal-msg" style="font-size:13px;font-weight:600;color:var(--danger);margin-top:4px;"></div>
                        <div style="font-size:11px;color:var(--text2);margin-top:6px;">Corregí el dato en el navegador y volvé a confirmar, o cancelá esta orden.</div>
                    </div>
                    <div id="confirmar-texto" style="font-size:14px;font-weight:500;margin-bottom:14px;">
                        Formulario listo — revisá los datos en el navegador y confirmá
                    </div>
                    <div style="display:flex;gap:10px;">
                        <button class="btn btn-primary" style="flex:1;padding:12px;font-size:14px;background:#f90;border-color:#f90;color:#000;"
                            onclick="confirmarFormulario()">✔ CONFIRMAR Y ENVIAR</button>
                        <button class="btn btn-secondary" style="padding:12px 18px;" onclick="cancelarFormulario()">✗ Cancelar</button>
                    </div>
                </div>

                <div style="margin-top:12px;">
                    <button class="btn btn-secondary" id="btn-cerrar-log" style="display:none" onclick="document.getElementById('panel-log').style.display='none'">Cerrar</button>
                </div>
            </div>
        </div>
    </div>

    <script>
    let accionActual = '';
    let polling = null;

    function iniciarProceso(accion) {{
        if (accion === 'cargar_base') {{ window.location.href = '/form755'; return; }}
        accionActual = accion;
        document.getElementById('panel-descarga').style.display = 'none';
        if (accion === 'solicitar' || accion === 'solicitar_descargar') {{
            mostrarModalModo(accion);
        }} else {{
            lanzarAccion(accion, '', '', false, false);
        }}
    }}

    function mostrarModalModo(accion) {{
        document.getElementById('modal-accion').value = accion;
        document.getElementById('modal-modo').style.display = 'flex';
    }}

    function confirmarModo(visible, conRevision) {{
        const accion = document.getElementById('modal-accion').value;
        document.getElementById('modal-modo').style.display = 'none';
        lanzarAccion(accion, '', '', visible, conRevision);
    }}

    function abrirDescarga() {{
        accionActual = 'descargar';
        document.getElementById('panel-descarga').style.display = 'block';
        document.getElementById('panel-log').style.display = 'none';
        const hoy = new Date();
        const desde = new Date(hoy); desde.setDate(desde.getDate() - 15);
        document.getElementById('f-desde').value = formatFecha(desde);
        document.getElementById('f-hasta').value = '';
    }}

    function formatFecha(d) {{
        return String(d.getDate()).padStart(2,'0') + '/' +
               String(d.getMonth()+1).padStart(2,'0') + '/' + d.getFullYear();
    }}

    function confirmarDescarga() {{
        const desde = document.getElementById('f-desde').value.trim();
        const hasta = document.getElementById('f-hasta').value.trim();
        if (!desde) {{ alert('Ingresá la fecha Desde'); return; }}
        document.getElementById('panel-descarga').style.display = 'none';
        lanzarAccion('descargar', desde, hasta, false, false);
    }}

    function lanzarAccion(accion, desde, hasta, modoVisible=false, confirmacionManual=false) {{
        document.getElementById('panel-log').style.display = 'block';
        document.getElementById('log-header').textContent = 'Proceso en curso...';
        document.getElementById('console-log').innerHTML = '';
        document.getElementById('prog-fill').style.width = '0%';
        document.getElementById('prog-label').textContent = 'Iniciando...';
        document.getElementById('btn-cerrar-log').style.display = 'none';
        document.getElementById('panel-confirmar').style.display = 'none';

        fetch('/iniciar_proceso', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{accion, desde, hasta, modo_visible: modoVisible, confirmacion_manual: confirmacionManual}})
        }}).then(r => r.json()).then(d => {{
            if (d.ok) {{ polling = setInterval(actualizarLog, 1000); }}
            else {{
                const con = document.getElementById('console-log');
                const div = document.createElement('div'); div.className = 'line-err';
                div.textContent = '❌ ' + d.error; con.appendChild(div);
            }}
        }});
    }}

    function actualizarLog() {{
        fetch('/estado_proceso').then(r => r.json()).then(d => {{
            const con = document.getElementById('console-log');
            con.innerHTML = '';
            d.log.forEach(l => {{
                const div = document.createElement('div');
                if (l.includes('✅') || l.includes('OK')) div.className = 'line-ok';
                else if (l.includes('❌')) div.className = 'line-err';
                else if (l.includes('⚠️')) div.className = 'line-warn';
                else if (l.includes('🔑') || l.includes('📥') || l.includes('🚀')) div.className = 'line-info';
                div.textContent = l;
                con.appendChild(div);
            }});
            con.scrollTop = con.scrollHeight;

            if (d.total > 0) {{
                const pct = Math.round((d.progreso / d.total) * 100);
                document.getElementById('prog-fill').style.width = pct + '%';
                document.getElementById('prog-label').textContent = d.fase + ' — ' + d.progreso + ' / ' + d.total;
            }}

            const panelConfirmar = document.getElementById('panel-confirmar');
            if (d.esperando_confirmacion) {{
                panelConfirmar.style.display = 'block';
                document.getElementById('confirmar-texto').textContent =
                    'Orden ' + (d.orden_confirmacion || '') + ' — Revisá los datos en el navegador y confirmá';
                document.getElementById('log-header').textContent = '⏸  Esperando confirmación...';
                const dialogoBox = document.getElementById('dialogo-portal-box');
                const dialogoMsg = document.getElementById('dialogo-portal-msg');
                if (d.dialogo_portal) {{
                    dialogoMsg.textContent = d.dialogo_portal; dialogoBox.style.display = 'block';
                }} else {{ dialogoBox.style.display = 'none'; }}
            }} else {{ panelConfirmar.style.display = 'none'; }}

            if (!d.corriendo) {{
                clearInterval(polling);
                panelConfirmar.style.display = 'none';
                document.getElementById('log-header').textContent = '✅ Proceso finalizado';
                document.getElementById('btn-cerrar-log').style.display = 'inline-block';
                document.getElementById('prog-fill').style.width = '100%';
            }}
        }});
    }}

    function confirmarFormulario() {{
        fetch('/api/confirmar_formulario', {{method:'POST'}})
            .then(r => r.json()).then(d => {{
                if (!d.ok) alert('Error: ' + d.error);
                else document.getElementById('panel-confirmar').style.display = 'none';
            }});
    }}

    function cancelarFormulario() {{
        if (!confirm('¿Cancelar este formulario? El script pasará al siguiente trámite.')) return;
        fetch('/api/cancelar_formulario', {{method:'POST'}})
            .then(r => r.json()).then(d => {{
                if (!d.ok) alert('Error: ' + d.error);
                else document.getElementById('panel-confirmar').style.display = 'none';
            }});
    }}
    </script>
    """
    return html


@app.route("/form755", methods=["GET", "POST"])
def form755():
    msg = ""
    if request.method == "POST":
        guardar_tramite(request.form, "755")
        msg = '<div class="alert alert-ok">✅ Trámite 755 guardado correctamente.</div>'

    html = CSS_JS + topbar("/form755") + f"""
    <div class="page">
        <div class="card">
            <div class="card-header"><div class="hbar"></div>755 — Consulta al Índice de Titulares</div>
            <div class="card-body">
                {msg}
                <form method="POST" onsubmit="return prepararSolicitante(this) && chkForm755()">
                    <div class="row"><div class="lbl">N° de Orden <span class="req">*</span></div>
                        <input type="text" name="orden" style="width:90px" autofocus></div>
                    <div class="row"><div class="lbl">Solicitante <span class="req">*</span></div>
                        {campo_solicitante()}</div>
                    <hr class="sep"><div class="sec-label" style="margin-left:224px">Datos del titular</div>
                    <div class="row"><div class="lbl">Apellido <span class="req">*</span></div>
                        <input type="text" name="apellido[]" style="width:240px" required oninput="mayus(this)"></div>
                    <div class="row"><div class="lbl">Nombre</div>
                        <input type="text" name="nombre[]" style="width:240px" oninput="mayus(this)"></div>
                    <div class="row"><div class="lbl">DNI <span class="req">*</span></div>
                        <input type="text" id="f-dni" name="dni" style="width:120px" maxlength="8"
                               oninput="validarDNI(this,'msg-dni')" placeholder="12345678">
                        <span class="vmsg" id="msg-dni"></span></div>
                    <div class="row"><div class="lbl">CUIT / CUIL <span class="req">*</span></div>
                        <input type="text" id="f-cuit" name="cuit" style="width:155px" maxlength="13"
                               oninput="validarCUIT(this,'msg-cuit')" placeholder="20-12345678-9">
                        <span class="vmsg" id="msg-cuit"></span></div>
                    <div class="row"><div class="lbl">Partido (Opcional)</div>
                        <input type="text" name="partido" style="width:80px" oninput="soloNum(this)"></div>
                    <button type="submit" class="btn-save">GUARDAR EN BASE DE DATOS</button>
                </form>
            </div>
        </div>
    </div>"""
    return html


@app.route("/form752", methods=["GET", "POST"])
def form752():
    msg = ""
    if request.method == "POST":
        guardar_tramite(request.form, "752")
        msg = '<div class="alert alert-ok">✅ Trámite 752 guardado correctamente.</div>'

    html = CSS_JS + topbar("/form752") + f"""
    <div class="page">
        <div class="card">
            <div class="card-header"><div class="hbar"></div>752 — Informe de Dominio Inmueble Matriculado (Folio Real)</div>
            <div class="card-body">
                {msg}
                <form method="POST" onsubmit="return prepararSolicitante(this)">
                    <div class="row"><div class="lbl">N° de Orden <span class="req">*</span></div>
                        <input type="text" name="orden" style="width:90px" autofocus></div>
                    <div class="row"><div class="lbl">Solicitante <span class="req">*</span></div>
                        {campo_solicitante()}</div>
                    <hr class="sep"><div class="sec-label" style="margin-left:224px">Datos del inmueble</div>
                    <div class="row"><div class="lbl">Partido <span class="req">*</span></div>
                        <input type="text" name="partido" style="width:80px" required oninput="soloNum(this)"></div>
                    <div class="row"><div class="lbl">N° Inscripción (Matrícula) <span class="req">*</span></div>
                        <input type="text" name="matricula" style="width:140px" required oninput="soloNum(this)"></div>
                    <div class="row"><div class="lbl">UF / UC</div>
                        <input type="text" name="uf" style="width:80px" oninput="soloNum(this)"></div>
                    <div class="row"><div class="lbl">Nomenclatura Catastral</div>
                        {nom_cat_fields()}</div>
                    <hr class="sep"><div class="sec-label" style="margin-left:224px">Titulares del dominio</div>
                    <div id="titulares-box">
                        <div class="titular-row" style="margin-left:224px">
                            <input type="text" name="apellido[]" placeholder="Apellido" required oninput="mayus(this)" style="width:180px">
                            <input type="text" name="nombre[]" placeholder="Nombre" oninput="mayus(this)" style="width:200px">
                        </div>
                    </div>
                    <button type="button" class="btn-add-t" onclick="agregarTitular()">+ Agregar cotitular</button>
                    <button type="submit" class="btn-save">GUARDAR EN BASE DE DATOS</button>
                </form>
            </div>
        </div>
    </div>"""
    return html


@app.route("/form754", methods=["GET", "POST"])
def form754():
    msg = ""
    if request.method == "POST":
        guardar_tramite(request.form, "754")
        msg = '<div class="alert alert-ok">✅ Trámite 754 guardado correctamente.</div>'

    html = CSS_JS + topbar("/form754") + f"""
    <div class="page">
        <div class="card">
            <div class="card-header"><div class="hbar"></div>754 — Copia de Dominio Inmueble Matriculado (Folio Real)</div>
            <div class="card-body">
                {msg}
                <form method="POST" onsubmit="return prepararSolicitante(this) && chkForm754()">
                    <div class="row"><div class="lbl">N° de Orden <span class="req">*</span></div>
                        <input type="text" name="orden" style="width:90px" autofocus></div>
                    <div class="row"><div class="lbl">Solicitante <span class="req">*</span></div>
                        {campo_solicitante()}</div>
                    <hr class="sep"><div class="sec-label" style="margin-left:224px">Datos del inmueble</div>
                    <div class="row"><div class="lbl">Partido <span class="req">*</span></div>
                        <input type="text" name="partido" style="width:80px" required oninput="soloNum(this)"></div>
                    <div class="row"><div class="lbl">N° Inscripción (Matrícula) <span class="req">*</span></div>
                        <input type="text" name="matricula" style="width:140px" required oninput="soloNum(this)"></div>
                    <div class="row"><div class="lbl">UF / UC</div>
                        <input type="text" name="uf" style="width:80px" oninput="soloNum(this)"></div>
                    <div class="row"><div class="lbl">Nomenclatura Catastral</div>
                        {nom_cat_fields()}</div>
                    <div class="row"><div class="lbl">CUIT Solicitante <span class="req">*</span></div>
                        <input type="text" id="f-cuit754" name="cuit" style="width:155px" maxlength="13"
                               oninput="validarCUIT(this,'msg-cuit754')" placeholder="20-12345678-9" required>
                        <span class="vmsg" id="msg-cuit754"></span></div>
                    <button type="submit" class="btn-save">GUARDAR EN BASE DE DATOS</button>
                </form>
            </div>
        </div>
    </div>"""
    return html


@app.route("/form753ph", methods=["GET", "POST"])
def form753ph():
    msg = ""
    if request.method == "POST":
        guardar_tramite(request.form, "753PH")
        msg = '<div class="alert alert-ok">✅ Trámite 753 PH guardado correctamente.</div>'

    html = CSS_JS + topbar("/form753ph") + f"""
    <div class="page">
        <div class="card">
            <div class="card-header"><div class="hbar"></div>753 PH — Inhibición Persona Humana</div>
            <div class="card-body">
                {msg}
                <form method="POST" onsubmit="return prepararSolicitante(this)">
                    <div class="row"><div class="lbl">N° de Orden <span class="req">*</span></div>
                        <input type="text" name="orden" style="width:90px" autofocus></div>
                    <div class="row"><div class="lbl">Solicitante <span class="req">*</span></div>
                        {campo_solicitante()}</div>
                    <hr class="sep"><div class="sec-label" style="margin-left:224px">Datos del inhibido</div>
                    <div class="row"><div class="lbl">Apellido <span class="req">*</span></div>
                        <input type="text" name="apellido[]" style="width:240px" required oninput="mayus(this)"></div>
                    <div class="row"><div class="lbl">Nombre</div>
                        <input type="text" name="nombre[]" style="width:240px" oninput="mayus(this)"></div>
                    <div class="row"><div class="lbl">DNI</div>
                        <input type="text" id="f-dni753" name="dni" style="width:120px" maxlength="8"
                               oninput="validarDNI(this,'msg-dni753')" placeholder="12345678">
                        <span class="vmsg" id="msg-dni753"></span></div>
                    <button type="submit" class="btn-save">GUARDAR EN BASE DE DATOS</button>
                </form>
            </div>
        </div>
    </div>"""
    return html


@app.route("/pendientes")
def pendientes():
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT ORDEN, TIPO_SOLICITUD, APELLIDO, NOMBRE, DNI, CUIT,
                       PARTIDO, NRO_INSCRIPCION, SOLICITANTE, ESTADO,
                       FECHA_CARGA, NRO_TRAMITE, NOTAS
                FROM tramites ORDER BY CAST(ORDEN AS INTEGER) DESC, id DESC
            """).fetchall()
            total      = conn.execute("SELECT COUNT(*) FROM tramites").fetchone()[0]
            pend       = conn.execute("SELECT COUNT(DISTINCT ORDEN) FROM tramites WHERE ESTADO='PENDIENTE'").fetchone()[0]
            carg       = conn.execute("SELECT COUNT(DISTINCT ORDEN) FROM tramites WHERE ESTADO='CARGADO'").fetchone()[0]
            comp       = conn.execute("SELECT COUNT(DISTINCT ORDEN) FROM tramites WHERE ESTADO='COMPLETADO'").fetchone()[0]
            sinnro     = conn.execute("SELECT COUNT(DISTINCT ORDEN) FROM tramites WHERE ESTADO='SIN_NRO'").fetchone()[0]
            errores_db = conn.execute("SELECT COUNT(DISTINCT ORDEN) FROM tramites WHERE ESTADO='ERROR'").fetchone()[0]
    except:
        rows, total, pend, carg, comp, sinnro, errores_db = [], 0, 0, 0, 0, 0, 0

    def badge_tipo(t):
        t = str(t).lower().replace(" ","").replace("ph","ph")
        return f'<span class="badge b-{t}">{t.upper()}</span>'

    def badge_estado(e):
        cfg = {
            "PENDIENTE":  ("b-pend",   "⏳"),
            "CARGADO":    ("b-carg",   "📤"),
            "COMPLETADO": ("b-comp",   "✅"),
            "SIN_NRO":    ("b-sinnro", "❓"),
            "ERROR":      ("b-error",  "❌"),
        }
        cls, ico = cfg.get(e, ("", ""))
        return f'<span class="badge {cls}">{ico} {e}</span>'

    filas_html = ""
    for r in rows:
        r = dict(r)
        orden  = r.get('ORDEN','')
        tipo   = str(r.get('TIPO_SOLICITUD','')).strip()
        estado = r.get('ESTADO','')
        dni_val     = r.get('DNI','') or ''
        cuit_val    = r.get('CUIT','') or ''
        partido_val = r.get('PARTIDO','') or ''
        mat_val     = r.get('NRO_INSCRIPCION','') or ''
        notas_val   = r.get('NOTAS','') or ''

        if tipo in ('752','754') and not dni_val and not cuit_val:
            doc_col     = f'<span style="color:var(--muted);font-family:var(--mono);font-size:11px">PTD {partido_val} MAT {mat_val}</span>' if partido_val or mat_val else ""
            partido_col = ""
            mat_col     = ""
        else:
            doc_col     = dni_val or cuit_val
            partido_col = partido_val
            mat_col     = mat_val

        sol_actual = r.get('SOLICITANTE','') or ''
        acciones_html = f'<button class="btn-accion btn-editsol" onclick="abrirEditarSol(\'{orden}\',\'{sol_actual.replace(chr(39), chr(92)+chr(39))}\')">✏ Sol.</button>'
        if estado in ('SIN_NRO', 'ERROR'):
            fname_sin = f"sin_nro_orden_{orden}.png"
            fname_err = f"error_orden_{orden}.png"
            screenshot_link = ""
            for fname in (fname_sin, fname_err):
                if os.path.exists(os.path.join(ERROR_PATH, fname)):
                    screenshot_link = f'<a class="btn-accion btn-screenshot" href="/errores/{fname}" target="_blank">📸 Ver error</a>'
                    break
            acciones_html += f"""
            <button class="btn-accion btn-reintentar" onclick="reintentar('{orden}')">↩ Reintentar</button>
            <button class="btn-accion btn-cargarnro"  onclick="abrirCargarNro('{orden}')">✎ Cargar nro</button>
            <button class="btn-accion btn-buscarnro"  onclick="buscarNroPortal('{orden}', this)">🔍 Buscar NRO</button>
            {screenshot_link}"""

        filas_html += f"""<tr data-estado="{estado}">
            <td data-sort="{orden}"><strong>{orden}</strong></td>
            <td>{badge_tipo(tipo)}</td>
            <td>{r.get('APELLIDO','')}</td>
            <td>{r.get('NOMBRE','')}</td>
            <td>{doc_col}</td>
            <td>{partido_col}</td>
            <td>{mat_col}</td>
            <td>{sol_actual}</td>
            <td data-sort="{estado}">{badge_estado(estado)}</td>
            <td>{r.get('FECHA_CARGA','')}</td>
            <td>{r.get('NRO_TRAMITE','')}</td>
            <td style="max-width:220px;color:var(--warn);font-size:11px;font-family:var(--mono)">{notas_val}</td>
            <td>{acciones_html}</td>
        </tr>"""

    lista_sol_json = json.dumps(obtener_solicitantes())

    html = CSS_JS + topbar("/pendientes") + f"""
    <style>
    .filtros {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; align-items:center; }}
    .btn-filtro {{ background:none; border:1px solid var(--border); color:var(--text2); padding:5px 12px;
                   font-family:var(--mono); font-size:11px; cursor:pointer; border-radius:2px; }}
    .btn-filtro:hover {{ border-color:var(--accent); color:var(--accent); }}
    .btn-filtro.activo {{ border-color:var(--accent); color:var(--accent); background:rgba(74,183,232,0.08); }}
    th.sortable {{ cursor:pointer; user-select:none; }}
    th.sortable:hover {{ color:var(--accent); }}
    th.sort-asc::after  {{ content:" ▲"; font-size:9px; }}
    th.sort-desc::after {{ content:" ▼"; font-size:9px; }}
    .btn-accion {{ border:none; padding:4px 8px; font-size:10px; font-family:var(--mono);
                   cursor:pointer; border-radius:2px; margin-right:4px; }}
    .btn-reintentar {{ background:rgba(232,164,74,0.12); color:var(--warn); border:1px solid rgba(232,164,74,0.3); }}
    .btn-reintentar:hover {{ background:rgba(232,164,74,0.25); }}
    .btn-cargarnro  {{ background:rgba(74,183,232,0.12); color:var(--accent); border:1px solid rgba(74,183,232,0.3); }}
    .btn-cargarnro:hover  {{ background:rgba(74,183,232,0.25); }}
    .btn-editsol    {{ background:rgba(120,200,120,0.12); color:#7bc47b; border:1px solid rgba(120,200,120,0.3); }}
    .btn-editsol:hover {{ background:rgba(120,200,120,0.25); }}
    .btn-buscarnro  {{ background:rgba(180,130,230,0.12); color:#b482e6; border:1px solid rgba(180,130,230,0.3); }}
    .btn-buscarnro:hover {{ background:rgba(180,130,230,0.25); }}
    .btn-buscarnro:disabled {{ opacity:0.5; cursor:wait; }}
    .btn-screenshot {{ background:rgba(200,200,200,0.08); color:#aaa; border:1px solid rgba(200,200,200,0.25); text-decoration:none; display:inline-block; }}
    .btn-screenshot:hover {{ background:rgba(200,200,200,0.2); color:#fff; }}
    #modal-manual {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.7);
                     z-index:600; align-items:center; justify-content:center; }}
    #modal-manual.open {{ display:flex; }}
    .modal-inner {{ background:var(--surface); border:1px solid var(--border); padding:28px; max-width:380px; width:90%; }}
    .modal-inner .row {{ display:flex; align-items:center; margin-bottom:12px; }}
    .modal-inner .lbl {{ width:110px; font-size:12px; color:var(--text2); }}
    .modal-inner input {{ flex:1; }}
    </style>

    <div class="page">
        <div class="stats">
            <div class="stat-card"><div class="num">{total}</div><div class="lab">Total registros</div></div>
            <div class="stat-card" style="cursor:pointer" onclick="filtrar('PENDIENTE')"><div class="num" style="color:var(--warn)">{pend}</div><div class="lab">⏳ Pendientes</div></div>
            <div class="stat-card" style="cursor:pointer" onclick="filtrar('CARGADO')"><div class="num" style="color:var(--accent3)">{carg}</div><div class="lab">📤 Cargados</div></div>
            <div class="stat-card" style="cursor:pointer" onclick="filtrar('COMPLETADO')"><div class="num" style="color:#5b8dee">{comp}</div><div class="lab">✅ Completados</div></div>
            <div class="stat-card" style="cursor:pointer" onclick="filtrar('SIN_NRO')"><div class="num" style="color:#c07ef0">{sinnro}</div><div class="lab">❓ Sin Nro</div></div>
            <div class="stat-card" style="cursor:pointer" onclick="filtrar('ERROR')"><div class="num" style="color:var(--danger)">{errores_db}</div><div class="lab">❌ Error</div></div>
        </div>
        <div class="card">
            <div class="card-header">
                <div class="hbar"></div>Todos los pedidos
                <div class="filtros" style="margin-top:12px;margin-bottom:0">
                    <button class="btn-filtro activo" id="f-todos"      onclick="filtrar('')">Todos ({total})</button>
                    <button class="btn-filtro" id="f-PENDIENTE"  onclick="filtrar('PENDIENTE')">⏳ Pendientes ({pend})</button>
                    <button class="btn-filtro" id="f-CARGADO"    onclick="filtrar('CARGADO')">📤 Cargados ({carg})</button>
                    <button class="btn-filtro" id="f-COMPLETADO" onclick="filtrar('COMPLETADO')">✅ Completados ({comp})</button>
                    <button class="btn-filtro" id="f-SIN_NRO"    onclick="filtrar('SIN_NRO')">❓ Sin Nro ({sinnro})</button>
                    <button class="btn-filtro" id="f-ERROR"      onclick="filtrar('ERROR')">❌ Error ({errores_db})</button>
                    <a href="/export/pedidos" class="btn-filtro" style="text-decoration:none;margin-left:auto">⬇ Exportar CSV</a>
                </div>
            </div>
            <div style="overflow-x:auto">
            <table id="tbl-pedidos">
                <thead><tr>
                    <th class="sortable" data-col="0">ORDEN</th>
                    <th data-col="1">TIPO</th>
                    <th class="sortable" data-col="2">APELLIDO</th>
                    <th data-col="3">NOMBRE</th>
                    <th data-col="4">DOC / INMUEBLE</th>
                    <th data-col="5">PTD</th><th data-col="6">MAT</th>
                    <th class="sortable" data-col="7">SOLICITANTE</th>
                    <th class="sortable" data-col="8">ESTADO</th>
                    <th data-col="9">F.CARGA</th>
                    <th data-col="10">NRO TRÁMITE</th>
                    <th data-col="11">NOTAS</th>
                    <th data-col="12">ACCIONES</th>
                </tr></thead>
                <tbody id="tbody-pedidos">{filas_html if filas_html else '<tr><td colspan="13" style="text-align:center;padding:30px;color:#999">No hay datos todavía</td></tr>'}</tbody>
            </table>
            </div>
        </div>
    </div>

    <!-- Modal editar solicitante -->
    <div id="modal-editsol" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:600;align-items:center;justify-content:center;">
        <div class="modal-inner">
            <div style="font-family:var(--mono);font-size:10px;color:#7bc47b;letter-spacing:2px;margin-bottom:16px;">// EDITAR SOLICITANTE</div>
            <div style="font-size:13px;color:var(--text2);margin-bottom:18px;">
                Orden <strong id="editsol-orden-label" style="color:var(--text)"></strong>
            </div>
            <div class="row">
                <div class="lbl">Solicitante</div>
                <select id="editsol-sel" style="width:180px" onchange="toggleEditsolOtro(this)"></select>
            </div>
            <div class="row" id="editsol-otro-box" style="display:none;">
                <div class="lbl">Nombre</div>
                <input type="text" id="editsol-otro-inp" placeholder="Escribí el nombre..." style="width:180px" oninput="this.value=this.value.toUpperCase()">
            </div>
            <div style="display:flex;gap:8px;margin-top:8px;">
                <button class="btn btn-primary" onclick="confirmarEditarSol()">Guardar</button>
                <button class="btn btn-secondary" onclick="cerrarEditarSol()">Cancelar</button>
            </div>
        </div>
    </div>

    <!-- Modal carga manual de número de trámite -->
    <div id="modal-manual">
        <div class="modal-inner">
            <div style="font-family:var(--mono);font-size:10px;color:var(--accent);letter-spacing:2px;margin-bottom:16px;">// CARGAR NRO MANUALMENTE</div>
            <div style="font-size:13px;color:var(--text2);margin-bottom:18px;">
                Orden <strong id="modal-orden-label" style="color:var(--text)"></strong>
            </div>
            <div class="row">
                <div class="lbl">Nro de trámite</div>
                <input type="text" id="inp-nro" placeholder="ej: 17897760" style="width:150px" oninput="this.value=this.value.replace(/\\D/g,'')">
            </div>
            <div class="row">
                <div class="lbl">Fecha de carga</div>
                <input type="text" id="inp-fecha" placeholder="DD/MM/AAAA" style="width:110px">
            </div>
            <div style="display:flex;gap:8px;margin-top:8px;">
                <button class="btn btn-primary" onclick="confirmarCargarNro()">Guardar → CARGADO</button>
                <button class="btn btn-secondary" onclick="cerrarModalManual()">Cancelar</button>
            </div>
        </div>
    </div>

    <script>
    let sortCol = -1, sortAsc = true, filtroActivo = '', ordenManual = '';
    const SOLICITANTES_LIST = {lista_sol_json};
    let editsolOrden = '';

    function filtrar(estado) {{
        filtroActivo = estado;
        document.querySelectorAll('.btn-filtro').forEach(b => b.classList.remove('activo'));
        const id = estado ? 'f-' + estado : 'f-todos';
        const btn = document.getElementById(id);
        if (btn) btn.classList.add('activo');
        document.querySelectorAll('#tbody-pedidos tr').forEach(tr => {{
            const est = tr.dataset.estado || '';
            tr.style.display = (!estado || est === estado) ? '' : 'none';
        }});
    }}

    document.querySelectorAll('th.sortable').forEach(th => {{
        th.addEventListener('click', () => {{
            const col = parseInt(th.dataset.col);
            if (sortCol === col) {{ sortAsc = !sortAsc; }}
            else {{ sortCol = col; sortAsc = true; }}
            document.querySelectorAll('th.sortable').forEach(t => t.classList.remove('sort-asc','sort-desc'));
            th.classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
            const tbody = document.getElementById('tbody-pedidos');
            const rows  = Array.from(tbody.querySelectorAll('tr'));
            rows.sort((a, b) => {{
                const tds_a = a.querySelectorAll('td'), tds_b = b.querySelectorAll('td');
                if (!tds_a[col] || !tds_b[col]) return 0;
                const va = tds_a[col].dataset.sort || tds_a[col].textContent.trim();
                const vb = tds_b[col].dataset.sort || tds_b[col].textContent.trim();
                const na = parseFloat(va), nb = parseFloat(vb);
                const cmp = (!isNaN(na) && !isNaN(nb)) ? na - nb : va.localeCompare(vb, 'es');
                return sortAsc ? cmp : -cmp;
            }});
            rows.forEach(r => tbody.appendChild(r));
            filtrar(filtroActivo);
        }});
    }});

    function reintentar(orden) {{
        if (!confirm('¿Pasar la orden ' + orden + ' a PENDIENTE para volver a intentar?')) return;
        fetch('/api/reintentar', {{method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{orden}})
        }}).then(r => r.json()).then(d => {{ if (d.ok) location.reload(); else alert('Error: ' + d.error); }});
    }}

    function abrirCargarNro(orden) {{
        ordenManual = orden;
        document.getElementById('modal-orden-label').textContent = orden;
        document.getElementById('inp-nro').value   = '';
        document.getElementById('inp-fecha').value = hoy();
        document.getElementById('modal-manual').classList.add('open');
        document.getElementById('inp-nro').focus();
    }}
    function cerrarModalManual() {{ document.getElementById('modal-manual').classList.remove('open'); }}
    function confirmarCargarNro() {{
        const nro = document.getElementById('inp-nro').value.trim();
        const fecha = document.getElementById('inp-fecha').value.trim();
        if (!nro)   {{ alert('Ingresá el número de trámite'); return; }}
        if (!fecha) {{ alert('Ingresá la fecha de carga'); return; }}
        if (!/^\\d{{2}}\\/\\d{{2}}\\/\\d{{4}}$/.test(fecha)) {{ alert('Formato: DD/MM/AAAA'); return; }}
        fetch('/api/cargar_manual', {{method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{orden: ordenManual, nro_tramite: nro, fecha_carga: fecha}})
        }}).then(r => r.json()).then(d => {{ if (d.ok) location.reload(); else alert('Error: ' + d.error); }});
    }}
    document.getElementById('modal-manual').addEventListener('click', function(e) {{ if (e.target === this) cerrarModalManual(); }});

    function abrirEditarSol(orden, solActual) {{
        editsolOrden = orden;
        document.getElementById('editsol-orden-label').textContent = orden;
        const sel = document.getElementById('editsol-sel');
        sel.innerHTML = '';
        SOLICITANTES_LIST.forEach(s => {{
            const opt = document.createElement('option');
            opt.value = s; opt.text = s;
            if (s === solActual) opt.selected = true;
            sel.appendChild(opt);
        }});
        const optOtro = document.createElement('option');
        optOtro.value = '__OTRO__'; optOtro.text = '★ Otro ★';
        sel.appendChild(optOtro);
        if (solActual && !SOLICITANTES_LIST.includes(solActual)) {{
            optOtro.selected = true;
            document.getElementById('editsol-otro-box').style.display = 'flex';
            document.getElementById('editsol-otro-inp').value = solActual;
        }} else {{ document.getElementById('editsol-otro-box').style.display = 'none'; }}
        document.getElementById('modal-editsol').style.display = 'flex';
    }}
    function toggleEditsolOtro(sel) {{
        const box = document.getElementById('editsol-otro-box');
        if (sel.value === '__OTRO__') {{ box.style.display = 'flex'; document.getElementById('editsol-otro-inp').focus(); }}
        else {{ box.style.display = 'none'; document.getElementById('editsol-otro-inp').value = ''; }}
    }}
    function cerrarEditarSol() {{ document.getElementById('modal-editsol').style.display = 'none'; }}
    function confirmarEditarSol() {{
        const sel = document.getElementById('editsol-sel');
        let valor = sel.value;
        if (valor === '__OTRO__') {{
            valor = document.getElementById('editsol-otro-inp').value.trim().toUpperCase();
            if (!valor) {{ alert('Ingresá el nombre del solicitante'); return; }}
        }}
        fetch('/api/editar_solicitante', {{method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{orden: editsolOrden, solicitante: valor}})
        }}).then(r => r.json()).then(d => {{ if (d.ok) location.reload(); else alert('Error: ' + d.error); }});
    }}
    document.getElementById('modal-editsol').addEventListener('click', function(e) {{ if (e.target === this) cerrarEditarSol(); }});

    function buscarNroPortal(orden, btn) {{
        if (!confirm('¿Buscar el NRO de la orden ' + orden + ' en el portal RPI?\\nEsto abre una sesión en segundo plano (~20 seg).')) return;
        btn.disabled = true;
        const orig = btn.textContent;
        btn.textContent = '⏳ Buscando...';
        fetch('/api/buscar_sin_nro', {{method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{orden}})
        }}).then(r => r.json()).then(d => {{
            btn.disabled = false; btn.textContent = orig;
            if (d.ok) {{ alert('✅ ' + d.msg); location.reload(); }}
            else      {{ alert('❌ ' + (d.error || d.msg)); }}
        }}).catch(() => {{ btn.disabled = false; btn.textContent = orig; alert('Error de conexión'); }});
    }}

    function hoy() {{
        const d = new Date();
        return String(d.getDate()).padStart(2,'0') + '/' +
               String(d.getMonth()+1).padStart(2,'0') + '/' + d.getFullYear();
    }}
    </script>
    """
    return html


# =====================================================
# ESTADÍSTICAS
# =====================================================

@app.route("/estadisticas")
def estadisticas():
    try:
        with get_db() as conn:
            estados_rows = conn.execute("""
                SELECT ESTADO, COUNT(DISTINCT ORDEN) as cnt
                FROM tramites GROUP BY ESTADO ORDER BY cnt DESC
            """).fetchall()
            tipos_rows = conn.execute("""
                SELECT TIPO_SOLICITUD, COUNT(DISTINCT ORDEN) as cnt
                FROM tramites GROUP BY TIPO_SOLICITUD ORDER BY cnt DESC
            """).fetchall()
            sol_rows = conn.execute("""
                SELECT SOLICITANTE, COUNT(DISTINCT ORDEN) as cnt
                FROM tramites WHERE SOLICITANTE IS NOT NULL AND SOLICITANTE != ''
                GROUP BY SOLICITANTE ORDER BY cnt DESC LIMIT 10
            """).fetchall()
            meses_rows = conn.execute("""
                SELECT substr(FECHA_CARGA,4,2) || '/' || substr(FECHA_CARGA,7,4) as mes,
                       COUNT(DISTINCT ORDEN) as cnt
                FROM tramites WHERE ESTADO IN ('CARGADO','COMPLETADO')
                  AND FECHA_CARGA IS NOT NULL AND length(FECHA_CARGA) = 10
                GROUP BY mes ORDER BY substr(FECHA_CARGA,7,4), substr(FECHA_CARGA,4,2)
            """).fetchall()
            total = conn.execute("SELECT COUNT(DISTINCT ORDEN) FROM tramites").fetchone()[0]
    except:
        estados_rows, tipos_rows, sol_rows, meses_rows, total = [], [], [], [], 0

    estados_labels = [r[0] for r in estados_rows]
    estados_data   = [r[1] for r in estados_rows]
    COLORES_ESTADO = {"COMPLETADO":"#5b8dee","CARGADO":"#4ab79a","PENDIENTE":"#e8a44a","SIN_NRO":"#c07ef0","ERROR":"#e84a4a"}
    estados_colors = [COLORES_ESTADO.get(e, "#888") for e in estados_labels]
    tipos_labels   = [r[0] for r in tipos_rows]
    tipos_data     = [r[1] for r in tipos_rows]
    COLORES_TIPO   = {"755":"#4ab7e8","752":"#4ab79a","754":"#e8a44a","753PH":"#c07ef0","753":"#c07ef0"}
    tipos_colors   = [COLORES_TIPO.get(str(t), "#888") for t in tipos_labels]
    sol_labels  = [r[0] for r in sol_rows]
    sol_data    = [r[1] for r in sol_rows]
    meses_labels = [r[0] for r in meses_rows]
    meses_data   = [r[1] for r in meses_rows]
    kpi_comp = next((r[1] for r in estados_rows if r[0] == 'COMPLETADO'), 0)
    kpi_pend = next((r[1] for r in estados_rows if r[0] == 'PENDIENTE'), 0)
    kpi_carg = next((r[1] for r in estados_rows if r[0] == 'CARGADO'), 0)

    html = CSS_JS + topbar("/estadisticas") + f"""
    <style>
    .charts-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-bottom:18px; }}
    .charts-grid .card {{ margin:0; }}
    .chart-wrap {{ position:relative; height:280px; }}
    .kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:18px; }}
    .kpi {{ background:var(--surface); border:1px solid var(--border); padding:18px; text-align:center; }}
    .kpi .big {{ font-size:32px; font-weight:700; font-family:var(--mono); }}
    .kpi .sub {{ font-size:11px; color:var(--text2); margin-top:4px; font-family:var(--mono); letter-spacing:.5px; }}
    @media(max-width:700px){{ .charts-grid{{ grid-template-columns:1fr; }} }}
    </style>
    <div class="page">
        <div style="font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:2px;margin-bottom:14px;">
            // ESTADÍSTICAS — {total} pedidos en total
        </div>
        <div class="kpi-grid">
            <div class="kpi"><div class="big">{total}</div><div class="sub">TOTAL PEDIDOS</div></div>
            <div class="kpi"><div class="big" style="color:#5b8dee">{kpi_comp}</div><div class="sub">COMPLETADOS</div></div>
            <div class="kpi"><div class="big" style="color:#4ab79a">{kpi_carg}</div><div class="sub">CARGADOS (en RPI)</div></div>
            <div class="kpi"><div class="big" style="color:var(--warn)">{kpi_pend}</div><div class="sub">PENDIENTES</div></div>
        </div>
        <div class="charts-grid">
            <div class="card">
                <div class="card-header"><div class="hbar"></div>Distribución por Estado</div>
                <div class="card-body"><div class="chart-wrap"><canvas id="ch-estados"></canvas></div></div>
            </div>
            <div class="card">
                <div class="card-header"><div class="hbar"></div>Distribución por Tipo de Informe</div>
                <div class="card-body"><div class="chart-wrap"><canvas id="ch-tipos"></canvas></div></div>
            </div>
        </div>
        <div class="card" style="margin-bottom:18px">
            <div class="card-header"><div class="hbar"></div>Top Solicitantes</div>
            <div class="card-body"><div class="chart-wrap"><canvas id="ch-sol"></canvas></div></div>
        </div>
        <div class="card">
            <div class="card-header"><div class="hbar"></div>Pedidos Cargados por Mes</div>
            <div class="card-body"><div class="chart-wrap"><canvas id="ch-meses"></canvas></div></div>
        </div>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script>
    Chart.defaults.color = '#888';
    Chart.defaults.font.family = "'DM Mono', monospace";
    Chart.defaults.font.size = 11;
    const gc = 'rgba(255,255,255,0.07)';
    new Chart(document.getElementById('ch-estados'), {{
        type:'doughnut', data:{{labels:{json.dumps(estados_labels)},datasets:[{{data:{json.dumps(estados_data)},backgroundColor:{json.dumps(estados_colors)},borderColor:'var(--bg)',borderWidth:2}}]}},
        options:{{plugins:{{legend:{{position:'right'}}}},cutout:'65%'}}
    }});
    new Chart(document.getElementById('ch-tipos'), {{
        type:'doughnut', data:{{labels:{json.dumps(tipos_labels)},datasets:[{{data:{json.dumps(tipos_data)},backgroundColor:{json.dumps(tipos_colors)},borderColor:'var(--bg)',borderWidth:2}}]}},
        options:{{plugins:{{legend:{{position:'right'}}}},cutout:'65%'}}
    }});
    new Chart(document.getElementById('ch-sol'), {{
        type:'bar', data:{{labels:{json.dumps(sol_labels)},datasets:[{{label:'Pedidos',data:{json.dumps(sol_data)},backgroundColor:'rgba(74,183,232,0.7)',borderColor:'#4ab7e8',borderWidth:1}}]}},
        options:{{indexAxis:'y',plugins:{{legend:{{display:false}}}},scales:{{x:{{grid:{{color:gc}}}},y:{{grid:{{color:gc}}}}}}}}
    }});
    new Chart(document.getElementById('ch-meses'), {{
        type:'bar', data:{{labels:{json.dumps(meses_labels)},datasets:[{{label:'Pedidos cargados',data:{json.dumps(meses_data)},backgroundColor:'rgba(91,141,238,0.7)',borderColor:'#5b8dee',borderWidth:1}}]}},
        options:{{plugins:{{legend:{{display:false}}}},scales:{{x:{{grid:{{color:gc}}}},y:{{grid:{{color:gc}},ticks:{{stepSize:1}}}}}}}}
    }});
    </script>
    """
    return html


# =====================================================
# EXPORTAR / SCREENSHOTS
# =====================================================

@app.route("/errores/<filename>")
def ver_screenshot(filename):
    from flask import send_from_directory
    if not re.match(r'^[\w\-]+\.png$', filename):
        return "Archivo no válido", 400
    return send_from_directory(ERROR_PATH, filename)


@app.route("/export/pedidos")
def export_pedidos():
    import csv, io
    from flask import Response
    with get_db() as conn:
        rows = conn.execute("""
            SELECT ORDEN, TIPO_SOLICITUD, APELLIDO, NOMBRE, DNI, CUIT,
                   PARTIDO, NRO_INSCRIPCION, UF_UC, SOLICITANTE, ESTADO,
                   NRO_TRAMITE, FECHA_CARGA, NOTAS
            FROM tramites ORDER BY CAST(ORDEN AS INTEGER) ASC, id ASC
        """).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["ORDEN","TIPO","APELLIDO","NOMBRE","DNI","CUIT",
                     "PARTIDO","NRO_INSCRIPCION","UF_UC","SOLICITANTE",
                     "ESTADO","NRO_TRAMITE","FECHA_CARGA","NOTAS"])
    for r in rows:
        writer.writerow(list(r))
    nombre = f"pedidos_rpi_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        "\ufeff" + buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={nombre}"}
    )


# =====================================================
# API ACCIONES SOBRE ÓRDENES
# =====================================================

@app.route("/api/reintentar", methods=["POST"])
def api_reintentar():
    orden = request.json.get("orden")
    if not orden:
        return jsonify({"ok": False, "error": "orden requerida"})
    with get_db() as conn:
        conn.execute(
            "UPDATE tramites SET ESTADO='PENDIENTE', NRO_TRAMITE='', NOTAS='' WHERE ORDEN=?",
            (orden,)
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/editar_solicitante", methods=["POST"])
def api_editar_solicitante():
    data  = request.json
    orden = data.get("orden", "").strip()
    sol   = data.get("solicitante", "").strip().upper()
    if not orden or not sol:
        return jsonify({"ok": False, "error": "orden y solicitante son requeridos"})
    with get_db() as conn:
        conn.execute("UPDATE tramites SET SOLICITANTE=? WHERE ORDEN=?", (sol, orden))
        conn.commit()
    registrar_uso_solicitante(sol)
    return jsonify({"ok": True})


@app.route("/api/cargar_manual", methods=["POST"])
def api_cargar_manual():
    data  = request.json
    orden = data.get("orden")
    nro   = data.get("nro_tramite", "").strip()
    fecha = data.get("fecha_carga", "").strip()
    if not orden or not nro or not fecha:
        return jsonify({"ok": False, "error": "orden, nro_tramite y fecha_carga son requeridos"})
    with get_db() as conn:
        conn.execute(
            "UPDATE tramites SET ESTADO='CARGADO', NRO_TRAMITE=?, FECHA_CARGA=?, NOTAS='' WHERE ORDEN=?",
            (nro, fecha, orden)
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/buscar_sin_nro", methods=["POST"])
def api_buscar_sin_nro():
    if estado_proceso["corriendo"]:
        return jsonify({"ok": False, "error": "Hay un proceso Playwright en curso. Esperá que termine."})
    orden = (request.json or {}).get("orden", "").strip()
    if not orden:
        return jsonify({"ok": False, "error": "orden requerida"})

    resultado = {"ok": False, "msg": ""}

    def run():
        async def _inner():
            async with async_playwright() as pw:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=os.path.join(USER_DATA_DIR, ".browser_data"),
                    headless=True,
                    accept_downloads=True,
                    downloads_path=DOWNLOAD_PATH,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                page = await context.new_page()
                if not await iniciar_sesion_pw(page):
                    resultado["msg"] = "No se pudo iniciar sesión en el portal"
                    await context.close()
                    return
                ok, msg = await ejecutar_buscar_sin_nro_pw(page, orden)
                resultado["ok"] = ok
                resultado["msg"] = msg
                await context.close()
        asyncio.run(_inner())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=90)
    if t.is_alive():
        return jsonify({"ok": False, "error": "Timeout: la búsqueda tardó demasiado"})
    return jsonify(resultado)


# =====================================================
# API PARA PROCESO PLAYWRIGHT
# =====================================================

@app.route("/iniciar_proceso", methods=["POST"])
def iniciar_proceso():
    global estado_proceso
    if estado_proceso["corriendo"]:
        return jsonify({"ok": False, "error": "Ya hay un proceso en curso"})

    data = request.json
    accion = data.get("accion")
    desde  = data.get("desde", "")
    hasta  = data.get("hasta", "")
    modo_visible        = data.get("modo_visible", False)
    confirmacion_manual = data.get("confirmacion_manual", False)
    estado_proceso = {"corriendo": True, "log": [], "progreso": 0, "total": 0, "fase": "Iniciando...",
                      "esperando_confirmacion": False, "orden_confirmacion": "", "dialogo_portal": ""}

    def run():
        asyncio.run(proceso_playwright(
            accion, desde, hasta,
            headless=not modo_visible,
            confirmacion_manual=confirmacion_manual
        ))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/estado_proceso")
def get_estado():
    return jsonify(estado_proceso)


@app.route("/api/confirmar_formulario", methods=["POST"])
def api_confirmar_formulario():
    if not estado_proceso.get("esperando_confirmacion"):
        return jsonify({"ok": False, "error": "No hay formulario esperando confirmación"})
    _confirmacion_event.set()
    return jsonify({"ok": True})


@app.route("/api/cancelar_formulario", methods=["POST"])
def api_cancelar_formulario():
    if not estado_proceso.get("esperando_confirmacion"):
        return jsonify({"ok": False, "error": "No hay formulario esperando confirmación"})
    estado_proceso["esperando_confirmacion"] = False
    estado_proceso["orden_confirmacion"] = ""
    _confirmacion_event.set()
    log_proceso("🚫 Formulario cancelado por el usuario")
    return jsonify({"ok": True})


# =====================================================
# PROCESO PLAYWRIGHT (async)
# =====================================================

async def proceso_playwright(accion, f_desde="", f_hasta="", headless=True, confirmacion_manual=False):
    global estado_proceso
    modo = "segundo plano (automático)" if headless else "visible"
    log_proceso(f"🖥️  Modo navegador: {modo}")
    try:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=os.path.join(USER_DATA_DIR, ".browser_data"),
                headless=headless,
                accept_downloads=True,
                downloads_path=DOWNLOAD_PATH,
                args=["--disable-blink-features=AutomationControlled"]
            )
            page = await context.new_page()

            if not await iniciar_sesion_pw(page):
                await context.close()
                estado_proceso["corriendo"] = False
                return

            if accion in ("solicitar", "solicitar_descargar"):
                await ejecutar_carga_pw(page, confirmacion_manual=confirmacion_manual)

            if accion in ("solicitar_descargar", "descargar"):
                if not f_desde:
                    f_desde = await sugerir_fecha_pw(page)
                if not f_hasta:
                    f_hasta = datetime.now().strftime("%d/%m/%Y")
                await ejecutar_descarga_pw(page, f_desde, f_hasta)

            await context.close()
    except Exception as e:
        import traceback
        log_proceso(f"❌ Error crítico: {e}")
        log_proceso(traceback.format_exc())
    finally:
        estado_proceso["corriendo"] = False


async def escribir_seguro(page, selector, texto):
    el = page.locator(selector)
    await el.scroll_into_view_if_needed()
    await el.click()
    await page.keyboard.press("Meta+a")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.3)
    for letra in str(texto):
        await page.keyboard.type(letra)
        await asyncio.sleep(0.10)


async def iniciar_sesion_pw(page):
    log_proceso("🔑 Iniciando sesión en RPI...")
    await page.goto("https://servicios.rpba.gob.ar/RegPropNew/signon/usernamePasswordLogin.jsp")
    try:
        await page.wait_for_selector('#josso_username', state='visible', timeout=15000)
    except:
        log_proceso("❌ No cargó la página de login")
        return False

    await page.fill('#josso_username', USUARIO)
    await page.fill('#josso_password', PASSWORD)
    await page.locator('#josso_password').press('Enter')

    try:
        await page.wait_for_url(re.compile(r".*index.*|.*VentanillaVirtual.*|.*RegProp.*"), timeout=10000)
        log_proceso("✅ Sesión iniciada con éxito.")
        return True
    except:
        if "usernamePasswordLogin" in page.url:
            log_proceso("❌ Credenciales incorrectas. Verificá el archivo .env")
            return False
        log_proceso("✅ Sesión iniciada.")
        return True


async def ejecutar_carga_pw(page, confirmacion_manual=False):
    log_proceso("🚀 Iniciando carga de trámites...")
    if confirmacion_manual:
        log_proceso("✋ Modo revisión: vas a confirmar cada formulario manualmente antes de enviarlo")
    estado_proceso["fase"] = "Cargando trámites"

    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM tramites WHERE ESTADO='PENDIENTE'
            ORDER BY CAST(ORDEN AS INTEGER), id
        """).fetchall()

    if not rows:
        log_proceso("☕ No hay trámites pendientes para cargar.")
        return

    ordenes = {}
    for r in rows:
        d = dict(r)
        o = d["ORDEN"]
        ordenes.setdefault(o, []).append(d)

    estado_proceso["total"] = len(ordenes)
    estado_proceso["progreso"] = 0

    for orden, grupo in ordenes.items():
        tipo = str(grupo[0]["TIPO_SOLICITUD"]).strip()
        log_proceso(f"\n[+] Orden {orden} — Tipo {tipo} — {grupo[0]['APELLIDO']}")

        try:
            fila0 = grupo[0]
            cuit_log = str(fila0.get("CUIT","") or "").strip()
            dni_log  = str(fila0.get("DNI","") or "").strip()
            doc_info = f"CUIT:{cuit_log}" if cuit_log else (f"DNI:{dni_log}" if dni_log else "sin doc")
            ptd_log  = str(fila0.get("PARTIDO","") or "").strip()
            mat_log  = str(fila0.get("NRO_INSCRIPCION","") or "").strip()
            if ptd_log or mat_log:
                doc_info += f" | PTD:{ptd_log} MAT:{mat_log}"
            log_proceso(f"   ⏳ {doc_info}")

            if tipo == "755":
                await completar_755(page, grupo[0])
            elif tipo == "752":
                titulares_blob = ", ".join(
                    f"{r['APELLIDO']} {r['NOMBRE']}".strip() for r in grupo
                )
                if len(grupo) > 1:
                    partes = [f"{r['APELLIDO']} {r['NOMBRE']}".strip() for r in grupo]
                    titulares_blob = ", ".join(partes[:-1]) + " y " + partes[-1]
                await completar_752(page, grupo[0], titulares_blob)
            elif tipo == "754":
                await completar_754(page, grupo[0])
            elif tipo in ("753PH", "753"):
                await completar_753ph(page, grupo[0])
            else:
                log_proceso(f"⚠️ Tipo {tipo} no soportado, saltando...")
                continue

            dialogo_portal = ""
            if confirmacion_manual:
                await esperar_submit_manual(page, orden=orden)
            else:
                dialogo_portal = await enviar_formulario(page)
                if dialogo_portal:
                    log_proceso(f"   ⚠️ Portal alertó: «{dialogo_portal}»")

            nro = await capturar_nro(page)
            if nro:
                with get_db() as conn:
                    conn.execute("""
                        UPDATE tramites SET ESTADO='CARGADO', NRO_TRAMITE=?, FECHA_CARGA=?, NOTAS=''
                        WHERE ORDEN=?
                    """, (nro, datetime.now().strftime("%d/%m/%Y"), orden))
                    conn.commit()
                log_proceso(f"✅ Orden {orden} → Trámite {nro}")
            else:
                msg_portal = ""
                try:
                    texto_pagina = await page.evaluate("() => document.body.innerText")
                    lineas = [l.strip() for l in texto_pagina.splitlines() if l.strip()]
                    msg_portal = " | ".join(lineas[:8])
                except:
                    pass

                ts = datetime.now().strftime("%d/%m/%Y %H:%M")
                nota = f"Sin NRO capturado ({ts})."
                if dialogo_portal:
                    nota += f" Alerta portal: «{dialogo_portal}»."
                elif msg_portal:
                    nota += f" Página: {msg_portal[:400]}"

                with get_db() as conn:
                    conn.execute(
                        "UPDATE tramites SET ESTADO='SIN_NRO', NOTAS=? WHERE ORDEN=?",
                        (nota, orden)
                    )
                    conn.commit()

                screenshot_path = os.path.join(ERROR_PATH, f"sin_nro_orden_{orden}.png")
                await page.screenshot(path=screenshot_path, full_page=True)
                log_proceso(f"⚠️ Orden {orden} → SIN_NRO")
                if msg_portal:
                    log_proceso(f"   Página dice: {msg_portal[:300]}")

            estado_proceso["progreso"] += 1

            try:
                btn = page.locator('[name="Cerrar"]')
                if await btn.is_visible(timeout=2000):
                    await btn.click()
            except:
                pass

        except Exception as e:
            msg_err = str(e)
            log_proceso(f"❌ Error en Orden {orden}: {msg_err}")
            screenshot_path = os.path.join(ERROR_PATH, f"error_orden_{orden}.png")
            await page.screenshot(path=screenshot_path, full_page=True)
            with get_db() as conn:
                conn.execute(
                    "UPDATE tramites SET ESTADO='ERROR', NOTAS=? WHERE ORDEN=?",
                    (f"Error al cargar: {msg_err[:300]} ({datetime.now().strftime('%d/%m/%Y %H:%M')})", orden)
                )
                conn.commit()

    log_proceso(f"\n✅ Carga finalizada. {estado_proceso['progreso']} / {estado_proceso['total']} órdenes procesadas.")
    guardar_log_sesion()


async def completar_755(page, fila):
    await page.goto("https://servicios.rpba.gob.ar/VentanillaVirtual/ventanillaVirtual/ControlarEscribanoIndiceDeTitularesSimpleAction.do?servicioId=159")
    await page.wait_for_selector('#cuit', state='visible', timeout=15000)
    await asyncio.sleep(1)
    await page.evaluate("var el=document.getElementById('tiposActos');el.value='76';$(el).trigger('chosen:updated');$(el).change();")
    await asyncio.sleep(0.5)
    await escribir_seguro(page, '#abreviatura749', "AGREGAR A INFORMES")

    cuit = str(fila.get("CUIT","")).strip().replace("-","")
    dni = str(fila.get("DNI","")).strip()

    if len(cuit) == 11:
        await escribir_seguro(page, '#cuit', cuit)
    elif dni:
        try:
            await escribir_seguro(page, '#documentoCuitDomic', f"DNI {dni}")
        except:
            pass

    await escribir_seguro(page, '#apellidoTitular', str(fila["APELLIDO"]))
    if fila.get("NOMBRE","").strip():
        try: await escribir_seguro(page, '#nombreTitular', str(fila["NOMBRE"]))
        except: pass
    if fila.get("PARTIDO","").strip():
        await escribir_seguro(page, '#partidoInmueble', str(fila["PARTIDO"]).split('.')[0])


async def completar_752(page, fila, titulares_blob):
    await page.goto("https://servicios.rpba.gob.ar/VentanillaVirtual/ventanillaVirtual/ControlarEscribanoInformeDeDominioSimpleAction.do?servicioId=160")
    await page.wait_for_selector('#tiposActos', state='visible', timeout=15000)
    await asyncio.sleep(1)
    await page.evaluate("var el=document.getElementById('tiposActos');el.value='76';$(el).trigger('chosen:updated');$(el).change();")
    await asyncio.sleep(0.5)
    await escribir_seguro(page, '#abreviatura749', "AGREGAR A INFORMES")
    if fila.get("PARTIDO","").strip():
        await escribir_seguro(page, '#partidoInmueble', str(fila["PARTIDO"]).split('.')[0])
    if fila.get("NRO_INSCRIPCION","").strip():
        await escribir_seguro(page, '#matriculaFolioLegajo', str(fila["NRO_INSCRIPCION"]).split('.')[0])
    mapeo = {"#circunscripcion":"C","#seccion":"S","#chacraN":"CH","#chacraL":"CH2",
             "#quintaN":"QTA","#quintaL":"QTA2","#fraccionN":"F","#fraccionL":"F2",
             "#manzanaN":"M","#manzanaL":"M2","#parcelaN":"P","#parcelaL":"P2","#subparcela":"SP"}
    for sel, col in mapeo.items():
        v = str(fila.get(col,"")).strip().split('.')[0]
        if v and v != 'nan':
            try: await escribir_seguro(page, sel, v)
            except: pass
    await escribir_seguro(page, '#titularesYObservaciones', titulares_blob)


async def completar_754(page, fila):
    await page.goto("https://servicios.rpba.gob.ar/VentanillaVirtual/ventanillaVirtual/ControlarEscribanoCopiaDeDominioSimpleAction.do?servicioId=158")
    await page.wait_for_selector('#tiposActos', state='visible', timeout=15000)
    await asyncio.sleep(1)
    await page.evaluate("var el=document.getElementById('tiposActos');el.value='76';$(el).trigger('chosen:updated');$(el).change();")
    await asyncio.sleep(0.5)
    await escribir_seguro(page, '#abreviatura749', "AGREGAR A INFORMES")
    if fila.get("PARTIDO","").strip():
        await escribir_seguro(page, '#partidoInmueble', str(fila["PARTIDO"]).split('.')[0])
    if fila.get("NRO_INSCRIPCION","").strip():
        try:
            await page.click('#radioMatricula')
            await escribir_seguro(page, '#matriculaFolioLegajo', str(fila["NRO_INSCRIPCION"]).split('.')[0])
        except: pass
    cuit = str(fila.get("CUIT","")).replace("-","").strip()
    if len(cuit) == 11:
        await escribir_seguro(page, '#cuit', cuit)
    try: await escribir_seguro(page, '#destino', "AGREGAR A INFORMES")
    except: pass


async def completar_753ph(page, fila):
    await page.goto("https://servicios.rpba.gob.ar/VentanillaVirtual/ventanillaVirtual/ControlarEscribanoInformeDeInhibicionPFSimpleAction.do?servicioId=162")
    await page.wait_for_selector('#abreviatura749', state='visible', timeout=15000)
    await asyncio.sleep(1)
    await page.evaluate("var el=document.getElementById('tiposActos');el.value='76';$(el).trigger('chosen:updated');$(el).change();")
    await asyncio.sleep(0.5)
    await escribir_seguro(page, '#abreviatura749', "AGREGAR A INFORMES")
    await escribir_seguro(page, '#apellido', str(fila["APELLIDO"]))
    if fila.get("NOMBRE","").strip():
        try: await escribir_seguro(page, '#nombres', str(fila["NOMBRE"]))
        except: pass
    dni = str(fila.get("DNI","")).strip()
    if dni:
        try: await escribir_seguro(page, '#documento', dni)
        except: pass


async def enviar_formulario(page) -> str:
    dialogo_caps: list[str] = []
    async def _on_dialog(dialog):
        dialogo_caps.append(dialog.message)
        await dialog.accept()
    page.once("dialog", _on_dialog)
    try:
        btn = page.locator("input[type='submit'][value*='Enviar'], input[type='submit'][value*='Continuar']").first
        await btn.click()
    except:
        try: await page.locator("form").first.evaluate("f => f.submit()")
        except: pass
    await asyncio.sleep(2)
    await _aceptar_pantalla_intermedia(page)
    return dialogo_caps[0] if dialogo_caps else ""


async def _aceptar_pantalla_intermedia(page):
    """Detecta y acepta la pantalla de confirmación de horario del portal RPI.
    Aparece después de las ~13:30hs con botones Continuar / Cancelar."""
    try:
        contenido = await page.content()
        es_pantalla_intermedia = any(p in contenido.lower() for p in [
            "fuera de horario", "horario de atención", "horario restringido",
            "desea continuar", "fuera del horario",
            "ventanilla virtual", "libro diario", "día hábil siguiente",
            "13:30", "13.30",
        ])
    except:
        es_pantalla_intermedia = False

    if not es_pantalla_intermedia:
        try:
            btn_h = page.locator('#aceptar')
            if await btn_h.is_visible(timeout=1500):
                log_proceso("⚠️  Confirmación detectada (#aceptar) → aceptando...")
                await btn_h.click()
                await asyncio.sleep(2)
        except:
            pass
        return

    log_proceso("⚠️  PANTALLA DE HORARIO DETECTADA → aceptando para continuar fuera de horario")
    try:
        sc_path = os.path.join(ERROR_PATH, f"pantalla_horario_{datetime.now().strftime('%H%M%S')}.png")
        await page.screenshot(path=sc_path)
        log_proceso(f"   Screenshot: errores/{os.path.basename(sc_path)}")
    except:
        pass

    SELECTORES_CONTINUAR = [
        'input[type="submit"][value="Continuar"]',
        'input[type="submit"][value="continuar"]',
        'input[type="button"][value="Continuar"]',
        'button:has-text("Continuar")',
        'a:has-text("Continuar")',
        'input[value*="Continuar"]',
        'input[value*="Aceptar"]',
        'input[value*="De acuerdo"]',
        '#aceptar',
    ]
    for sel in SELECTORES_CONTINUAR:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                log_proceso(f"   Click en: '{sel}'")
                await el.click()
                await asyncio.sleep(3)
                return
        except:
            pass

    log_proceso("✗ No se encontró el botón Continuar en la pantalla de horario → verificar screenshot")


async def capturar_nro(page):
    await asyncio.sleep(3)
    await _aceptar_pantalla_intermedia(page)
    await asyncio.sleep(2)
    elementos = await page.locator("b").all()
    for el in elementos:
        t = (await el.inner_text()).strip()
        if t.isdigit() and len(t) >= 5:
            return t
    return None


async def esperar_submit_manual(page, orden=""):
    """Modo con revisión: espera confirmación desde la UI web y envía el formulario.
    Si el portal devuelve un alert() de error, muestra el mensaje y permite reintentar."""
    global _confirmacion_event
    MAX_INTENTOS = 5

    for intento in range(MAX_INTENTOS):
        _confirmacion_event.clear()
        estado_proceso["esperando_confirmacion"] = True
        estado_proceso["orden_confirmacion"] = str(orden)
        estado_proceso["dialogo_portal"] = ""

        label = f" (intento {intento + 1})" if intento > 0 else ""
        log_proceso(f"⏸️  Formulario listo{label} → hacé click en CONFIRMAR Y ENVIAR en la interfaz web")

        for _ in range(300):  # 5 min máximo por intento
            await asyncio.sleep(1)
            if _confirmacion_event.is_set():
                break
        else:
            estado_proceso["esperando_confirmacion"] = False
            estado_proceso["orden_confirmacion"] = ""
            estado_proceso["dialogo_portal"] = ""
            log_proceso("⚠️  Tiempo de espera agotado → formulario cancelado")
            return

        fue_confirmado = estado_proceso.get("esperando_confirmacion", False)
        estado_proceso["esperando_confirmacion"] = False
        estado_proceso["orden_confirmacion"] = ""

        if not fue_confirmado:
            log_proceso("🚫 Formulario cancelado → saltando esta orden")
            estado_proceso["dialogo_portal"] = ""
            return

        log_proceso("✓ Confirmado → enviando formulario...")
        dialogo = await enviar_formulario(page)

        if not dialogo:
            estado_proceso["dialogo_portal"] = ""
            return

        estado_proceso["dialogo_portal"] = dialogo
        log_proceso(f"   ⚠️ Portal dice: «{dialogo}»")
        log_proceso(f"   Corregí los datos en el navegador y volvé a hacer click en CONFIRMAR, o cancelá")

    log_proceso(f"✗ Máximo de intentos ({MAX_INTENTOS}) alcanzado → orden saltada")


async def sugerir_fecha_pw(page):
    fecha_defecto = datetime.now() - timedelta(days=15)
    fecha_excel = None
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT FECHA_CARGA FROM tramites WHERE ESTADO='CARGADO' AND FECHA_CARGA != ''
            """).fetchall()
            fechas = []
            for r in rows:
                try:
                    fechas.append(datetime.strptime(r[0], "%d/%m/%Y"))
                except: pass
            if fechas:
                fecha_excel = min(fechas)
                log_proceso(f"💡 Excel: trámite más antiguo CARGADO: {fecha_excel.strftime('%d/%m/%Y')}")
    except: pass

    candidatas = [f for f in [fecha_defecto, fecha_excel] if f]
    return min(candidatas).strftime("%d/%m/%Y")


async def ejecutar_descarga_pw(page, f_desde, f_hasta):
    log_proceso(f"📂 Iniciando descarga ({f_desde} → {f_hasta})...")
    estado_proceso["fase"] = "Descargando informes"

    await page.goto("https://servicios.rpba.gob.ar/VentanillaVirtual/ventanillaVirtual/jsp/consultaTramiteWeb.jsp?servicioId=75")
    await page.wait_for_selector('#fechaDesde', state='visible', timeout=10000)
    await page.evaluate(f"document.getElementById('fechaDesde').value = '{f_desde}'")
    await page.evaluate(f"document.getElementById('fechaHasta').value = '{f_hasta}'")
    await page.locator("input[type='submit']").first.click()
    await asyncio.sleep(3)

    contenido = await page.content()
    if "EXCEDE LAS 300 OPERACIONES" in contenido:
        log_proceso("⚠️ El rango supera las 300 operaciones. Reducí el rango de fechas.")
        return

    descargados = set()
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            descargados = {l.strip() for l in f if l.strip()}

    filas = await page.locator("table tr").all()
    filas = filas[1:]
    if not filas:
        log_proceso("No se encontraron trámites en el rango de fechas.")
        return

    estado_proceso["total"] = len(filas)
    estado_proceso["progreso"] = 0
    nuevos = 0
    ingresados = 0

    log_proceso(f"Se encontraron {len(filas)} trámites en el portal.")

    for fila_tr in filas:
        cols = await fila_tr.locator("td").all()
        if not cols: continue
        nro_web = re.sub(r'\D', '', await cols[0].inner_text())
        if not nro_web: continue
        estado_proceso["progreso"] += 1

        if nro_web in descargados:
            continue

        btn_list = await fila_tr.locator("a[href*='descargarPDF']").all()
        if not btn_list:
            ingresados += 1
            continue

        log_proceso(f"📥 {nro_web}...")
        async with page.expect_download() as dl_info:
            await btn_list[0].click()
        download = await dl_info.value
        ruta_tmp = os.path.join(DOWNLOAD_PATH, download.suggested_filename or f"{nro_web}.pdf")
        await download.save_as(ruta_tmp)
        nuevos += 1

        # Renombrar
        nombre_final, carpeta_destino = await renombrar_pdf(nro_web, ruta_tmp)
        nombre_final = re.sub(r'[/:*?"<>|]', '-', nombre_final).strip()
        destino_final = os.path.join(carpeta_destino, nombre_final)
        os.rename(ruta_tmp, destino_final)

        # Marcar como COMPLETADO en DB
        hoy_str = datetime.now().strftime("%d/%m/%Y")
        with get_db() as conn:
            cur = conn.execute("""
                UPDATE tramites SET ESTADO='COMPLETADO', FECHA_COMPLETADO=?
                WHERE NRO_TRAMITE=? OR ORDEN IN (
                    SELECT ORDEN FROM tramites WHERE NRO_TRAMITE=?
                )
            """, (hoy_str, nro_web, nro_web))
            conn.commit()
            filas_actualizadas = cur.rowcount

        if filas_actualizadas == 0:
            rescatar_sin_nro(nro_web, destino_final)

        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "a") as f:
                f.write(f"{nro_web}\n")
        else:
            with open(LOG_FILE, "w") as f:
                f.write(f"{nro_web}\n")

        log_proceso(f"✅ {nombre_final}")

    log_proceso(f"\n--- Resumen ---")
    log_proceso(f"Nuevos descargados: {nuevos}")
    log_proceso(f"Sin PDF aún (en proceso): {ingresados}")


async def renombrar_pdf(nro_web, ruta_tmp):
    """Genera el nombre final del PDF según los datos de la DB o del PDF."""
    carpeta_destino = DOWNLOAD_PATH

    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT * FROM tramites WHERE NRO_TRAMITE=? LIMIT 1
            """, (nro_web,)).fetchall()

            if not rows:
                # Buscar por número limpio
                rows = conn.execute("""
                    SELECT * FROM tramites WHERE REPLACE(NRO_TRAMITE,'-','')=? LIMIT 1
                """, (nro_web,)).fetchall()

            if rows:
                f = dict(rows[0])
                tipo = str(f.get("TIPO_SOLICITUD","")).strip()
                orden = str(f.get("ORDEN","")).split('.')[0]
                ape = str(f.get("APELLIDO","")).strip()
                nom = str(f.get("NOMBRE","")).strip()
                sol = str(f.get("SOLICITANTE","")).strip()

                # Para 752/754: usar Partido + Matrícula si no hay nombre útil
                if tipo in ("752","754") and (not ape or ape.lower() == "nan"):
                    ptd = str(f.get("PARTIDO","")).split('.')[0]
                    mat = str(f.get("NRO_INSCRIPCION","")).split('.')[0]
                    nombre_final = f"{orden} - PTD {ptd} MAT {mat}.pdf"
                else:
                    # Ver si hay múltiples titulares
                    mismo_orden = conn.execute(
                        "SELECT COUNT(*) FROM tramites WHERE ORDEN=?", (f["ORDEN"],)
                    ).fetchone()[0]
                    if mismo_orden > 1 and tipo == "752":
                        nombre_final = f"{orden} - {ape} y OTROS.pdf"
                    else:
                        nombre_final = f"{orden} - {ape} {nom}.pdf".replace("  "," ").replace(" nan","").replace("nan ","")

                if sol and sol.lower() not in ("nan",""):
                    carpeta_destino = os.path.join(DOWNLOAD_PATH, sol)
                    os.makedirs(carpeta_destino, exist_ok=True)

                return nombre_final, carpeta_destino
    except:
        pass

    # Fallback: extraer del PDF
    titular = extraer_nombre_pdf(ruta_tmp)
    if titular:
        return f"{nro_web} - {titular}.pdf", DOWNLOAD_PATH
    return f"{nro_web}_REVISAR.pdf", DOWNLOAD_PATH


def extraer_nombre_pdf(ruta):
    try:
        with pdfplumber.open(ruta) as pdf:
            texto = ""
            for p in pdf.pages:
                t = p.extract_text()
                if t: texto += t + "\n"
            lineas = [l.strip() for l in texto.split('\n') if l.strip()]
            for i, l in enumerate(lineas):
                if "Apellido y Nombres/Razón Social:" in l:
                    partes = l.split("Social:")
                    if len(partes) > 1 and len(partes[1].strip()) > 3:
                        return partes[1].strip()
                    if i+1 < len(lineas): return lineas[i+1]
            # Para 752/754: buscar Partido y Matrícula
            for l in lineas:
                m = re.search(r'Partido[:\s]+(\d+).*[Mm]atr[íi]cula[:\s]+(\d+)', l)
                if m: return f"PTD {m.group(1)} MAT {m.group(2)}"
    except: pass
    return None


def extraer_datos_identificatorios_pdf(ruta):
    """Extrae CUIT/DNI y Partido+Matrícula del PDF para cruzar con órdenes SIN_NRO."""
    resultado = {}
    try:
        with pdfplumber.open(ruta) as pdf:
            texto = ""
            for p in pdf.pages:
                t = p.extract_text()
                if t: texto += t + "\n"

        m = re.search(r'\b(\d{2}-?\d{8}-?\d{1})\b', texto)
        if m:
            resultado["cuit"] = re.sub(r'\D', '', m.group(1))

        m = re.search(r'(?:DNI|D\.N\.I\.?)[:\s]+(\d{7,8})', texto, re.IGNORECASE)
        if m:
            resultado["dni"] = m.group(1).lstrip("0")

        m = re.search(r'Partido[:\s]+(\d+).*?[Mm]atr[íi]cula[:\s]+(\d+)', texto)
        if m:
            resultado["partido"] = m.group(1)
            resultado["matricula"] = m.group(2)
    except:
        pass
    return resultado


def rescatar_sin_nro(nro_web: str, ruta_tmp: str) -> bool:
    """Intenta asociar un NRO_TRAMITE del portal a una orden SIN_NRO/ERROR sin número.
    Retorna True si encontró y actualizó una orden."""
    datos = extraer_datos_identificatorios_pdf(ruta_tmp)
    if not datos:
        return False

    hoy_str = datetime.now().strftime("%d/%m/%Y")
    try:
        with get_db() as conn:
            candidatas = conn.execute("""
                SELECT ORDEN, CUIT, DNI, PARTIDO, NRO_INSCRIPCION
                FROM tramites
                WHERE ESTADO IN ('SIN_NRO', 'ERROR')
                  AND (NRO_TRAMITE IS NULL OR NRO_TRAMITE = '')
            """).fetchall()

            for row in candidatas:
                r = dict(row)
                cuit_db = re.sub(r'\D', '', str(r.get("CUIT") or ""))
                dni_db  = str(r.get("DNI") or "").lstrip("0")
                ptd_db  = str(r.get("PARTIDO") or "").split(".")[0].strip()
                mat_db  = str(r.get("NRO_INSCRIPCION") or "").split(".")[0].strip()

                match = False
                if datos.get("cuit") and cuit_db and datos["cuit"] == cuit_db:
                    match = True
                elif datos.get("dni") and dni_db and datos["dni"] == dni_db:
                    match = True
                elif datos.get("partido") and datos.get("matricula"):
                    if datos["partido"] == ptd_db and datos["matricula"] == mat_db:
                        match = True

                if match:
                    conn.execute("""
                        UPDATE tramites
                        SET ESTADO='COMPLETADO', NRO_TRAMITE=?, FECHA_CARGA=?,
                            FECHA_COMPLETADO=?, NOTAS='Rescatado automáticamente durante descarga'
                        WHERE ORDEN=?
                    """, (nro_web, hoy_str, hoy_str, r["ORDEN"]))
                    conn.commit()
                    log_proceso(f"🔗 Orden {r['ORDEN']} rescatada → NRO_TRAMITE={nro_web}")
                    return True
    except Exception as e:
        log_proceso(f"⚠️ Error en rescate SIN_NRO: {e}")
    return False


async def ejecutar_buscar_sin_nro_pw(page, orden: str) -> tuple:
    """Busca en el portal RPI el NRO_TRAMITE de una orden SIN_NRO usando las órdenes
    vecinas como referencia de secuencia. Devuelve (ok, mensaje)."""
    with get_db() as conn:
        row = conn.execute("""
            SELECT * FROM tramites
            WHERE ORDEN=? AND ESTADO IN ('SIN_NRO','ERROR')
              AND (NRO_TRAMITE IS NULL OR TRIM(NRO_TRAMITE)='')
            LIMIT 1
        """, (orden,)).fetchone()
    if not row:
        return False, "La orden no existe, ya tiene NRO_TRAMITE, o no está en SIN_NRO/ERROR"

    with get_db() as conn:
        refs = conn.execute("""
            SELECT DISTINCT ORDEN, NRO_TRAMITE, FECHA_CARGA
            FROM tramites
            WHERE ESTADO IN ('CARGADO','COMPLETADO')
              AND NRO_TRAMITE IS NOT NULL AND TRIM(NRO_TRAMITE) != ''
              AND CAST(ORDEN AS INTEGER) BETWEEN CAST(? AS INTEGER)-20 AND CAST(? AS INTEGER)+20
              AND ORDEN != ?
            ORDER BY ABS(CAST(ORDEN AS INTEGER) - CAST(? AS INTEGER)) ASC
            LIMIT 8
        """, (orden, orden, orden, orden)).fetchall()
    refs = [dict(r) for r in refs]

    if not refs:
        return False, "No hay órdenes vecinas con NRO_TRAMITE. No se puede determinar el rango."

    def parse_fecha(s):
        try:    return datetime.strptime(s, "%d/%m/%Y")
        except: return None

    fechas_ref = [f for f in (parse_fecha(r["FECHA_CARGA"]) for r in refs) if f]
    if not fechas_ref:
        return False, "Las órdenes de referencia no tienen FECHA_CARGA registrada"

    f_desde = (min(fechas_ref) - timedelta(days=2)).strftime("%d/%m/%Y")
    f_hasta = (max(fechas_ref) + timedelta(days=2)).strftime("%d/%m/%Y")

    ref_nros = sorted(
        int(re.sub(r'\D', '', str(r["NRO_TRAMITE"])))
        for r in refs if r["NRO_TRAMITE"]
    )

    log_proceso(f"🔍 Buscando NRO para orden {orden} → rango {f_desde}–{f_hasta} → refs: {ref_nros}")

    await page.goto("https://servicios.rpba.gob.ar/VentanillaVirtual/ventanillaVirtual/jsp/consultaTramiteWeb.jsp?servicioId=75")
    await page.wait_for_selector('#fechaDesde', state='visible', timeout=10000)
    await page.evaluate(f"document.getElementById('fechaDesde').value = '{f_desde}'")
    await page.evaluate(f"document.getElementById('fechaHasta').value = '{f_hasta}'")
    await page.locator("input[type='submit']").first.click()
    await asyncio.sleep(3)

    portal_entries = []
    filas = await page.locator("table tr").all()
    for fila_tr in filas[1:]:
        cols = await fila_tr.locator("td").all()
        if not cols:
            continue
        nro_str = re.sub(r'\D', '', await cols[0].inner_text())
        if not nro_str:
            continue
        fecha_portal = ""
        row_text = await fila_tr.inner_text()
        m = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', row_text)
        if m:
            fecha_portal = m.group(1)
        portal_entries.append((int(nro_str), nro_str, fecha_portal))

    if not portal_entries:
        return False, f"No se encontraron trámites en el portal para {f_desde}–{f_hasta}"

    log_proceso(f"   Portal devolvió {len(portal_entries)} trámites en ese rango")

    with get_db() as conn:
        asignados = {
            re.sub(r'\D', '', str(r[0]))
            for r in conn.execute(
                "SELECT NRO_TRAMITE FROM tramites WHERE NRO_TRAMITE IS NOT NULL AND NRO_TRAMITE != ''"
            ).fetchall()
        }

    candidatos = [
        (n_int, n_str, fecha)
        for n_int, n_str, fecha in portal_entries
        if n_str not in asignados
    ]

    if not candidatos:
        return False, "Todos los trámites encontrados en el portal ya están asignados en la DB"

    ref_min, ref_max = min(ref_nros), max(ref_nros)
    ref_center = (ref_min + ref_max) / 2

    dentro = [(n, s, f) for n, s, f in candidatos if ref_min <= n <= ref_max]
    fuera  = [(n, s, f) for n, s, f in candidatos if n < ref_min or n > ref_max]

    pool = dentro if dentro else fuera
    pool.sort(key=lambda x: abs(x[0] - ref_center))
    mejor = pool[0]

    nro_final   = mejor[1]
    fecha_final = mejor[2] or max(fechas_ref, default=datetime.now()).strftime("%d/%m/%Y")

    with get_db() as conn:
        conn.execute("""
            UPDATE tramites
            SET ESTADO='CARGADO', NRO_TRAMITE=?, FECHA_CARGA=?,
                NOTAS='NRO rescatado por búsqueda de adyacencia en portal RPI'
            WHERE ORDEN=?
        """, (nro_final, fecha_final, orden))
        conn.commit()

    log_proceso(f"✓ Orden {orden} rescatada → NRO_TRAMITE={nro_final} / FECHA={fecha_final}")
    return True, f"NRO_TRAMITE {nro_final} asignado a la orden {orden} (fecha {fecha_final})"


# =====================================================
# SETUP — PRIMER ARRANQUE
# =====================================================

_SETUP_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>GestorRPI — Configuración inicial</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0f0f11;--surface:#17171a;--surface2:#1e1e23;--border:#2a2a35;--border2:#35353f;
      --accent:#e8c84a;--accent2:#4a9ee8;--accent3:#4ae89a;--danger:#e84a4a;
      --text:#e8e8f0;--text2:#a0a0b8;--muted:#5a5a72;
      --mono:"DM Mono",monospace;--sans:"DM Sans",sans-serif;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:var(--sans);background:var(--bg);color:var(--text);
     min-height:100vh;display:flex;align-items:center;justify-content:center;}
.card{background:var(--surface);border:1px solid var(--border);width:100%;max-width:440px;}
.card-header{padding:14px 22px;font-family:var(--mono);font-size:11px;letter-spacing:1.5px;
             color:var(--text2);text-transform:uppercase;border-bottom:1px solid var(--border);
             background:var(--surface2);display:flex;align-items:center;gap:8px;}
.card-header::before{content:"//";color:var(--accent);}
.card-body{padding:28px;}
.brand{font-family:var(--mono);font-size:20px;font-weight:500;color:var(--accent);
       letter-spacing:3px;text-align:center;margin-bottom:6px;}
.subtitle{font-size:12px;color:var(--muted);text-align:center;margin-bottom:24px;
          font-family:var(--mono);}
.notice{background:rgba(232,200,74,.08);border:1px solid rgba(232,200,74,.3);
        color:var(--accent);padding:12px 14px;font-size:12px;font-family:var(--mono);
        margin-bottom:20px;line-height:1.6;}
.field{margin-bottom:16px;}
.field label{display:block;font-size:11px;font-family:var(--mono);color:var(--text2);
             letter-spacing:1px;margin-bottom:6px;text-transform:uppercase;}
.field input{width:100%;padding:10px 12px;border:1px solid var(--border2);
             background:var(--surface2);color:var(--text);font-size:14px;
             font-family:var(--sans);outline:none;transition:border-color .15s;}
.field input:focus{border-color:var(--accent2);box-shadow:0 0 0 2px rgba(74,158,232,.1);}
.field small{font-size:11px;color:var(--muted);font-family:var(--mono);margin-top:4px;display:block;}
.btn-save{width:100%;padding:13px;background:var(--accent2);border:none;color:white;
          font-size:13px;font-family:var(--mono);letter-spacing:1px;cursor:pointer;
          transition:background .15s;margin-top:8px;}
.btn-save:hover{background:#3a8ed8;}
.error{background:rgba(232,74,74,.1);border:1px solid var(--danger);color:var(--danger);
       padding:10px 14px;font-size:12px;font-family:var(--mono);margin-bottom:16px;}
</style>
</head>
<body>
<div class="card">
  <div class="card-header">Configuración inicial</div>
  <div class="card-body">
    <div class="brand">RPI GESTOR</div>
    <div class="subtitle">Primera vez en este equipo</div>
    <div class="notice">
      Ingresá tus credenciales del portal del RPI.<br>
      Se guardan solo en tu computadora — nunca salen de ella.
    </div>
    {error_block}
    <form method="POST">
      <div class="field">
        <label>Usuario RPI</label>
        <input type="text" name="usuario" required autofocus
               placeholder="Tu usuario del portal RPI" value="{usuario}"
               autocomplete="off">
        <small>El mismo que usás para ingresar al portal del RPI de CABA</small>
      </div>
      <div class="field">
        <label>Contraseña RPI</label>
        <input type="password" name="password" required placeholder="••••••••">
      </div>
      <hr style="border:none;border-top:1px solid var(--border);margin:20px 0;">
      <div style="font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:1.5px;margin-bottom:14px;">
        PROXY CORPORATIVO (OPCIONAL)
      </div>
      <div style="font-size:11px;color:var(--muted);font-family:var(--mono);margin-bottom:14px;line-height:1.6;">
        Solo completar si tu red requiere proxy para salir a internet (ej: redes bancarias o corporativas).
      </div>
      <div class="field">
        <label>URL del Proxy</label>
        <input type="text" name="proxy_url" placeholder="http://proxy.empresa.com:8080"
               value="{proxy_url}" autocomplete="off">
        <small>Incluí el protocolo y puerto. Dejá vacío si no usás proxy.</small>
      </div>
      <div class="field">
        <label>Usuario del Proxy</label>
        <input type="text" name="proxy_usuario" placeholder="usuario" value="{proxy_usuario}" autocomplete="off">
      </div>
      <div class="field">
        <label>Contraseña del Proxy</label>
        <input type="password" name="proxy_password" placeholder="••••••••">
      </div>
      <button class="btn-save" type="submit">Guardar y continuar →</button>
    </form>
  </div>
</div>
</body></html>
"""

@app.route("/setup", methods=["GET", "POST"])
def setup():
    error = ""
    usuario_val = ""
    proxy_cfg = load_proxy_config()
    proxy_url_val = proxy_cfg.get("url", "")
    proxy_usuario_val = proxy_cfg.get("usuario", "")
    if request.method == "POST":
        usuario_val     = request.form.get("usuario", "").strip()
        password_val    = request.form.get("password", "")
        proxy_url_val   = request.form.get("proxy_url", "").strip()
        proxy_usuario_val = request.form.get("proxy_usuario", "").strip()
        proxy_password_val = request.form.get("proxy_password", "")
        if not usuario_val or not password_val:
            error = "Completá usuario y contraseña."
        else:
            save_rpi_credentials(usuario_val, password_val, proxy_url_val, proxy_usuario_val, proxy_password_val)
            global USUARIO, PASSWORD
            USUARIO, PASSWORD = load_rpi_credentials()
            return redirect(url_for("index"))

    error_block = f'<div class="error">{error}</div>' if error else ""
    from flask import Response as _Response
    html = (_SETUP_HTML
            .replace("{error_block}", error_block)
            .replace("{usuario}", usuario_val)
            .replace("{proxy_url}", proxy_url_val)
            .replace("{proxy_usuario}", proxy_usuario_val))
    return _Response(html, mimetype="text/html")


@app.route("/borrar-config", methods=["POST"])
def borrar_config():
    """Borra las credenciales RPI guardadas y redirige al setup."""
    delete_rpi_credentials()
    global USUARIO, PASSWORD
    USUARIO, PASSWORD = None, None
    return redirect("/setup")


# =====================================================
# PRODUCTOS
# =====================================================

@app.route("/productos")
def productos():
    html = CSS_JS + topbar("/productos") + """
    <style>
    .prod-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
        gap: 20px;
        margin-top: 8px;
    }
    .prod-card {
        background: var(--s1);
        border: 1px solid var(--b1);
        border-radius: 12px;
        overflow: hidden;
        transition: border-color .2s, box-shadow .2s;
    }
    .prod-card:hover {
        border-color: var(--amber);
        box-shadow: 0 0 24px rgba(245,158,11,.1);
    }
    .prod-card-header {
        padding: 1rem 1.4rem .8rem;
        background: var(--s2);
        border-bottom: 1px solid var(--b1);
        display: flex;
        align-items: center;
        gap: .8rem;
    }
    .prod-icon {
        font-size: 1.6rem;
        line-height: 1;
    }
    .prod-name {
        font-family: var(--mono);
        font-size: .85rem;
        font-weight: 600;
        color: var(--text);
        letter-spacing: .06em;
    }
    .prod-tag {
        font-family: var(--mono);
        font-size: .6rem;
        color: var(--amber);
        background: var(--amber-gl);
        border: 1px solid rgba(245,158,11,.25);
        border-radius: 4px;
        padding: 2px 7px;
        letter-spacing: .08em;
        margin-left: auto;
    }
    .prod-body {
        padding: 1.2rem 1.4rem;
    }
    .prod-desc {
        font-size: .82rem;
        color: var(--text2);
        line-height: 1.65;
        margin-bottom: 1.2rem;
    }
    .prod-features {
        list-style: none;
        margin-bottom: 1.4rem;
        display: flex;
        flex-direction: column;
        gap: .45rem;
    }
    .prod-features li {
        font-size: .78rem;
        color: var(--text2);
        display: flex;
        align-items: flex-start;
        gap: .5rem;
    }
    .prod-features li::before {
        content: "▸";
        color: var(--amber);
        font-size: .7rem;
        margin-top: 1px;
        flex-shrink: 0;
    }
    .prod-footer {
        display: flex;
        align-items: center;
        gap: 1rem;
        flex-wrap: wrap;
    }
    .prod-btn {
        display: inline-flex;
        align-items: center;
        gap: .4rem;
        padding: .5rem 1.1rem;
        border-radius: 7px;
        font-family: var(--mono);
        font-size: .72rem;
        font-weight: 600;
        letter-spacing: .05em;
        text-decoration: none;
        transition: opacity .15s, transform .1s;
    }
    .prod-btn:hover { opacity: .85; transform: translateY(-1px); }
    .prod-btn-primary {
        background: var(--amber);
        color: #07080f;
    }
    .prod-btn-ghost {
        background: transparent;
        color: var(--amber);
        border: 1px solid rgba(245,158,11,.35);
    }
    .prod-qr {
        border-radius: 8px;
        border: 1px solid var(--b1);
        background: #fff;
        padding: 4px;
    }
    .current-badge {
        font-family: var(--mono);
        font-size: .6rem;
        color: var(--green);
        background: rgba(34,197,94,.1);
        border: 1px solid rgba(34,197,94,.25);
        border-radius: 4px;
        padding: 2px 7px;
        letter-spacing: .08em;
    }
    </style>
    <div class="page">
        <div style="font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:2px;margin-bottom:18px;">
            // NUESTROS PRODUCTOS
        </div>
        <div class="prod-grid">

            <!-- GestorRPI -->
            <div class="prod-card">
                <div class="prod-card-header">
                    <span class="prod-icon">🏛️</span>
                    <div>
                        <div class="prod-name">GestorRPI</div>
                        <div style="font-size:.65rem;color:var(--muted);font-family:var(--mono);margin-top:2px;">Registro de la Propiedad Inmueble · Bs. As.</div>
                    </div>
                    <span class="current-badge">ACTIVO</span>
                </div>
                <div class="prod-body">
                    <p class="prod-desc">
                        Automatizá la carga de trámites en el portal del RPIBA. Ingresás los datos una sola vez y el sistema los presenta automáticamente, con seguimiento de estado y notificaciones de resultado.
                    </p>
                    <ul class="prod-features">
                        <li>Carga automática de índice de titulares, informes y copias de dominio</li>
                        <li>Seguimiento de pedidos pendientes en tiempo real</li>
                        <li>Estadísticas y exportación de trámites</li>
                        <li>Descarga automática de informes completados</li>
                    </ul>
                    <div class="prod-footer">
                        <span style="font-family:var(--mono);font-size:.7rem;color:var(--muted);">Estás usando esta app ahora</span>
                    </div>
                </div>
            </div>

            <!-- Bot Telegram -->
            <div class="prod-card">
                <div class="prod-card-header">
                    <span class="prod-icon">✈️</span>
                    <div>
                        <div class="prod-name">SalidasRPI Bot</div>
                        <div style="font-size:.65rem;color:var(--muted);font-family:var(--mono);margin-top:2px;">@SalidasRPIbot · Telegram</div>
                    </div>
                    <span class="prod-tag">TELEGRAM</span>
                </div>
                <div class="prod-body">
                    <p class="prod-desc">
                        Bot de Telegram para seguimiento de trámites del RPIBA. Cargás tus expedientes y el bot los monitorea automáticamente: te dice cómo están y te avisa en el momento en que salen.
                    </p>
                    <ul class="prod-features">
                        <li>Cargá tus trámites ingresados en mesa de entrada</li>
                        <li>Consulta de estado en cualquier momento</li>
                        <li>Notificación automática al detectar salida</li>
                        <li>Sin instalar nada — funciona directo en Telegram</li>
                    </ul>
                    <div class="prod-footer">
                        <a href="https://t.me/SalidasRPIbot" target="_blank" class="prod-btn prod-btn-primary">
                            ✈️ Abrir en Telegram
                        </a>
                        <img class="prod-qr"
                             src="https://api.qrserver.com/v1/create-qr-code/?data=https://t.me/SalidasRPIbot&size=80x80&color=07080f&bgcolor=ffffff"
                             width="80" height="80" alt="QR @SalidasRPIbot">
                    </div>
                </div>
            </div>

        </div>
    </div>
    </body></html>
    """
    return html


# =====================================================
# PUNTO DE ENTRADA
# =====================================================

def run_flask():
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)  # Silencia los GET/POST logs y el warning de dev server
    app.run(port=5001, debug=False, use_reloader=False)

if __name__ == "__main__":
    init_db()

    usuario_cfg, _ = load_rpi_credentials()

    # ── Correr Flask en hilo secundario ───────────────────────────────────────
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Abrir navegador después de 1.5s
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5001")).start()

    # ── Ícono en barra de menú (Mac) ──────────────────────────────────────────
    try:
        import rumps

        class GestorRPIApp(rumps.App):
            def __init__(self):
                super().__init__("GestorRPI", title="🏛️")
                self.menu = [
                    rumps.MenuItem("Abrir GestorRPI", callback=self.abrir),
                    None,  # separador
                    rumps.MenuItem("Cerrar GestorRPI", callback=self.cerrar),
                ]

            def abrir(self, _):
                webbrowser.open("http://localhost:5001")

            def cerrar(self, _):
                rumps.quit_application()

        GestorRPIApp().run()

    except ImportError:
        # Fallback si rumps no está disponible (ej. Windows)
        run_flask()