"""
Crea un plan de suscripción de $1 en MercadoPago para testear el flujo completo.
Uso:
    python3 tools/crear_plan_test.py
Te va a pedir el MP_ACCESS_TOKEN (lo copiás de Render → Environment).
"""

import json
import getpass
import requests

def main():
    print("=" * 55)
    print("  Crear plan de prueba $1 — MercadoPago")
    print("=" * 55)
    print()
    print("Copiá el MP_ACCESS_TOKEN de Render → tu servicio → Environment")
    print("(empieza con APP_USR-...)")
    print()
    token = getpass.getpass("MP_ACCESS_TOKEN: ").strip()
    if not token:
        print("❌ Token vacío. Saliendo.")
        return

    payload = {
        "reason": "GestorRPI - Plan Test $1",
        "auto_recurring": {
            "frequency":          1,
            "frequency_type":     "months",
            "transaction_amount": 1,
            "currency_id":        "ARS",
        },
        "back_url": "https://gestorrpi.netlify.app/download.html",
        "status":   "active",
    }

    print()
    print("Creando plan en MercadoPago...")
    resp = requests.post(
        "https://api.mercadopago.com/preapproval_plan",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )

    data = resp.json()

    if resp.status_code in (200, 201) and "id" in data:
        plan_id   = data["id"]
        plan_link = data.get("init_point", "")
        print()
        print("✅ Plan creado exitosamente!")
        print()
        print(f"  ID del plan:  {plan_id}")
        print(f"  Nombre:       {data.get('reason', '')}")
        print(f"  Monto:        ${data.get('auto_recurring', {}).get('transaction_amount', '')} ARS/mes")
        print()
        print("─" * 55)
        print("PRÓXIMOS PASOS:")
        print()
        print("1. Anotá el ID del plan para usarlo en Render si querés")
        print("   probarlo de forma completa (no es necesario para el test).")
        print()
        print("2. Para TESTEAR el pago directamente, abrí este link:")
        if plan_link:
            print(f"   {plan_link}")
        else:
            checkout = (
                f"https://www.mercadopago.com.ar/subscriptions/checkout"
                f"?preapproval_plan_id={plan_id}"
                f"&external_reference=TU_UID_FIREBASE"
            )
            print(f"   {checkout}")
            print()
            print("   Reemplazá TU_UID_FIREBASE por tu UID real de Firebase")
            print("   (Firebase Console → Authentication → tu email → User UID)")
        print()
        print("3. Pagá con tu tarjeta real (son $1 ARS).")
        print()
        print("4. Verificá en Render → Logs que aparezca:")
        print('   "✅ Suscripción activada: uid=..."')
        print()
        print("5. Verificá en Firestore que el documento subscriptions/{uid}")
        print("   tenga active=true.")
        print()
        print("6. ¡Listo! El flujo completo funciona.")
        print()
        print("─" * 55)
        print("Para CANCELAR el plan de $1 después del test:")
        print(f"  https://www.mercadopago.com.ar/subscriptions")
        print("  (buscás 'GestorRPI - Plan Test $1' y lo cancelás)")
        print("=" * 55)
    else:
        print()
        print(f"❌ Error {resp.status_code}:")
        print(json.dumps(data, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
