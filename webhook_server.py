"""
webhook_server.py — Servidor Flask para recibir notificaciones de MercadoPago
Se despliega en Render.com (gratis) y actualiza Firestore automáticamente.

Flujo:
  1. Usuario paga en MercadoPago
  2. MercadoPago hace POST a /webhook con los datos del pago
  3. Este servidor verifica el pago y escribe en Firestore:
     subscriptions/{uid} → {active: true, plan: "mensual", expires_at: ...}
"""

import os
import hmac
import hashlib
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)
# Permitir requests desde el sitio web (Netlify, GitHub Pages, etc.)
CORS(app, resources={r"/crear_suscripcion": {"origins": "*"}})

# ─── Configuración (se leen de variables de entorno en Render) ────────────────
MP_ACCESS_TOKEN   = os.environ.get("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_SECRET = os.environ.get("MP_WEBHOOK_SECRET", "")

# IDs de los planes de suscripción en MercadoPago (filtrar claves vacías)
_PLAN_IDS_RAW = {
    os.environ.get("MP_PLAN_MENSUAL_ID", ""):    {"nombre": "mensual",    "meses": 1},
    os.environ.get("MP_PLAN_BIMESTRAL_ID", ""):  {"nombre": "bimestral",  "meses": 2},
    os.environ.get("MP_PLAN_TRIMESTRAL_ID", ""): {"nombre": "trimestral", "meses": 3},
}
PLAN_IDS = {k: v for k, v in _PLAN_IDS_RAW.items() if k}

# Advertir al arrancar si falta algún plan
_planes_esperados = {"MP_PLAN_MENSUAL_ID", "MP_PLAN_BIMESTRAL_ID", "MP_PLAN_TRIMESTRAL_ID"}
for _var in _planes_esperados:
    if not os.environ.get(_var):
        print(f"⚠️  Variable de entorno faltante: {_var} — plan no disponible")

# ─── Firebase Admin (usa Service Account desde variable de entorno) ────────────
_firebase_initialized = False

def init_firebase():
    global _firebase_initialized
    if _firebase_initialized:
        return
    import json
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
    if sa_json:
        sa_dict = json.loads(sa_json)
        cred = credentials.Certificate(sa_dict)
    else:
        cred = credentials.Certificate("service_account.json")
    firebase_admin.initialize_app(cred)
    _firebase_initialized = True

def get_db():
    init_firebase()
    return firestore.client()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def verificar_firma_mp(payload_bytes: bytes, signature_header: str, request_id: str, data_id: str) -> bool:
    """Verifica la firma HMAC-SHA256 de MercadoPago."""
    if not MP_WEBHOOK_SECRET:
        return False
    try:
        ts = None
        v1 = None
        for part in signature_header.split(","):
            part = part.strip()
            if part.startswith("ts="):
                ts = part[3:]
            elif part.startswith("v1="):
                v1 = part[3:]
        if not ts or not v1:
            return False
        signed_template = f"id:{data_id};request-id:{request_id};ts:{ts};"
        expected = hmac.new(
            MP_WEBHOOK_SECRET.encode(),
            signed_template.encode(),
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, v1)
    except Exception:
        return False


def obtener_datos_suscripcion(preapproval_id: str) -> dict | None:
    """Consulta la API de MercadoPago para obtener los datos de la suscripción."""
    url = f"https://api.mercadopago.com/preapproval/{preapproval_id}"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    resp = requests.get(url, headers=headers, timeout=10)
    if resp.status_code == 200:
        return resp.json()
    return None


def activar_suscripcion(uid: str, plan_nombre: str, meses: int):
    """Escribe o actualiza el documento de suscripción en Firestore."""
    db = get_db()
    expires_at = datetime.now(timezone.utc) + timedelta(days=30 * meses)
    db.collection("subscriptions").document(uid).set({
        "active": True,
        "plan": plan_nombre,
        "expires_at": expires_at.isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    print(f"✅ Suscripción activada: uid={uid}, plan={plan_nombre}, vence={expires_at.date()}")


def desactivar_suscripcion(uid: str, motivo: str = ""):
    """Marca la suscripción como inactiva en Firestore."""
    db = get_db()
    db.collection("subscriptions").document(uid).set({
        "active": False,
        "motivo_baja": motivo,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, merge=True)
    print(f"⛔ Suscripción cancelada: uid={uid}, motivo={motivo}")


def _ya_procesado(preapproval_id: str) -> bool:
    """Devuelve True si este preapproval_id ya fue procesado (idempotencia)."""
    try:
        db = get_db()
        doc = db.collection("webhook_events").document(preapproval_id).get()
        return doc.exists
    except Exception:
        return False


def _marcar_procesado(preapproval_id: str, uid: str, estado: str):
    """Registra el evento para evitar reprocesarlo."""
    try:
        db = get_db()
        db.collection("webhook_events").document(preapproval_id).set({
            "uid": uid,
            "estado": estado,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        print(f"⚠️  No se pudo registrar evento {preapproval_id}: {e}")


# ─── Rutas ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "RPI Webhook Server"})


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Recibe notificaciones de MercadoPago sobre suscripciones.
    MercadoPago envía eventos como: authorized, paused, cancelled.
    """
    data = request.json or {}
    data_id = str(data.get("data", {}).get("id", ""))

    # Verificar firma HMAC antes de procesar nada
    signature_header = request.headers.get("x-signature", "")
    request_id       = request.headers.get("x-request-id", "")
    if not verificar_firma_mp(request.get_data(), signature_header, request_id, data_id):
        print(f"⛔ Webhook rechazado: firma inválida (x-signature={signature_header!r})")
        return jsonify({"error": "firma inválida"}), 401

    print(f"📩 Webhook recibido: {data}")

    # Solo nos interesan eventos de suscripciones (preapproval)
    if data.get("type") != "preapproval":
        return jsonify({"status": "ignored"}), 200

    preapproval_id = data_id
    if not preapproval_id:
        return jsonify({"error": "sin id"}), 400

    # Idempotencia: si ya lo procesamos, responder OK sin reprocesar
    if _ya_procesado(preapproval_id):
        print(f"↩️  Evento {preapproval_id} ya procesado, ignorando")
        return jsonify({"status": "already_processed"}), 200

    # Consultar MP para obtener los detalles reales del pago
    suscripcion = obtener_datos_suscripcion(preapproval_id)
    if not suscripcion:
        print(f"❌ No se pudo obtener suscripción {preapproval_id}")
        return jsonify({"error": "no encontrada"}), 500  # 500 para que MP reintente

    uid = suscripcion.get("external_reference")
    if not uid:
        print("❌ Sin external_reference (Firebase UID)")
        return jsonify({"error": "sin uid"}), 400

    plan_id = suscripcion.get("preapproval_plan_id", "")
    estado  = suscripcion.get("status", "")

    print(f"   uid={uid}, plan_id={plan_id}, estado={estado}")

    if estado == "authorized":
        plan_info = PLAN_IDS.get(plan_id, {"nombre": "mensual", "meses": 1})
        try:
            activar_suscripcion(uid, plan_info["nombre"], plan_info["meses"])
            _marcar_procesado(preapproval_id, uid, estado)
        except Exception as e:
            print(f"❌ Error al escribir en Firestore para uid={uid}: {e}")
            return jsonify({"error": "error interno"}), 500

    elif estado in ("cancelled", "paused"):
        try:
            desactivar_suscripcion(uid, motivo=estado)
            _marcar_procesado(preapproval_id, uid, estado)
        except Exception as e:
            print(f"❌ Error al desactivar suscripción uid={uid}: {e}")
            return jsonify({"error": "error interno"}), 500

    return jsonify({"status": "ok"}), 200


@app.route("/crear_suscripcion", methods=["POST"])
def crear_suscripcion():
    """
    La app local llama a este endpoint para crear una preferencia de suscripción.
    Recibe: {uid, plan} donde plan es "mensual", "bimestral" o "trimestral"
    Devuelve: {init_point: "https://www.mercadopago.com.ar/..."}
    """
    body = request.json or {}
    uid  = body.get("uid")
    plan = body.get("plan")

    if not uid or not plan:
        return jsonify({"error": "faltan parámetros"}), 400

    plan_id_map = {
        "mensual":    os.environ.get("MP_PLAN_MENSUAL_ID", ""),
        "bimestral":  os.environ.get("MP_PLAN_BIMESTRAL_ID", ""),
        "trimestral": os.environ.get("MP_PLAN_TRIMESTRAL_ID", ""),
    }
    plan_id = plan_id_map.get(plan)
    if not plan_id:
        return jsonify({"error": "plan inválido o no configurado"}), 400

    init_point = (
        f"https://www.mercadopago.com.ar/subscriptions/checkout"
        f"?preapproval_plan_id={plan_id}"
        f"&external_reference={uid}"
    )
    print(f"✅ Link generado para uid={uid}, plan={plan}: {init_point}")
    return jsonify({"init_point": init_point})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)
