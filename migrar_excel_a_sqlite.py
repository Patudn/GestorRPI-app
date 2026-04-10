#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrar_excel_a_sqlite.py
Migra los datos del Excel existente (Gestion_Tramites_RPI.xlsx)
a la base de datos SQLite (.datos/tramites.db) del nuevo sistema.

Uso: python3 migrar_excel_a_sqlite.py
"""

import os
import sqlite3
import pandas as pd
from datetime import datetime

# --- RUTAS ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXCEL_PATH = os.path.join(BASE_DIR, "Gestion_Tramites_RPI.xlsx")
DB_DIR = os.path.join(BASE_DIR, ".datos")
DB_PATH = os.path.join(DB_DIR, "tramites.db")

# =====================================================
# PREPARAR BASE DE DATOS
# =====================================================
def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tramites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ORDEN TEXT,
            TIPO_SOLICITUD TEXT,
            APELLIDO TEXT,
            NOMBRE TEXT,
            DNI TEXT,
            CUIT TEXT,
            PARTIDO TEXT,
            NRO_INSCRIPCION TEXT,
            UF_UC TEXT,
            C TEXT, S TEXT, CH TEXT, CH2 TEXT,
            QTA TEXT, QTA2 TEXT,
            F TEXT, F2 TEXT,
            M TEXT, M2 TEXT,
            P TEXT, P2 TEXT, SP TEXT,
            SOLICITANTE TEXT,
            ESTADO TEXT DEFAULT 'PENDIENTE',
            NRO_TRAMITE TEXT,
            FECHA_CARGA TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

# =====================================================
# LIMPIAR VALOR
# =====================================================
def limpiar(val):
    if val is None: return ""
    s = str(val).strip()
    if s.lower() in ("nan", "none", "nat"): return ""
    # Quitar .0 de números enteros leídos como float
    if s.endswith(".0") and s[:-2].isdigit(): return s[:-2]
    return s

# =====================================================
# MIGRACIÓN
# =====================================================
def migrar():
    print("=" * 55)
    print("  MIGRACIÓN Excel → SQLite")
    print("  Gestor RPI v3.0")
    print("=" * 55)

    # Verificar Excel
    if not os.path.exists(EXCEL_PATH):
        print(f"\n❌ No se encontró el Excel en:\n   {EXCEL_PATH}")
        print("   Copiá el archivo al mismo directorio que este script.")
        return

    # Crear carpeta .datos si no existe
    os.makedirs(DB_DIR, exist_ok=True)

    # Leer Excel
    print(f"\n📂 Leyendo Excel: {EXCEL_PATH}")
    df = pd.read_excel(EXCEL_PATH, dtype=str)
    print(f"   → {len(df)} filas encontradas")
    print(f"   → Columnas: {', '.join(df.columns.tolist())}")

    # Conectar a SQLite
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # Verificar si ya hay datos
    existentes = conn.execute("SELECT COUNT(*) FROM tramites").fetchone()[0]
    if existentes > 0:
        print(f"\n⚠️  La base de datos ya tiene {existentes} registros.")
        resp = input("   ¿Querés agregar los del Excel de todas formas? (s/n): ").strip().lower()
        if resp != 's':
            print("   Migración cancelada.")
            conn.close()
            return

    # Mapeo de columnas Excel → SQLite
    # UF/UC en Excel → UF_UC en SQLite
    col_map = {
        "ORDEN": "ORDEN",
        "TIPO_SOLICITUD": "TIPO_SOLICITUD",
        "APELLIDO": "APELLIDO",
        "NOMBRE": "NOMBRE",
        "DNI": "DNI",
        "CUIT": "CUIT",
        "PARTIDO": "PARTIDO",
        "NRO_INSCRIPCION": "NRO_INSCRIPCION",
        "UF/UC": "UF_UC",
        "C": "C", "S": "S",
        "CH": "CH", "CH2": "CH2",
        "QTA": "QTA", "QTA2": "QTA2",
        "F": "F", "F2": "F2",
        "M": "M", "M2": "M2",
        "P": "P", "P2": "P2",
        "SP": "SP",
        "SOLICITANTE": "SOLICITANTE",
        "ESTADO": "ESTADO",
        "NRO_TRAMITE": "NRO_TRAMITE",
        "FECHA_CARGA": "FECHA_CARGA",
    }

    insertados = 0
    errores = 0

    print(f"\n🔄 Migrando registros...")

    for idx, row in df.iterrows():
        try:
            valores = {}
            for col_excel, col_db in col_map.items():
                if col_excel in df.columns:
                    valores[col_db] = limpiar(row.get(col_excel))
                else:
                    valores[col_db] = ""

            # Asegurar ESTADO válido
            if valores["ESTADO"] not in ("PENDIENTE", "CARGADO", "COMPLETADO"):
                valores["ESTADO"] = "PENDIENTE"

            cols = ", ".join(valores.keys())
            placeholders = ", ".join(["?"] * len(valores))

            conn.execute(
                f"INSERT INTO tramites ({cols}) VALUES ({placeholders})",
                list(valores.values())
            )
            insertados += 1

        except Exception as e:
            print(f"   ⚠️ Error en fila {idx + 2}: {e}")
            errores += 1

    conn.commit()
    conn.close()

    # Resumen
    print(f"\n{'='*55}")
    print(f"  ✅ Migración completada")
    print(f"  → Registros migrados: {insertados}")
    if errores:
        print(f"  → Errores: {errores}")
    print(f"  → Base de datos: {DB_PATH}")
    print(f"{'='*55}")

    # Verificar resultado
    conn2 = sqlite3.connect(DB_PATH)
    total = conn2.execute("SELECT COUNT(*) FROM tramites").fetchone()[0]
    pend = conn2.execute("SELECT COUNT(*) FROM tramites WHERE ESTADO='PENDIENTE'").fetchone()[0]
    carg = conn2.execute("SELECT COUNT(*) FROM tramites WHERE ESTADO='CARGADO'").fetchone()[0]
    comp = conn2.execute("SELECT COUNT(*) FROM tramites WHERE ESTADO='COMPLETADO'").fetchone()[0]
    conn2.close()

    print(f"\n  📊 Estado de la base de datos:")
    print(f"     Total registros : {total}")
    print(f"     ⏳ Pendientes    : {pend}")
    print(f"     📤 Cargados      : {carg}")
    print(f"     ✅ Completados   : {comp}")
    print(f"\n  Podés iniciar el sistema con: python3 gestor_rpi.py\n")

if __name__ == "__main__":
    migrar()