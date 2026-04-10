#!/bin/bash
# tools/build_dist_mac.sh
# Arma el paquete de distribución final para Mac: GestorRPI_Mac.zip
# Estructura: GestorRPI_Mac/ ├── GestorRPI.app └── browsers/chromium-1208/

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$SCRIPT_DIR/dist"
PKG_NAME="GestorRPI_Mac"
PKG_DIR="$DIST_DIR/$PKG_NAME"
CHROMIUM_SRC="$HOME/Library/Caches/ms-playwright/chromium-1208"

echo "═══════════════════════════════════════════════"
echo "  GestorRPI — Build distribución Mac"
echo "═══════════════════════════════════════════════"

# Verificar que el .app existe
if [ ! -d "$DIST_DIR/GestorRPI.app" ]; then
  echo "❌ No se encontró dist/GestorRPI.app"
  echo "   Corré primero: pyinstaller GestorRPI.spec --clean -y"
  exit 1
fi

# Verificar que Chromium está instalado
if [ ! -d "$CHROMIUM_SRC" ]; then
  echo "❌ Chromium no encontrado en $CHROMIUM_SRC"
  echo "   Corré: playwright install chromium"
  exit 1
fi

# Limpiar y crear carpeta de distribución
echo "→ Creando carpeta de distribución..."
rm -rf "$PKG_DIR"
mkdir -p "$PKG_DIR/browsers"

# Copiar .app
echo "→ Copiando GestorRPI.app (~200 MB)..."
cp -r "$DIST_DIR/GestorRPI.app" "$PKG_DIR/"

# Copiar Chromium
echo "→ Copiando Chromium (~340 MB)..."
cp -r "$CHROMIUM_SRC" "$PKG_DIR/browsers/"

# Copiar LEEME
echo "→ Copiando LEEME.txt..."
cp "$SCRIPT_DIR/LEEME.txt" "$PKG_DIR/"

# Crear ZIP
echo "→ Creando ZIP..."
cd "$DIST_DIR"
zip -r -q "$PKG_NAME.zip" "$PKG_NAME"

echo ""
echo "✅ Listo: dist/$PKG_NAME.zip"
du -sh "$DIST_DIR/$PKG_NAME.zip"
echo ""
echo "Para distribuir:"
echo "  1. Subir $PKG_NAME.zip a Google Drive"
echo "  2. Pegar el link en download.html"
echo "═══════════════════════════════════════════════"
