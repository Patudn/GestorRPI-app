# Workflow 04 — Testear el flujo de pagos con MercadoPago

## Estado actual
El servidor webhook en Render está OK: https://rpi-webhook.onrender.com ✅

## Checklist antes de testear

### 1. Configurar la URL de webhook en MercadoPago
1. Entrá a https://www.mercadopago.com.ar/developers/panel
2. Elegí tu aplicación
3. Ir a **Webhooks** (en el menú izquierdo)
4. Agregá la URL: `https://rpi-webhook.onrender.com/webhook`
5. Seleccioná el evento: **Suscripciones (preapproval)**
6. Guardá

### 2. Verificar IDs de planes en Render
En Render → tu servicio → Environment:
- `MP_PLAN_MENSUAL_ID` = el ID del plan mensual de MP
- `MP_PLAN_BIMESTRAL_ID` = ID bimestral
- `MP_PLAN_TRIMESTRAL_ID` = ID trimestral

Para obtener los IDs si no los tenés:
```bash
curl -s "https://api.mercadopago.com/preapproval_plan/search?status=active" \
  -H "Authorization: Bearer TU_ACCESS_TOKEN" | python3 -m json.tool
```

### 3. Test con usuario real (recomendado para suscripciones)
MercadoPago no tiene sandbox para suscripciones preapproval en Argentina.
El test más confiable es hacer una compra real con tu propio método de pago
y luego verificar que:
  a) El webhook llegó a Render (ver logs)
  b) Firestore se actualizó (ver consola de Firebase)
  c) download.html muestra "Suscripción activa"

### 4. Test manual del webhook (simular un pago)
```bash
# Simula que MP notifica un pago aprobado
curl -X POST https://rpi-webhook.onrender.com/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "type": "preapproval",
    "data": {
      "id": "ID_DE_UNA_SUSCRIPCION_REAL_DE_MP"
    }
  }'
```

### 5. Activar suscripción manualmente (para testing sin pago)
Podés escribir directamente en Firestore desde la consola:
1. firebase.google.com → proyecto rpi-bsas → Firestore
2. Colección: `subscriptions`
3. Documento: `UID_DEL_USUARIO` (lo ves en Authentication)
4. Campos:
   ```json
   {
     "active": true,
     "plan": "mensual",
     "expires_at": "2026-12-31T00:00:00+00:00"
   }
   ```

## Verificar que el pago llegó

### Render logs
1. Render dashboard → tu servicio → **Logs**
2. Deberías ver líneas como:
   ```
   📩 Webhook recibido: {'type': 'preapproval', 'data': {'id': '...'}}
   ✅ Suscripción activada: uid=xxx, plan=mensual, vence=2026-05-10
   ```

### Firebase Firestore
1. firebase.google.com → rpi-bsas → Firestore
2. Colección `subscriptions` → documento con el UID del usuario
3. Debería tener `active: true` y `expires_at` en el futuro

## Flujo completo esperado
1. Usuario va a gestorrpi.com.ar
2. Elige plan → register.html (crea cuenta o loguea)
3. Se redirige a checkout de MP con `external_reference=UID`
4. Usuario paga en MP
5. MP hace POST a https://rpi-webhook.onrender.com/webhook
6. Webhook consulta la API de MP para verificar el pago
7. Webhook escribe en Firestore: active=true, plan=X, expires_at=Y
8. Usuario vuelve a download.html → ve "Suscripción activa"
