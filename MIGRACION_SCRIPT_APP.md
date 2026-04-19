# Reglas de Migración: Script Personal → App GestorRPI

Este documento define qué migrar y qué no cuando se portan mejoras desde el script personal (`gestor_rpi.py` personal) hacia la app vendida a clientes.

---

## Reglas Generales

### SIEMPRE migrar
- Mejoras en funciones Playwright (manejo de errores del portal, pantallas inesperadas, recuperación de trámites)
- Nuevas columnas en la DB que mejoren el tracking (NOTAS, FECHA_COMPLETADO, etc.)
- Funciones de recuperación de datos (rescate SIN_NRO, búsqueda por adyacencia)
- Mejoras en la UI de la tabla de pedidos (filtros, ordenamiento, acciones por fila)
- Nuevas rutas de estadísticas o exportación

### NUNCA migrar (hasta que este .md lo indique)
- **Gestión de email**: `enviar_email_informes()`, `escanear_informes()`, modal de email, variables `EMAIL_*`. La app no tendrá este feature hasta nuevo aviso.
- **Función "Vaciar carpeta"** (opción 6 del menú del script): no aplica al flujo de la app.

---

## Reglas Específicas

### Solicitantes
- **No migrar listas predefinidas**: `SOLICITANTES_BASE` en la app debe quedar `[]` (lista vacía). Cada usuario de la app define sus propios solicitantes con el uso.
- El sistema de ranking (`_calcular_ranking`, `obtener_solicitantes`, `registrar_uso_solicitante`) SÍ debe migrarse porque es la mecánica de autocompletado.

### Campo ORDEN
- En la app, el número de orden **no es obligatorio** al cargar un formulario. El usuario puede dejarlo vacío.
- El script personal puede tener validación de ORDEN requerida — ignorar esa restricción al migrar.

### Rutas y Paths
- El script personal usa `BASE_DIR` (directorio del archivo). La app usa `USER_DATA_DIR` (de `platformdirs`).
- Al migrar cualquier función que referencie rutas de archivos, reemplazar `BASE_DIR` → `USER_DATA_DIR`.
- `DOWNLOAD_PATH`, `ERROR_PATH`, `LOGS_PATH`, `DB_PATH`, `LOG_FILE` ya están definidos correctamente en la app — no redefinir.

### Credenciales
- El script personal usa `.env` + `load_dotenv()` + `os.getenv("USUARIO")`.
- La app usa `config.json` leído desde `USER_DATA_DIR`. No migrar el patrón `.env`.

### Puerto Flask
- El script personal tiene `PORT = int(os.getenv("FLASK_PORT", "5002"))`. La app usa puerto fijo 5001. No cambiar.

### Auth y Suscripción
- La app tiene Firebase Auth, `check_subscription()`, `@before_request verificar_acceso()`, blueprint `auth_routes`. **Nunca tocar estos sistemas al migrar features**.
- El script personal no tiene auth — simplemente ignorar esa diferencia.

### Funciones del menú principal
| Función | Migrar |
|---|---|
| 1. Cargar Base (importar Excel) | ✅ Sí |
| 2. Solicitar (solo carga) | ✅ Sí |
| 3. Solicitar + Descargar | ✅ Sí |
| 4. Descargar informes | ✅ Sí |
| 5. Revisar/Enviar informes por email | ❌ No (ver arriba) |
| 6. Vaciar carpeta informes | ❌ No |

---

## Checklist antes de cada migración

1. ¿La función usa rutas de archivo? → reemplazar `BASE_DIR` por `USER_DATA_DIR`
2. ¿La función usa credenciales del portal? → usar `get_credentials()` de la app, no `os.getenv()`
3. ¿La función modifica la DB? → verificar que las columnas existan (y agregar migración en `init_db()` si son nuevas)
4. ¿La función toca email? → no migrar
5. ¿La función valida ORDEN como requerida? → hacerlo opcional
6. ¿La función define `SOLICITANTES_BASE` con valores? → dejarlo `[]`
7. Después de editar: `python3 -m py_compile gestor_rpi.py`
