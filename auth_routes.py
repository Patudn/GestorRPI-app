# -*- coding: utf-8 -*-
"""
auth_routes.py - Rutas Flask para autenticación y suscripción
Gestor RPI — Francisco Di Nardo (PatuDN)

Rutas que agrega este módulo:
  GET/POST  /login        → pantalla de login
  GET       /logout       → cierra sesión
  GET       /suscripcion  → panel de suscripción y pago
"""

from flask import Blueprint, render_template_string, request, redirect, url_for, session, Response
from firebase_auth import (
    login_with_email_password,
    clear_session,
    get_subscription_info,
)

auth_bp = Blueprint("auth", __name__)

# ─── Paleta de estilos (igual al resto de la app) ─────────────────────────────
_BASE_CSS = """
<!DOCTYPE html><html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Gestor RPI — {title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{{
  --bg:#0f0f11;--surface:#17171a;--surface2:#1e1e23;--surface3:#25252c;
  --border:#2a2a35;--border2:#35353f;
  --accent:#e8c84a;--accent2:#4a9ee8;--accent3:#4ae89a;
  --danger:#e84a4a;--warn:#e8a44a;
  --text:#e8e8f0;--text2:#a0a0b8;--muted:#5a5a72;
  --mono:"DM Mono",monospace;--sans:"DM Sans",sans-serif;
}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:var(--sans);background:var(--bg);color:var(--text);
      min-height:100vh;display:flex;align-items:center;justify-content:center;}}
.card{{background:var(--surface);border:1px solid var(--border);width:100%;max-width:420px;}}
.card-header{{padding:16px 22px;font-family:var(--mono);font-size:11px;
              letter-spacing:1.5px;color:var(--text2);text-transform:uppercase;
              border-bottom:1px solid var(--border);background:var(--surface2);
              display:flex;align-items:center;gap:8px;}}
.card-header::before{{content:"//";color:var(--accent);}}
.card-body{{padding:28px 28px 24px;}}
.brand{{font-family:var(--mono);font-size:22px;font-weight:500;color:var(--accent);
        letter-spacing:3px;text-align:center;margin-bottom:6px;}}
.subtitle{{font-size:12px;color:var(--muted);text-align:center;margin-bottom:28px;font-family:var(--mono);}}
.field{{margin-bottom:16px;}}
.field label{{display:block;font-size:11px;font-family:var(--mono);color:var(--text2);
              letter-spacing:1px;margin-bottom:6px;text-transform:uppercase;}}
.field input{{width:100%;padding:10px 12px;border:1px solid var(--border2);
              background:var(--surface2);color:var(--text);font-size:14px;
              font-family:var(--sans);outline:none;transition:border-color .15s;}}
.field input:focus{{border-color:var(--accent2);box-shadow:0 0 0 2px rgba(74,158,232,.1);}}
.btn-login{{width:100%;padding:12px;background:var(--accent2);border:none;
            color:white;font-size:13px;font-family:var(--mono);letter-spacing:1px;
            cursor:pointer;transition:background .15s;margin-top:8px;}}
.btn-login:hover{{background:#3a8ed8;}}
.error{{background:rgba(232,74,74,.1);border:1px solid var(--danger);
        color:var(--danger);padding:10px 14px;font-size:12px;
        font-family:var(--mono);margin-bottom:16px;}}
.info{{background:rgba(74,158,232,.1);border:1px solid var(--accent2);
       color:var(--accent2);padding:10px 14px;font-size:12px;
       font-family:var(--mono);margin-bottom:16px;}}
.ok{{background:rgba(74,232,154,.1);border:1px solid var(--accent3);
     color:var(--accent3);padding:10px 14px;font-size:12px;
     font-family:var(--mono);margin-bottom:16px;}}
.sep{{border:none;border-top:1px solid var(--border);margin:20px 0;}}
.plan-card{{border:1px solid var(--border2);padding:18px;margin-bottom:14px;cursor:pointer;transition:border-color .15s;}}
.plan-card:hover,.plan-card.selected{{border-color:var(--accent2);background:rgba(74,158,232,.05);}}
.plan-name{{font-family:var(--mono);font-size:14px;font-weight:500;color:var(--text);}}
.plan-price{{font-size:22px;font-weight:600;color:var(--accent);margin:4px 0;}}
.plan-desc{{font-size:12px;color:var(--muted);}}
.tag{{display:inline-block;padding:2px 8px;font-size:10px;font-family:var(--mono);
      background:rgba(74,232,154,.15);color:var(--accent3);border:1px solid var(--accent3);
      margin-left:8px;vertical-align:middle;}}
.status-row{{display:flex;justify-content:space-between;align-items:center;
             padding:10px 0;border-bottom:1px solid var(--border);font-size:13px;}}
.status-row:last-child{{border-bottom:none;}}
.status-label{{color:var(--text2);font-family:var(--mono);font-size:11px;letter-spacing:.5px;}}
.status-value{{color:var(--text);font-weight:500;}}
.status-ok{{color:var(--accent3);}}
.status-err{{color:var(--danger);}}
a.link{{color:var(--accent2);text-decoration:none;font-size:12px;font-family:var(--mono);}}
a.link:hover{{text-decoration:underline;}}
.footer-links{{text-align:center;margin-top:18px;font-size:12px;color:var(--muted);font-family:var(--mono);}}
</style>
</head><body>
"""

