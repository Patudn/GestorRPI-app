# Workflow 01 — Landing Page de Venta (gestorrpi.netlify.app)

## Objetivo
Construir el sitio web estático que comercializa el script `gestor_rpi.py`. El sitio vive en Netlify y se integra con el backend ya existente (Firebase Auth + MercadoPago + webhook_server en Render).

## Stack
- HTML + CSS + JS vanilla (sin frameworks)
- Firebase Auth (web SDK v9 modular) para login/registro
- Firestore (web SDK) para verificar suscripción activa
- MercadoPago via endpoint `/crear_suscripcion` del webhook_server en Render
- Deploy: Netlify (drag & drop o CLI)

## Estructura de archivos (en `landing/`)
```
landing/
  index.html       # Landing de venta: hero, beneficios, planes, CTA
  login.html       # Registro y login con Firebase Auth
  download.html    # Página protegida: verifica suscripción antes de mostrar descarga
  assets/
    style.css      # Estilos compartidos
    app.js         # Lógica Firebase Auth compartida (init, sesión)
    plans.js       # Lógica de compra: llama a /crear_suscripcion y redirige a MP
    download.js    # Verifica suscripción en Firestore y desbloquea descarga
```

## Inputs requeridos
- Firebase config object (apiKey, authDomain, projectId, etc.)
- URL del webhook_server en Render (`RENDER_URL`)
- IDs de planes de MercadoPago (mensual, bimestral, trimestral)
- Precio de cada plan
- URL de descarga del script (Google Drive, etc.)

## Flujo de usuario
1. Usuario llega a `index.html` → ve beneficios → hace click en un plan
2. Si no está logueado → redirige a `login.html`
3. En `login.html` → crea cuenta o loguea con Firebase Auth
4. Después del login → vuelve al plan elegido → se llama `/crear_suscripcion` con `{uid, plan}`
5. Render devuelve `init_point` de MercadoPago → redirige al usuario al checkout
6. MercadoPago procesa el pago → llama al webhook → Firestore se actualiza → `active: true`
7. MercadoPago redirige a `download.html`
8. `download.html` lee Firestore → si `active: true` → muestra el link de descarga

## Páginas

### index.html
- Hero: nombre del producto, tagline, CTA ("Empezar ahora")
- Sección beneficios (3-4 puntos clave del script)
- Sección precios: 3 cards (mensual / bimestral / trimestral)
- Footer

### login.html
- Tabs: Iniciar sesión / Crear cuenta
- Formulario email + password
- Feedback de errores inline
- Después del login: redirige al plan seleccionado (guardado en sessionStorage)

### download.html
- Protegida: si no hay sesión o suscripción inactiva → redirige a login
- Muestra nombre del plan y fecha de vencimiento
- Botón de descarga (link al script)
- Instrucciones de instalación

## Consideraciones
- El `external_reference` que MercadoPago devuelve al webhook es el Firebase UID
- El `back_url` en webhook_server ya apunta a `gestorrpi.netlify.app/download.html`
- Usar `sessionStorage` para persistir el plan elegido durante el flujo de compra
- Firebase SDK se carga desde CDN (no requiere build step)

## Estado
- [ ] index.html
- [ ] login.html
- [ ] download.html
- [ ] assets/style.css
- [ ] assets/app.js
- [ ] assets/plans.js
- [ ] assets/download.js
- [ ] Deploy a Netlify
