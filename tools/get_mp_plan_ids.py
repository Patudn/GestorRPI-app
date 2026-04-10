"""
tools/get_mp_plan_ids.py
Consulta la API de MercadoPago y lista todos los planes de suscripción con sus IDs.
"""

import requests
import os

# Intentar leer el token del entorno, sino pedirlo
token = os.environ.get("MP_ACCESS_TOKEN") or input("Pegá tu MP_ACCESS_TOKEN: ").strip()

resp = requests.get(
    "https://api.mercadopago.com/preapproval_plan/search",
    headers={"Authorization": f"Bearer {token}"},
    params={"limit": 20, "offset": 0},
    timeout=10,
)

if resp.status_code != 200:
    print(f"❌ Error {resp.status_code}: {resp.text}")
    exit(1)

resultados = resp.json().get("results", [])

if not resultados:
    print("⚠️  No se encontraron planes. Verificá que el token sea de PRODUCCIÓN.")
    exit(0)

print(f"\n{'─'*60}")
print(f"  {'PLAN':<20} {'PRECIO':>10}   ID")
print(f"{'─'*60}")
for p in resultados:
    nombre = p.get("reason", "—")[:20]
    precio = p.get("auto_recurring", {}).get("transaction_amount", "?")
    pid    = p.get("id", "—")
    status = p.get("status", "")
    activo = "✅" if status == "active" else "⛔"
    print(f"  {activo} {nombre:<20} ${precio:>8}   {pid}")

print(f"{'─'*60}\n")