# ─── LOGIN ─────────────────────────────────────────────────────────────────────

LOGIN_HTML = _BASE_CSS.format(title="Login") + """
<div class="card">
  <div class="card-header">Acceso al sistema</div>
  <div class="card-body">
    <div class="brand">RPI GESTOR</div>
    <div class="subtitle">Buenos Aires · v3.0</div>
    {error_block}
    <form method="POST">
      <div class="field">
        <label>Email</label>
        <input type="email" name="email" required autofocus placeholder="tu@email.com" value="{email}">
      </div>
      <div class="field">
        <label>Contraseña</label>
        <input type="password" name="password" required placeholder="••••••••">
      </div>
      <button class="btn-login" type="submit">Ingresar →</button>
    </form>
    <div class="footer-links" style="margin-top:16px;">
      ¿No tenés cuenta? <a class="link" href="/registro">Registrarse</a>
    </div>
  </div>
</div>
</body></html>
"""

# ─── REGISTRO ──────────────────────────────────────────────────────────────────

REGISTRO_HTML = _BASE_CSS.format(title="Registro") + """
<div class="card">
  <div class="card-header">Crear cuenta</div>
  <div class="card-body">
    <div class="brand">RPI GESTOR</div>
    <div class="subtitle">Creá tu cuenta para comenzar</div>
    {msg_block}
    <form method="POST">
      <div class="field">
        <label>Email</label>
        <input type="email" name="email" required autofocus placeholder="tu@email.com" value="{email}">
      </div>
      <div class="field">
        <label>Contraseña (mínimo 6 caracteres)</label>
        <input type="password" name="password" required placeholder="••••••••" minlength="6">
      </div>
      <div class="field">
        <label>Confirmar contraseña</label>
        <input type="password" name="password2" required placeholder="••••••••" minlength="6">
      </div>
      <button class="btn-login" type="submit">Crear cuenta →</button>
    </form>
    <div class="footer-links" style="margin-top:16px;">
      ¿Ya tenés cuenta? <a class="link" href="/login">Iniciar sesión</a>
    </div>
  </div>
</div>
</body></html>
"""

# ─── SUSCRIPCIÓN ───────────────────────────────────────────────────────────────

SUSCRIPCION_HTML = _BASE_CSS.format(title="Suscripción") + """
<div class="card" style="max-width:480px;">
  <div class="card-header">Suscripción</div>
  <div class="card-body">
    <div class="brand" style="font-size:16px;margin-bottom:4px;">RPI GESTOR</div>

    <!-- Estado actual -->
    <div style="margin-bottom:20px;">
      <div class="status-row">
        <span class="status-label">Usuario</span>
        <span class="status-value">{email}</span>
      </div>
      <div class="status-row">
        <span class="status-label">Estado</span>
        <span class="status-value {status_class}">{status_text}</span>
      </div>
      <div class="status-row">
        <span class="status-label">Plan</span>
        <span class="status-value">{plan}</span>
      </div>
      <div class="status-row">
        <span class="status-label">Vence</span>
        <span class="status-value">{expires}</span>
      </div>
    </div>

    <hr class="sep">

    {payment_block}

    <div class="footer-links">
      <a class="link" href="/logout">Cerrar sesión</a>
      {back_link}
    </div>
  </div>
</div>
</body></html>
"""

