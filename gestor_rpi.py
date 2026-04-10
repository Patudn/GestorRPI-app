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

for path in [USER_DATA_DIR, DOWNLOAD_PATH, ERROR_PATH]:
    os.makedirs(path, exist_ok=True)

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

def save_rpi_credentials(usuario: str, password: str):
    """Guarda credenciales del RPI en config.json."""
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump({"usuario": usuario, "password": password}, f)

def delete_rpi_credentials():
    """Borra config.json (permite reconfigurar o desinstalar)."""
    if os.path.exists(CONFIG_FILE):
        os.remove(CONFIG_FILE)

USUARIO, PASSWORD = load_rpi_credentials()

SOLICITANTES_BASE = ["MILOVAN", "CONGRESO", "MARTIN GARCIA"]

# Estado global del proceso Playwright
estado_proceso = {
    "corriendo": False,
    "log": [],
    "progreso": 0,
    "total": 0,
    "fase": ""
}

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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def db_to_dict(row):
    return dict(row)

def obtener_solicitantes():
    base = set(SOLICITANTES_BASE)
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT DISTINCT SOLICITANTE FROM tramites WHERE SOLICITANTE != ''").fetchall()
            for r in rows:
                if r[0]: base.add(r[0].upper().strip())
    except:
        pass
    return sorted(base)

def log_proceso(msg):
    estado_proceso["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
    print(msg)

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
<title>Gestor RPI</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0f0f11; --surface: #17171a; --surface2: #1e1e23; --surface3: #25252c;
  --border: #2a2a35; --border2: #35353f;
  --accent: #e8c84a; --accent2: #4a9ee8; --accent3: #4ae89a;
  --danger: #e84a4a; --warn: #e8a44a;
  --text: #e8e8f0; --text2: #a0a0b8; --muted: #5a5a72;
  --mono: "DM Mono", monospace; --sans: "DM Sans", sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--sans); background: var(--bg); color: var(--text); font-size: 13px; min-height: 100vh; }
