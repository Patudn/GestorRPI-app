# Workflow 02 — Build ejecutable (Mac + Windows)

## Objetivo
Empaquetar `gestor_rpi.py` como ejecutable nativo para Mac (.app) y Windows (.exe) sin
incluir credenciales ni .env. Cada usuario configura sus propias credenciales del portal RPI
al primer arranque.

## Arquitectura de datos por usuario

```
Mac:   ~/Library/Application Support/GestorRPI/
Win:   %APPDATA%\GestorRPI\
         ├── config.json        ← credenciales RPI (USUARIO + PASSWORD)
         ├── session.json       ← token Firebase (ya existía en .datos/)
         ├── tramites.db        ← base de datos SQLite
         └── descargados.txt    ← log de descargas
```

**Nunca va dentro del bundle:** nada sensible. Los informes descargados van a `~/Documentos/GestorRPI/informes/`.

## Flujo de primer arranque

```
Ejecutar GestorRPI
      ↓
¿config.json existe?
  NO  → abrir browser en localhost:5001/setup (pedir USUARIO + PASSWORD del RPI)
  SÍ  → ¿session.json existe?
          NO  → /login (Firebase)
          SÍ  → ¿suscripción activa?
                  NO  → /suscripcion (pagar)
                  SÍ  → / (app normal)
```

## Cambios al código (ya aplicados)

### gestor_rpi.py
- `USER_DATA_DIR`: usa `platformdirs.user_data_dir("GestorRPI", "PatuDN")`
- `INFORMES_DIR`: `~/Documents/GestorRPI/informes/`
- Nueva ruta `/setup` (GET + POST): formulario first-launch para USUARIO/PASSWORD del RPI
- Nueva ruta `/borrar-config`: borra config.json y redirige a /setup
- `load_rpi_credentials()` / `save_rpi_credentials()`: helpers para config.json
- `@app.before_request`: después de verificar suscripción, si no hay config → redirige a /setup

### firebase_auth.py
- `TOKEN_FILE`: apunta a `USER_DATA_DIR/session.json`

### auth_routes.py
- `WEBHOOK_SERVER_URL`: hardcodeado a `https://rpi-webhook.onrender.com` (no más localhost:5050)

## Dependencias nuevas
```
platformdirs>=4.0     # rutas de user data multiplataforma
```

## PyInstaller

### Instalación
```bash
pip install pyinstaller platformdirs
playwright install chromium
```

### Build Mac
```bash
pyinstaller --onedir \
  --name GestorRPI \
  --add-data "$(python -c 'import playwright; import os; print(os.path.dirname(playwright.__file__))')/:playwright/" \
  --hidden-import playwright \
  --hidden-import pdfplumber \
  --hidden-import firebase_admin \
  gestor_rpi.py
```

### Build Windows (desde Windows)
```bash
pyinstaller --onedir ^
  --name GestorRPI ^
  --add-data "playwright;playwright" ^
  --hidden-import playwright ^
  --hidden-import pdfplumber ^
  --hidden-import firebase_admin ^
  gestor_rpi.py
```

## Distribución
1. Comprimir `dist/GestorRPI/` → `GestorRPI_Mac.zip` / `GestorRPI_Win.zip`
2. Subir a Google Drive con acceso restringido (o GitHub Releases privado)
3. Pegar links en `download.html` (reemplazar `REEMPLAZAR_CON_LINK_DE_DESCARGA`)

## Estado
- [x] Cambios al código (gestor_rpi.py, firebase_auth.py, auth_routes.py)
- [ ] Instalar platformdirs y testear flujo first-launch localmente
- [ ] Build Mac con PyInstaller
- [ ] Build Windows (necesita máquina Windows o VM)
- [ ] Subir ejecutables a Google Drive
- [ ] Actualizar download.html con links reales