PAYMENT_BLOCK_ACTIVE = """
<div class="ok">✅ Tu suscripción está activa. Podés usar el sistema normalmente.</div>
<a href="/" style="display:block;width:100%;padding:12px;background:var(--surface2);
   border:1px solid var(--border2);color:var(--text);font-family:var(--mono);
   font-size:12px;text-align:center;text-decoration:none;letter-spacing:1px;">
   ← Volver al gestor
</a>
"""

PAYMENT_BLOCK_INACTIVE = """
<div class="error" style="margin-bottom:20px;">
  ⚠️ No tenés una suscripción activa. Elegí un plan para continuar.
</div>

<div class="plan-card selected" onclick="selectPlan(this,'mensual')">
  <div class="plan-name">Plan Mensual <span class="tag">MÁS POPULAR</span></div>
  <div class="plan-price">$60.000 <span style="font-size:14px;color:var(--text2)">/mes</span></div>
  <div class="plan-desc">Acceso completo · Renovación automática mensual</div>
</div>

<div class="plan-card" onclick="selectPlan(this,'bimestral')">
  <div class="plan-name">Plan Bimestral</div>
  <div class="plan-price">$90.000 <span style="font-size:14px;color:var(--text2)">/2 meses</span></div>
  <div class="plan-desc">Acceso completo · $45.000/mes promedio</div>
</div>

<div class="plan-card" onclick="selectPlan(this,'trimestral')">
  <div class="plan-name">Plan Trimestral <span class="tag" style="background:rgba(232,200,74,.15);color:var(--accent);border-color:var(--accent)">MEJOR PRECIO</span></div>
  <div class="plan-price">$100.000 <span style="font-size:14px;color:var(--text2)">/3 meses</span></div>
  <div class="plan-desc">Acceso completo · $33.333/mes promedio</div>
</div>

<button id="btn-pagar" onclick="pagar()"
   style="display:block;width:100%;padding:14px;background:#009ee3;border:none;
   color:white;font-size:13px;font-family:var(--mono);letter-spacing:1px;
   text-align:center;cursor:pointer;margin-top:8px;">
   Pagar con MercadoPago →
</button>
<div id="msg-pago" style="margin-top:10px;font-family:var(--mono);font-size:12px;color:var(--text2);text-align:center;"></div>

<script>
let planSeleccionado = 'mensual';
function selectPlan(el, plan) {
  document.querySelectorAll('.plan-card').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  planSeleccionado = plan;
}
async function pagar() {
  const btn = document.getElementById('btn-pagar');
  const msg = document.getElementById('msg-pago');
  btn.disabled = true;
  btn.textContent = 'Generando link...';
  msg.textContent = '';
  try {
    const resp = await fetch('/iniciar_pago', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({plan: planSeleccionado})
    });
    const data = await resp.json();
    if (data.init_point) {
      window.open(data.init_point, '_blank');
      msg.textContent = '✅ Se abrió MercadoPago. Una vez que pagues, tu suscripción se activa sola en minutos.';
    } else {
      msg.style.color = 'var(--danger)';
      msg.textContent = '❌ ' + (data.error || 'Error al generar el link. Intentá de nuevo.');
    }
  } catch(e) {
    msg.style.color = 'var(--danger)';
    msg.textContent = '❌ Error de conexión. Intentá de nuevo.';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Pagar con MercadoPago →';
  }
}
</script>
"""


# ─── Rutas ────────────────────────────────────────────────────────────────────

import requests as _req
import os as _os
FIREBASE_API_KEY   = "AIzaSyDF7W4POQmClkEuwEq8NVmmpQaqKB9495Q"
WEBHOOK_SERVER_URL = "https://rpi-webhook.onrender.com"