.topbar {
  display: flex; align-items: center;
  height: 52px; padding: 0 24px;
  background: var(--surface); border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 100;
}
.brand {
  font-family: var(--mono); font-size: 13px; font-weight: 500;
  color: var(--accent); letter-spacing: 2px; margin-right: 24px;
  padding-right: 24px; border-right: 1px solid var(--border);
  white-space: nowrap;
}
.topbar a {
  color: var(--muted); text-decoration: none;
  padding: 0 14px; height: 52px; display: flex; align-items: center;
  font-size: 12px; font-family: var(--mono); letter-spacing: 0.5px;
  border-bottom: 2px solid transparent; transition: all 0.15s;
}
.topbar a:hover { color: var(--text); background: var(--surface2); }
.topbar a.active { color: var(--accent); border-bottom-color: var(--accent); }
.page { max-width: 980px; margin: 24px auto; padding: 0 20px; }
.card { background: var(--surface); border: 1px solid var(--border); margin-bottom: 16px; }
.card-header {
  padding: 12px 18px; font-family: var(--mono); font-size: 11px;
  letter-spacing: 1.5px; color: var(--text2); text-transform: uppercase;
  border-bottom: 1px solid var(--border); background: var(--surface2);
  display: flex; align-items: center; gap: 8px;
}
.card-header::before { content: "//"; color: var(--accent); }
.card-body { padding: 20px 22px; }
.row { display: flex; align-items: center; margin-bottom: 10px; }
.row.top { align-items: flex-start; }
.lbl { width: 220px; min-width: 220px; text-align: right; margin-right: 16px; color: var(--text2); padding-top: 4px; font-size: 12px; }
.req { color: var(--accent); margin-left: 2px; }
input[type=text], input[type=number], select {
  padding: 7px 10px; border: 1px solid var(--border2);
  background: var(--surface2); color: var(--text);
  font-size: 13px; font-family: var(--sans); outline: none;
  transition: border-color 0.15s;
}
input[type=text]::placeholder { color: var(--muted); }
input:focus, select:focus { border-color: var(--accent2); box-shadow: 0 0 0 2px rgba(74,158,232,0.1); }
input.valid { border-color: var(--accent3) !important; }
input.invalid { border-color: var(--danger) !important; }
select option { background: var(--surface2); color: var(--text); }
.vmsg { font-size: 11px; margin-left: 8px; font-family: var(--mono); }
.vmsg.ok { color: var(--accent3); }
.vmsg.err { color: var(--danger); }
.nom { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; }
.nom input { width: 38px; text-align: center; padding: 6px 4px; }
.nom .nl { font-family: var(--mono); font-size: 10px; color: var(--accent); font-weight: 500; }
.nom .ns { color: var(--muted); }
.titular-row { display: flex; gap: 6px; margin-bottom: 6px; align-items: center; margin-left: 236px; }
.titular-row input { flex: 1; }
.btn-rm { background: rgba(232,74,74,0.1); border: 1px solid var(--danger); color: var(--danger); cursor: pointer; padding: 5px 10px; font-size: 12px; transition: all 0.15s; }
.btn-rm:hover { background: var(--danger); color: white; }
.btn-add-t { background: var(--surface2); border: 1px solid var(--border2); color: var(--text2); cursor: pointer; padding: 6px 14px; font-size: 12px; margin-left: 236px; margin-top: 6px; font-family: var(--mono); transition: all 0.15s; }
.btn-add-t:hover { border-color: var(--accent2); color: var(--accent2); }
.btn { padding: 8px 18px; border: 1px solid; cursor: pointer; font-size: 12px; font-family: var(--mono); letter-spacing: 0.5px; transition: all 0.15s; text-decoration: none; display: inline-block; }
.btn-primary { background: var(--accent2); border-color: var(--accent2); color: white; }
.btn-primary:hover { background: #3a8ed8; }
.btn-success { background: var(--accent3); border-color: var(--accent3); color: #000; }
.btn-danger { background: transparent; border-color: var(--danger); color: var(--danger); }
.btn-danger:hover { background: var(--danger); color: white; }
.btn-secondary { background: var(--surface2); border-color: var(--border2); color: var(--text2); }
.btn-secondary:hover { border-color: var(--text); color: var(--text); }
.btn-save {
  width: 100%; margin-top: 18px; padding: 12px;
  font-size: 12px; font-family: var(--mono); letter-spacing: 2px;
  background: var(--accent); border: none; color: #000;
  cursor: pointer; font-weight: 500; text-transform: uppercase;
  transition: background 0.15s;
}
.btn-save:hover { background: #f0d85a; }
.alert { padding: 10px 14px; margin-bottom: 14px; font-size: 12px; border-left: 3px solid; font-family: var(--mono); }
.alert-ok { background: rgba(74,232,154,0.07); border-color: var(--accent3); color: var(--accent3); }
.alert-err { background: rgba(232,74,74,0.07); border-color: var(--danger); color: var(--danger); }
.alert-warn { background: rgba(232,164,74,0.07); border-color: var(--warn); color: var(--warn); }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { background: var(--surface2); color: var(--muted); padding: 8px 10px; text-align: left; font-size: 10px; font-family: var(--mono); letter-spacing: 1px; text-transform: uppercase; border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 7px 10px; border-bottom: 1px solid var(--border); }
tr:hover td { background: var(--surface2); }
.badge { display: inline-block; padding: 2px 8px; font-size: 10px; font-family: var(--mono); border: 1px solid; white-space: nowrap; }
.b-755 { background: rgba(74,158,232,0.1); color: var(--accent2); border-color: rgba(74,158,232,0.25); }
.b-752 { background: rgba(232,200,74,0.1); color: var(--accent); border-color: rgba(232,200,74,0.25); }
.b-754 { background: rgba(180,74,232,0.1); color: #c07ef0; border-color: rgba(180,74,232,0.25); }
.b-753ph { background: rgba(74,232,154,0.1); color: var(--accent3); border-color: rgba(74,232,154,0.25); }
.b-pend { background: rgba(232,164,74,0.1); color: var(--warn); border-color: rgba(232,164,74,0.25); }
.b-carg { background: rgba(74,158,232,0.1); color: var(--accent2); border-color: rgba(74,158,232,0.25); }
.b-comp { background: rgba(74,232,154,0.1); color: var(--accent3); border-color: rgba(74,232,154,0.25); }
.stats { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }
.stat-card { background: var(--surface); border: 1px solid var(--border); padding: 16px 20px; flex: 1; min-width: 110px; text-align: center; }
.stat-card .num { font-size: 30px; font-family: var(--mono); color: var(--accent); line-height: 1; }
.stat-card .lab { font-size: 10px; color: var(--muted); margin-top: 5px; font-family: var(--mono); letter-spacing: 0.5px; text-transform: uppercase; }
.menu-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 8px; }
.menu-card {
  border: 1px solid var(--border); background: var(--surface);
  padding: 22px 20px; cursor: pointer; text-align: left; width: 100%;
  transition: all 0.15s; position: relative; color: var(--text);
}
.menu-card:hover { border-color: var(--accent); background: var(--surface2); }
.menu-card:disabled { opacity: 0.3; cursor: not-allowed; pointer-events: none; }
.menu-card .num { font-family: var(--mono); font-size: 10px; color: var(--accent); letter-spacing: 2px; margin-bottom: 10px; text-transform: uppercase; }
.menu-card .title { font-size: 15px; font-weight: 500; margin-bottom: 5px; }
.menu-card .desc { font-size: 12px; color: var(--text2); line-height: 1.5; }
.menu-card .icon { position: absolute; top: 18px; right: 18px; font-size: 24px; opacity: 0.4; }
.console {
  background: #0a0a0e; color: #b8b8d0; font-family: var(--mono);
  font-size: 12px; padding: 16px; height: 340px;
  overflow-y: auto; border: 1px solid var(--border); line-height: 1.7;
}
.console::-webkit-scrollbar { width: 3px; }
.console::-webkit-scrollbar-thumb { background: var(--border2); }
.line-ok { color: var(--accent3); }
.line-err { color: var(--danger); }
.line-warn { color: var(--warn); }
.line-info { color: var(--accent2); }
.progress-bar { height: 2px; background: var(--border); margin: 10px 0; }
.progress-fill { height: 100%; background: var(--accent); transition: width 0.4s ease; }
.prog-label { font-family: var(--mono); font-size: 11px; color: var(--muted); margin-bottom: 8px; }
hr.sep { border: none; border-top: 1px solid var(--border); margin: 16px 0; }
.sec-label { font-family: var(--mono); font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 10px; margin-left: 236px; }
::-webkit-scrollbar { width: 5px; } ::-webkit-scrollbar-thumb { background: var(--border2); }

/* TOPBAR DROPDOWN */
.tb-dropdown { position: relative; height: 52px; display: flex; align-items: center; }
.tb-drop-btn {
  background: none; border: none; color: var(--muted);
  padding: 0 14px; height: 52px; font-size: 12px;
  font-family: var(--mono); letter-spacing: 0.5px;
  cursor: pointer; border-bottom: 2px solid transparent;
  transition: all 0.15s; display: flex; align-items: center; gap: 4px;
}
.tb-drop-btn:hover, .tb-dropdown:hover .tb-drop-btn { color: var(--text); background: var(--surface2); }
.tb-drop-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
.tb-drop-menu {
  display: none; position: absolute; top: 52px; left: 0;
  background: var(--surface2); border: 1px solid var(--border);
  border-top: 2px solid var(--accent); min-width: 260px;
  z-index: 200; box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}
.tb-dropdown:hover .tb-drop-menu { display: block; }
.tb-drop-menu a {
  display: block; padding: 11px 16px;
  color: var(--text2); text-decoration: none;
  font-size: 12px; font-family: var(--mono);
  border-bottom: 1px solid var(--border);
  transition: all 0.1s; height: auto; border-bottom-color: var(--border);
}
.tb-drop-menu a:last-child { border-bottom: none; }
.tb-drop-menu a:hover { background: var(--surface); color: var(--accent); padding-left: 20px; }
.tb-drop-menu a.active { color: var(--accent); background: rgba(232,200,74,0.06); }
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
</script>
"""


def topbar(activo=""):
    cargar_activo = activo in ("/form755", "/form752", "/form754", "/form753ph")
    cargar_cls = "active" if cargar_activo else ""

    html = '''<div class="topbar">
  <span class="brand">RPI</span>
  <a href="/" class="''' + ("active" if activo=="/" else "") + '''">INICIO</a>
  <div class="tb-dropdown">
    <button class="tb-drop-btn ''' + cargar_cls + '''">CARGAR ▾</button>
    <div class="tb-drop-menu">
      <a href="/form755" class="''' + ("active" if activo=="/form755" else "") + '''">755 — Índice de Titulares</a>
      <a href="/form752" class="''' + ("active" if activo=="/form752" else "") + '''">752 — Informe de Dominio FR</a>
      <a href="/form754" class="''' + ("active" if activo=="/form754" else "") + '''">754 — Copia de Dominio FR</a>
      <a href="/form753ph" class="''' + ("active" if activo=="/form753ph" else "") + '''">753 PH — Inhibición Persona Humana</a>
    </div>
  </div>
  <a href="/pendientes" class="''' + ("active" if activo=="/pendientes" else "") + '''">PEDIDOS</a>
  <div style="margin-left:auto;display:flex;align-items:center;gap:12px;">
    <form method="POST" action="/borrar-config" style="margin:0;"
          onsubmit="return confirm('¿Borrar credenciales RPI? Tendrás que volver a configurarlas.')">
      <button type="submit" style="background:none;border:none;color:var(--muted);
              font-size:11px;font-family:var(--mono);letter-spacing:0.5px;
              cursor:pointer;padding:0 14px;height:52px;transition:color .15s;"
              onmouseover="this.style.color='var(--danger)'"
              onmouseout="this.style.color='var(--muted)'">
        BORRAR CONFIG
      </button>
    </form>
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
                    orden, tipo, ape.upper().strip(), nom.upper().strip(),
                    dni_raw, cuit_raw,
                    f.get("partido","").strip(), f.get("matricula","").strip(), f.get("uf","").strip(),
                    f.get("c","").strip(), f.get("s","").strip(),
                    f.get("ch","").strip(), f.get("ch2","").strip(),
                    f.get("qta","").strip(), f.get("qta2","").strip(),
                    f.get("f","").strip(), f.get("f2","").strip(),
                    f.get("m","").strip(), f.get("m2","").strip(),
                    f.get("p","").strip(), f.get("p2","").strip(),
                    f.get("sp","").strip(),
                    f.get("solicitante","").upper().strip(), "PENDIENTE"
                ))
            else:
                conn.execute("""
                    INSERT INTO tramites (ORDEN, TIPO_SOLICITUD, APELLIDO, NOMBRE, ESTADO)
                    VALUES (?,?,?,?,?)
                """, (orden, tipo, ape.upper().strip(), nom.upper().strip(), "PENDIENTE"))
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
            <div style="background:var(--surface);border:1px solid var(--border);padding:32px;max-width:440px;width:90%;">
                <div style="font-family:var(--mono);font-size:10px;color:var(--accent);letter-spacing:2px;margin-bottom:16px;">// MODO DE EJECUCIÓN</div>
                <div style="font-size:15px;font-weight:500;margin-bottom:8px;">¿Cómo querés ejecutar la carga?</div>
                <div style="font-size:13px;color:var(--text2);margin-bottom:24px;line-height:1.6;">
                    En <strong style="color:var(--text)">modo automático</strong> el navegador corre en segundo plano.<br>
                    En <strong style="color:var(--text)">modo manual</strong> podés ver y controlar cada paso.
                </div>
                <input type="hidden" id="modal-accion">
                <div style="display:flex;gap:10px;">
                    <button class="btn btn-primary" style="flex:1;padding:12px;" onclick="confirmarModo(false)">
                        ⚡ Automático<br><span style="font-size:11px;font-weight:400;opacity:0.8">Navegador en segundo plano</span>
                    </button>
                    <button class="btn btn-secondary" style="flex:1;padding:12px;" onclick="confirmarModo(true)">
                        👁 Manual<br><span style="font-size:11px;font-weight:400;opacity:0.8">Ver el navegador en acción</span>
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
        if (accion === 'cargar_base') {{
            window.location.href = '/form755';
            return;
        }}
        accionActual = accion;
        document.getElementById('panel-descarga').style.display = 'none';

        // Para carga: preguntar auto o manual → define si el navegador es visible
        if (accion === 'solicitar' || accion === 'solicitar_descargar') {{
            mostrarModalModo(accion);
        }} else {{
            // Descarga: siempre en segundo plano, sin preguntar
            lanzarAccion(accion, '', '', false);
        }}
    }}

    function mostrarModalModo(accion) {{
        document.getElementById('modal-accion').value = accion;
        document.getElementById('modal-modo').style.display = 'flex';
    }}

    function confirmarModo(visible) {{
        const accion = document.getElementById('modal-accion').value;
        document.getElementById('modal-modo').style.display = 'none';
        lanzarAccion(accion, '', '', visible);
    }}

    function abrirDescarga() {{
        accionActual = 'descargar';
        document.getElementById('panel-descarga').style.display = 'block';
        document.getElementById('panel-log').style.display = 'none';
        // Sugerir fecha de hace 15 días
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
        lanzarAccion('descargar', desde, hasta, false);  // descarga siempre en segundo plano
    }}

    function lanzarAccion(accion, desde, hasta, modoVisible=false) {{
        document.getElementById('panel-log').style.display = 'block';
        document.getElementById('log-header').textContent = 'Proceso en curso...';
        document.getElementById('console-log').innerHTML = '';
        document.getElementById('prog-fill').style.width = '0%';
        document.getElementById('prog-label').textContent = 'Iniciando...';
        document.getElementById('btn-cerrar-log').style.display = 'none';

        fetch('/iniciar_proceso', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{accion, desde, hasta, modo_visible: modoVisible}})
        }}).then(r => r.json()).then(d => {{
            if (d.ok) {{
                polling = setInterval(actualizarLog, 1000);
            }} else {{
                agregarLinea('❌ ' + d.error, 'err');
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

            if (!d.corriendo) {{
                clearInterval(polling);
                document.getElementById('log-header').textContent = '✅ Proceso finalizado';
                document.getElementById('btn-cerrar-log').style.display = 'inline-block';
                document.getElementById('prog-fill').style.width = '100%';
            }}
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

    lista_s = obtener_solicitantes()
    opts = "".join(f'<option value="{s}">{s}</option>' for s in lista_s)

    html = CSS_JS + topbar("/form755") + f"""
    <div class="page">
        <div class="card">
            <div class="card-header"><div class="hbar"></div>755 — Consulta al Índice de Titulares</div>
            <div class="card-body">
                {msg}
                <form method="POST" onsubmit="return chkForm755()">
                    <div class="row"><div class="lbl">N° de Orden <span class="req">*</span></div>
                        <input type="text" name="orden" required style="width:90px" autofocus></div>
                    <div class="row"><div class="lbl">Solicitante <span class="req">*</span></div>
                        <select name="solicitante" style="width:200px">{opts}</select></div>
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

    lista_s = obtener_solicitantes()
    opts = "".join(f'<option value="{s}">{s}</option>' for s in lista_s)

    html = CSS_JS + topbar("/form752") + f"""
    <div class="page">
        <div class="card">
            <div class="card-header"><div class="hbar"></div>752 — Informe de Dominio Inmueble Matriculado (Folio Real)</div>
            <div class="card-body">
                {msg}
                <form method="POST">
                    <div class="row"><div class="lbl">N° de Orden <span class="req">*</span></div>
                        <input type="text" name="orden" required style="width:90px" autofocus></div>
                    <div class="row"><div class="lbl">Solicitante <span class="req">*</span></div>
                        <select name="solicitante" style="width:200px">{opts}</select></div>
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

    lista_s = obtener_solicitantes()
    opts = "".join(f'<option value="{s}">{s}</option>' for s in lista_s)

    html = CSS_JS + topbar("/form754") + f"""
    <div class="page">
        <div class="card">
            <div class="card-header"><div class="hbar"></div>754 — Copia de Dominio Inmueble Matriculado (Folio Real)</div>
            <div class="card-body">
                {msg}
                <form method="POST" onsubmit="return chkForm754()">
                    <div class="row"><div class="lbl">N° de Orden <span class="req">*</span></div>
                        <input type="text" name="orden" required style="width:90px" autofocus></div>
                    <div class="row"><div class="lbl">Solicitante <span class="req">*</span></div>
                        <select name="solicitante" style="width:200px">{opts}</select></div>
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

    lista_s = obtener_solicitantes()
    opts = "".join(f'<option value="{s}">{s}</option>' for s in lista_s)

    html = CSS_JS + topbar("/form753ph") + f"""
    <div class="page">
        <div class="card">
            <div class="card-header"><div class="hbar"></div>753 PH — Inhibición Persona Humana</div>
            <div class="card-body">
                {msg}
                <form method="POST">
                    <div class="row"><div class="lbl">N° de Orden <span class="req">*</span></div>
                        <input type="text" name="orden" required style="width:90px" autofocus></div>
                    <div class="row"><div class="lbl">Solicitante <span class="req">*</span></div>
                        <select name="solicitante" style="width:200px">{opts}</select></div>
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
                       FECHA_CARGA, NRO_TRAMITE
                FROM tramites ORDER BY CAST(ORDEN AS INTEGER) DESC, id DESC
            """).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM tramites").fetchone()[0]
            pend = conn.execute("SELECT COUNT(DISTINCT ORDEN) FROM tramites WHERE ESTADO='PENDIENTE'").fetchone()[0]
            carg = conn.execute("SELECT COUNT(DISTINCT ORDEN) FROM tramites WHERE ESTADO='CARGADO'").fetchone()[0]
            comp = conn.execute("SELECT COUNT(DISTINCT ORDEN) FROM tramites WHERE ESTADO='COMPLETADO'").fetchone()[0]
    except:
        rows, total, pend, carg, comp = [], 0, 0, 0, 0

    def badge_tipo(t):
        t = str(t).lower().replace(" ","").replace("ph","ph")
        return f'<span class="badge b-{t}">{t.upper()}</span>'

    def badge_estado(e):
        m = {"PENDIENTE": "b-pend", "CARGADO": "b-carg", "COMPLETADO": "b-comp"}
        return f'<span class="badge {m.get(e,"")}">{"" if e=="PENDIENTE" else "✅ " if e=="COMPLETADO" else "📤 "}{e}</span>'

    filas_html = ""
    for r in rows:
        r = dict(r)
        tipo = str(r.get('TIPO_SOLICITUD','')).strip()
        dni_val = r.get('DNI','') or ''
        cuit_val = r.get('CUIT','') or ''
        partido_val = r.get('PARTIDO','') or ''
        mat_val = r.get('NRO_INSCRIPCION','') or ''

        # Para 752/754: mostrar PTD+MAT en lugar de DNI/CUIT vacíos
        if tipo in ('752','754') and not dni_val and not cuit_val:
            doc_col = f'<span style="color:var(--muted);font-family:var(--mono);font-size:11px">PTD {partido_val} MAT {mat_val}</span>' if partido_val or mat_val else ""
            partido_col = ""
            mat_col = ""
        else:
            doc_col = dni_val or cuit_val
            partido_col = partido_val
            mat_col = mat_val

        filas_html += f"""<tr>
            <td><strong>{r.get('ORDEN','')}</strong></td>
            <td>{badge_tipo(r.get('TIPO_SOLICITUD',''))}</td>
            <td>{r.get('APELLIDO','')}</td>
            <td>{r.get('NOMBRE','')}</td>
            <td>{doc_col}</td>
            <td>{partido_col}</td>
            <td>{mat_col}</td>
            <td>{r.get('SOLICITANTE','')}</td>
            <td>{badge_estado(r.get('ESTADO',''))}</td>
            <td>{r.get('FECHA_CARGA','')}</td>
            <td>{r.get('NRO_TRAMITE','')}</td>
        </tr>"""

    html = CSS_JS + topbar("/pendientes") + f"""
    <div class="page">
        <div class="stats">
            <div class="stat-card"><div class="num">{total}</div><div class="lab">Total registros</div></div>
            <div class="stat-card"><div class="num" style="color:#806000">{pend}</div><div class="lab">⏳ Pendientes</div></div>
            <div class="stat-card"><div class="num" style="color:#206020">{carg}</div><div class="lab">📤 Cargados</div></div>
            <div class="stat-card"><div class="num" style="color:#202080">{comp}</div><div class="lab">✅ Completados</div></div>
        </div>
        <div class="card">
            <div class="card-header"><div class="hbar"></div>Todos los pedidos</div>
            <div style="overflow-x:auto">
            <table>
                <thead><tr>
                    <th>ORDEN</th><th>TIPO</th><th>APELLIDO</th><th>NOMBRE</th>
                    <th>DOC / INMUEBLE</th><th>PTD</th><th>MAT</th>
                    <th>SOLICITANTE</th><th>ESTADO</th><th>F.CARGA</th><th>NRO TRÁMITE</th>
                </tr></thead>
                <tbody>{filas_html if filas_html else '<tr><td colspan="12" style="text-align:center;padding:30px;color:#999">No hay datos todavía</td></tr>'}</tbody>
            </table>
            </div>
        </div>
    </div>"""
    return html


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
    desde = data.get("desde", "")
    hasta = data.get("hasta", "")

    modo_visible = data.get("modo_visible", False)
    estado_proceso = {"corriendo": True, "log": [], "progreso": 0, "total": 0, "fase": "Iniciando..."}

    def run():
        # headless=True  → navegador invisible (automático)
        # headless=False → navegador visible  (manual, para supervisar)
        asyncio.run(proceso_playwright(accion, desde, hasta, headless=not modo_visible))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/estado_proceso")
def get_estado():
    return jsonify(estado_proceso)


# =====================================================
# PROCESO PLAYWRIGHT (async)
# =====================================================

async def proceso_playwright(accion, f_desde="", f_hasta="", headless=True):
    global estado_proceso
    modo = "segundo plano" if headless else "visible"
    log_proceso(f"🖥️  Modo navegador: {modo}")
    try:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=os.path.join(BASE_DIR, ".browser_data"),
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
                await ejecutar_carga_pw(page)

            if accion in ("solicitar_descargar", "descargar"):
                if not f_desde:
                    f_desde = await sugerir_fecha_pw(page)
                if not f_hasta:
                    f_hasta = datetime.now().strftime("%d/%m/%Y")
                await ejecutar_descarga_pw(page, f_desde, f_hasta)

            await context.close()
    except Exception as e:
        log_proceso(f"❌ Error crítico: {e}")
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


async def ejecutar_carga_pw(page):
    log_proceso("🚀 Iniciando carga de trámites...")
    estado_proceso["fase"] = "Cargando trámites"

    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM tramites WHERE ESTADO='PENDIENTE'
            ORDER BY CAST(ORDEN AS INTEGER), id
        """).fetchall()

    if not rows:
        log_proceso("☕ No hay trámites pendientes para cargar.")
        return

    # Agrupar por ORDEN
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

            # Enviar formulario
            await enviar_formulario(page)

            # Capturar número de trámite
            nro = await capturar_nro(page)
            if nro:
                with get_db() as conn:
                    conn.execute("""
                        UPDATE tramites SET ESTADO='CARGADO', NRO_TRAMITE=?, FECHA_CARGA=?
                        WHERE ORDEN=?
                    """, (nro, datetime.now().strftime("%d/%m/%Y"), orden))
                    conn.commit()
                log_proceso(f"✅ Orden {orden} → Trámite {nro}")
            else:
                log_proceso(f"⚠️ Orden {orden}: número de trámite no capturado automáticamente")

            estado_proceso["progreso"] += 1

            # Cerrar si hay botón
            try:
                btn = page.locator('[name="Cerrar"]')
                if await btn.is_visible(timeout=2000):
                    await btn.click()
            except:
                pass

        except Exception as e:
            log_proceso(f"❌ Error en Orden {orden}: {e}")
            await page.screenshot(path=os.path.join(ERROR_PATH, f"error_orden_{orden}.png"))

    log_proceso(f"\n✅ Carga finalizada. {estado_proceso['progreso']} / {estado_proceso['total']} órdenes procesadas.")


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


async def enviar_formulario(page):
    try:
        btn = page.locator("input[type='submit'][value*='Enviar'], input[type='submit'][value*='Continuar']").first
        await btn.click()
    except:
        try: await page.locator("form").first.evaluate("f => f.submit()")
        except: pass
    try:
        page.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await asyncio.sleep(2)
    except: pass
    try:
        btn_h = page.locator('#aceptar')
        if await btn_h.is_visible(timeout=3000): await btn_h.click()
    except: pass


async def capturar_nro(page):
    await asyncio.sleep(4)
    elementos = await page.locator("b").all()
    for el in elementos:
        t = (await el.inner_text()).strip()
        if t.isdigit() and len(t) >= 5:
            return t
    return None


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
        with get_db() as conn:
            conn.execute("""
                UPDATE tramites SET ESTADO='COMPLETADO'
                WHERE NRO_TRAMITE=? OR ORDEN IN (
                    SELECT ORDEN FROM tramites WHERE NRO_TRAMITE=?
                )
            """, (nro_web, nro_web))
            conn.commit()

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
    if request.method == "POST":
        usuario_val = request.form.get("usuario", "").strip()
        password_val = request.form.get("password", "")
        if not usuario_val or not password_val:
            error = "Completá usuario y contraseña."
        else:
            save_rpi_credentials(usuario_val, password_val)
            # Recargar en memoria
            global USUARIO, PASSWORD
            USUARIO, PASSWORD = load_rpi_credentials()
            return redirect(url_for("index"))

    error_block = f'<div class="error">{error}</div>' if error else ""
    from flask import Response as _Response
    html = _SETUP_HTML.replace("{error_block}", error_block).replace("{usuario}", usuario_val)
    return _Response(html, mimetype="text/html")


@app.route("/borrar-config", methods=["POST"])
def borrar_config():
    """Borra las credenciales RPI guardadas y redirige al setup."""
    delete_rpi_credentials()
    global USUARIO, PASSWORD
    USUARIO, PASSWORD = None, None
    return redirect("/setup")


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
    print("=" * 55)
    print("  GESTOR RPI v3.0 — Francisco Di Nardo (PatuDN)")
    print("=" * 55)
    print(f"  Datos:    {USER_DATA_DIR}")
    print(f"  Informes: {INFORMES_DIR}")
    print("  Servidor: http://localhost:5001")
    print("  Cerrá esta ventana para detener el sistema.")
    print("=" * 55)

    usuario_cfg, _ = load_rpi_credentials()
    if not usuario_cfg:
        print("  ⚠️  Primera vez — abriendo configuración inicial...")
    else:
        print(f"  ✅ Usuario RPI: {usuario_cfg}")

    # Abrir navegador después de 1.5s
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5001")).start()

    # Correr Flask (bloquea hasta Ctrl+C)
    run_flask()