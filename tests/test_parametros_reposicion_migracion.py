"""La revisión 0003 (ADR-020): `catalog_items.lead_time_days` y `max_stock`. Las fixtures y los helpers son los de
`tests/test_vencimientos.py` (una base por motor)."""
from __future__ import annotations

import test_vencimientos as _vto
from alembic import command
from test_vencimientos import (
    PREVIA,
    _columnas_de,
    _con_conexion,
    _crear_base_al_dia_de_0001,
    _ledger,
    catalogo,
    migrar,
    stock,
)

destino = _vto.destino


def _poblar(conn):
    yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0, stock_minimo=5.0)
    stock.ajustar_stock(conn, yerba, 40.0, "inicial", fecha=PREVIA)
    conn.commit()
    return _ledger(conn)


def test_la_revision_0003_corre_sobre_datos_previos_sin_tocar_una_fila_y_es_idempotente(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino, "0002_vencimientos_lotes")
    antes = _con_conexion(destino, _poblar)

    migrar.upgrade(destino, "0003_parametros_reposicion")

    def verificar(conn):
        cols = _columnas_de(conn, "catalog_items")
        assert {"lead_time_days", "max_stock"} <= cols
        fila = conn.execute("SELECT lead_time_days, max_stock, min_stock FROM catalog_items").fetchall()
        assert len(fila) == 1 and fila[0][0] is None and fila[0][1] is None and float(fila[0][2]) == 5.0
        assert _ledger(conn) == antes, "la revisión modificó el ledger"
        return [f[0] for f in conn.execute("SELECT version_num FROM alembic_version_libracommerce").fetchall()]

    assert _con_conexion(destino, verificar) == ["0003_parametros_reposicion"]
    migrar.upgrade(destino, "0003_parametros_reposicion")
    assert _con_conexion(destino, verificar) == ["0003_parametros_reposicion"]


def test_la_revision_0003_baja_sin_tocar_el_ledger_ni_la_marca_de_vencimiento(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino, "0003_parametros_reposicion")
    antes = _con_conexion(destino, _poblar)

    command.downgrade(migrar.configuracion(destino), "0002_vencimientos_lotes")

    def verificar(conn):
        cols = _columnas_de(conn, "catalog_items")
        assert "lead_time_days" not in cols and "max_stock" not in cols and "tracks_expiry" in cols
        assert _ledger(conn) == antes, "bajar la revisión modificó el ledger"

    _con_conexion(destino, verificar)
    migrar.upgrade(destino)   # y vuelve a subir