def _register_user(email, password):
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_API_KEY}"
    try:
        resp = _req.post(url, json={"email": email, "password": password, "returnSecureToken": True}, timeout=10)
        data = resp.json()
        if "idToken" in data:
            return True, None
        msg_map = {
            "EMAIL_EXISTS": "Ese email ya está registrado.",
            "WEAK_PASSWORD : Password should be at least 6 characters": "La contraseña es muy corta (mínimo 6 caracteres).",
        }
        raw = data.get("error", {}).get("message", "Error al registrar")
        return False, msg_map.get(raw, raw)
    except Exception as e:
        return False, str(e)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    email = ""
    if request.method == "POST":
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        ok, result = login_with_email_password(email, password)
        if ok:
            session["user_email"] = result["email"]
            session["user_id"]    = result["localId"]
            return redirect(url_for("index"))
        error = result

    error_block = f'<div class="error">{error}</div>' if error else ""
    html = LOGIN_HTML.replace("{error_block}", error_block).replace("{email}", email)
    return Response(html, mimetype="text/html")


@auth_bp.route("/registro", methods=["GET", "POST"])
def registro():
    msg_block = ""
    email = ""
    if request.method == "POST":
        email  = request.form.get("email", "").strip()
        pwd    = request.form.get("password", "")
        pwd2   = request.form.get("password2", "")
        if pwd != pwd2:
            msg_block = '<div class="error">Las contraseñas no coinciden.</div>'
        else:
            ok, err = _register_user(email, pwd)
            if ok:
                msg_block = '<div class="ok">✅ Cuenta creada. Ya podés iniciar sesión.</div>'
                email = ""
            else:
                msg_block = f'<div class="error">{err}</div>'

    html = REGISTRO_HTML.replace("{msg_block}", msg_block).replace("{email}", email)
    return Response(html, mimetype="text/html")


@auth_bp.route("/logout")
def logout():
    clear_session()
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.route("/iniciar_pago", methods=["POST"])
def iniciar_pago():
    """Llama al webhook server para crear una preferencia de suscripción en MP."""
    from flask import jsonify
    from firebase_auth import get_valid_token
    id_token, sess = get_valid_token()
    if not id_token or not sess:
        return jsonify({"error": "no autenticado"}), 401

    body = request.json or {}
    plan = body.get("plan", "mensual")
    uid  = sess.get("localId", "")

    try:
        resp = _req.post(
            f"{WEBHOOK_SERVER_URL}/crear_suscripcion",
            json={"uid": uid, "plan": plan},
            timeout=15,
        )
        return resp.json(), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/suscripcion_ok")
def suscripcion_ok():
    """Página de retorno tras pagar en MercadoPago."""
    html = _BASE_CSS.format(title="Pago exitoso") + """
        <div class="card">
          <div class="card-header">Pago recibido</div>
          <div class="card-body" style="text-align:center;padding:40px 28px;">
            <div style="font-size:48px;margin-bottom:16px;">✅</div>
            <div class="brand" style="margin-bottom:8px;">¡Gracias!</div>
            <p style="color:var(--text2);font-size:14px;margin-bottom:24px;">
              Tu suscripción se está activando. En menos de un minuto
              vas a poder acceder al sistema normalmente.
            </p>
            <a href="/suscripcion" style="display:inline-block;padding:12px 28px;
               background:var(--accent2);color:white;font-family:var(--mono);
               font-size:12px;letter-spacing:1px;text-decoration:none;">
               Ver mi suscripción →
            </a>
          </div>
        </div>
        </body></html>
        """
    return Response(html, mimetype="text/html")


@auth_bp.route("/suscripcion")
def suscripcion():
    from firebase_auth import get_valid_token, check_subscription
    id_token, sess = get_valid_token()

    if not id_token or not sess:
        return redirect(url_for("auth.login"))

    email   = sess.get("email", "")
    user_id = sess.get("localId", "")
    info    = get_subscription_info(id_token, user_id)
    active  = check_subscription(id_token, user_id)

    if active:
        payment_block = PAYMENT_BLOCK_ACTIVE
        back_link     = ' · <a class="link" href="/">Volver al gestor</a>'
    else:
        payment_block = PAYMENT_BLOCK_INACTIVE
        back_link = ""

    html = (SUSCRIPCION_HTML
        .replace("{email}",         email)
        .replace("{status_class}",  "status-ok" if active else "status-err")
        .replace("{status_text}",   "✅ Activa" if active else "❌ Sin suscripción")
        .replace("{plan}",          info.get("plan", "-").capitalize())
        .replace("{expires}",       info.get("expires", "-"))
        .replace("{payment_block}", payment_block)
        .replace("{back_link}",     back_link)
    )
    return Response(html, mimetype="text/html")
