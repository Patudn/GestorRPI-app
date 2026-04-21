# -*- coding: utf-8 -*-
"""
firebase_auth.py - Módulo de autenticación y suscripciones
Gestor RPI — Francisco Di Nardo (PatuDN)

Maneja:
- Login / logout con Firebase Auth (REST API)
- Refresh automático de tokens
- Verificación de suscripción activa en Firestore
"""

import os
import json
import requests
import time
from datetime import datetime, timezone


def _get_proxies() -> dict | None:
    """
    Detecta proxy en este orden:
    1. config.json (configuración manual explícita)
    2. Proxy del sistema operativo (Windows lo configura IT automáticamente)
    Si hay credenciales en config.json, las inyecta en la URL del proxy del sistema.
    """
    try:
        from platformdirs import user_data_dir
        cfg_dir = user_data_dir("GestorRPI", "PatuDN")
    except ImportError:
        cfg_dir = os.path.join(os.path.expanduser("~"), ".gestorrpi")
    cfg_file = os.path.join(cfg_dir, "config.json")

    proxy_cfg = {}
    try:
        with open(cfg_file, "r") as f:
            proxy_cfg = json.load(f).get("proxy", {})
    except Exception:
        pass

    proxy_url = proxy_cfg.get("url", "").strip()

    # Si no hay URL manual, intentar detectar la del sistema
    if not proxy_url:
        try:
            import urllib.request
            sys_proxies = urllib.request.getproxies()
            proxy_url = sys_proxies.get("https") or sys_proxies.get("http", "")
        except Exception:
            pass

    if not proxy_url:
        return None

    # Inyectar credenciales si están configuradas
    usuario = proxy_cfg.get("usuario", "").strip()
    password = proxy_cfg.get("password", "").strip()
    if usuario and password:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(proxy_url)
        if not parsed.username:  # solo si la URL no trae credenciales ya
            proxy_url = urlunparse(parsed._replace(netloc=f"{usuario}:{password}@{parsed.netloc}"))

    return {"http": proxy_url, "https": proxy_url}


def _session() -> requests.Session:
    """Devuelve una Session con proxy configurado si corresponde."""
    s = requests.Session()
    proxies = _get_proxies()
    if proxies:
        s.proxies.update(proxies)
    return s

# ─── Configuración Firebase ────────────────────────────────────────────────────
FIREBASE_API_KEY    = "AIzaSyDF7W4POQmClkEuwEq8NVmmpQaqKB9495Q"
FIREBASE_PROJECT_ID = "rpi-bsas"

# ─── Cuentas del creador (bypass de suscripción) ──────────────────────────────
# Estos emails nunca son bloqueados por falta de suscripción.
# Agregar el email con el que te registraste en Firebase.
CREATOR_EMAILS = {
    "juanfdinardo@gmail.com",
}

try:
    from platformdirs import user_data_dir
    _USER_DATA_DIR = user_data_dir("GestorRPI", "PatuDN")
except ImportError:
    _USER_DATA_DIR = os.path.join(os.path.expanduser("~"), ".gestorrpi")

os.makedirs(_USER_DATA_DIR, exist_ok=True)
TOKEN_FILE = os.path.join(_USER_DATA_DIR, "session.json")

# ─── Gestión de sesión local ───────────────────────────────────────────────────

def save_session(data: dict):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)

def load_session() -> dict | None:
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None

def clear_session():
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)

# ─── Autenticación ─────────────────────────────────────────────────────────────

def login_with_email_password(email: str, password: str) -> tuple[bool, str | dict]:
    """
    Autentica con Firebase.
    Retorna (True, session_dict) o (False, mensaje_error)
    """
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
    try:
        resp = _session().post(url, json={
            "email": email,
            "password": password,
            "returnSecureToken": True
        }, timeout=10)
        data = resp.json()
    except Exception as e:
        return False, f"Error de conexión: {e}"

    if "idToken" in data:
        session = {
            "idToken":       data["idToken"],
            "refreshToken":  data["refreshToken"],
            "email":         data["email"],
            "localId":       data["localId"],
            "expiresAt":     time.time() + int(data.get("expiresIn", 3600))
        }
        save_session(session)
        return True, session

    msg_map = {
        "EMAIL_NOT_FOUND":       "Email no registrado.",
        "INVALID_PASSWORD":      "Contraseña incorrecta.",
        "USER_DISABLED":         "Cuenta deshabilitada.",
        "INVALID_LOGIN_CREDENTIALS": "Email o contraseña incorrectos.",
        "TOO_MANY_ATTEMPTS_TRY_LATER": "Demasiados intentos. Intentá más tarde.",
    }
    raw = data.get("error", {}).get("message", "Error desconocido")
    return False, msg_map.get(raw, raw)


def _refresh_id_token(refresh_token: str) -> str | None:
    url = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"
    try:
        resp = _session().post(url, json={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token
        }, timeout=10)
        data = resp.json()
        return data.get("id_token")
    except Exception:
        return None


def get_valid_token() -> tuple[str | None, dict | None]:
    """
    Devuelve (id_token, session) con token renovado si hace falta.
    Si no hay sesión o no se puede renovar, devuelve (None, None).
    """
    session = load_session()
    if not session:
        return None, None

    # Renovar si vence en menos de 5 minutos
    if time.time() > session.get("expiresAt", 0) - 300:
        new_token = _refresh_id_token(session["refreshToken"])
        if new_token:
            session["idToken"]    = new_token
            session["expiresAt"]  = time.time() + 3600
            save_session(session)
        else:
            clear_session()
            return None, None

    return session["idToken"], session

# ─── Suscripción ───────────────────────────────────────────────────────────────

def check_subscription(id_token: str, user_id: str) -> bool:
    """
    Verifica en Firestore si el usuario tiene suscripción activa y vigente.
    Cuentas en CREATOR_EMAILS tienen acceso permanente sin suscripción.
    """
    # Bypass para cuentas del creador
    session = load_session()
    if session and session.get("email", "").lower() in {e.lower() for e in CREATOR_EMAILS}:
        return True

    url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/subscriptions/{user_id}"
    )
    try:
        resp = _session().get(url, headers={"Authorization": f"Bearer {id_token}"}, timeout=10)
    except Exception:
        return False

    if resp.status_code != 200:
        return False

    fields = resp.json().get("fields", {})
    active = fields.get("active", {}).get("booleanValue", False)
    if not active:
        return False

    expires_raw = fields.get("expires_at", {}).get("timestampValue", "")
    if expires_raw:
        try:
            exp = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            return exp > datetime.now(timezone.utc)
        except Exception:
            return False

    return True  # Si no tiene fecha de vencimiento, se considera activa


def get_subscription_info(id_token: str, user_id: str) -> dict:
    """
    Devuelve info completa de la suscripción para mostrar en el panel.
    """
    url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/subscriptions/{user_id}"
    )
    try:
        resp = _session().get(url, headers={"Authorization": f"Bearer {id_token}"}, timeout=10)
    except Exception:
        return {"active": False, "plan": "-", "expires": "-"}

    if resp.status_code != 200:
        return {"active": False, "plan": "-", "expires": "-"}

    fields = resp.json().get("fields", {})
    active  = fields.get("active", {}).get("booleanValue", False)
    plan    = fields.get("plan", {}).get("stringValue", "mensual")
    expires = fields.get("expires_at", {}).get("timestampValue", "")

    expires_str = "-"
    if expires:
        try:
            exp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            expires_str = exp.strftime("%d/%m/%Y")
        except Exception:
            pass

    return {"active": active, "plan": plan, "expires": expires_str}
