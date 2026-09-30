"""Red de seguridad de A-4 (ADR-018): el ledger EXACTO que escriben las salidas de stock de un producto sin marcar.

Contalibra, Restolibra y VentaLibra comparten `erp.stock.descontar_stock_venta`, `erp.ventas.anular_venta` /
`devolver_items`, `catalogo.transferir_stock` y `stock.ajustar_stock`. A-4 va a cambiar esas salidas para que
descuenten por lote (FEFO) **sólo** en los productos marcados `tracks_expiry=1`. Estos tests son de CARACTERIZACIÓN:
fijan, sobre el código de hoy, cada fila de `stock_movements` que producen esas operaciones sobre productos que NO
usan lotes, para que los PRs siguientes prueben que no cambió ni una fila. Son la verdad del código actual: si un caso
falla después, cambió el comportamiento de quien no pidió nada (no se corrige el test, se corrige el cambio).

Qué se compara: el ledger COMPLETO de lo que escribió la operación (todas las columnas salvo `id` y `created_at`, en
orden de escritura) contra tuplas escritas a mano. El orden de las columnas es el de `COLUMNAS`. Los ids (producto,
depósito, venta) son los que reparte la base en cada corrida, así que entran como variables; todo lo demás es literal.

Tres perfiles de producto, y las mismas filas esperadas para los tres (es justo lo que se mide):

- `sin_marcar`: producto común, stock sin lote;
- `sin_marcar_con_lotes`: producto SIN marcar cuyo stock entró por una recepción de compra con lote y vencimiento. Se
  vende «como siempre», SIN FEFO: las salidas no llevan lote y los lotes del ledger no se tocan;
- `marcado_sin_lotes`: producto marcado con `vencimientos.marcar_vence` pero sin ningún lote en el ledger. Hoy
  produce el mismo ledger que uno sin marcar, y A-4 tiene que conservarlo: **un marcado sin lotes se comporta igual**.

Al final, una familia aparte y rotulada (`test_a4_cambia_*`): el comportamiento actual de un marcado CON lotes, que A-4
cambia a propósito. Todos esos tests pasan por `_hoy_la_salida_no_descuenta_por_lote`, el único punto a invertir.
"""

from __future__ import annotations

import datetime
import inspect
import re
from contextlib import contextmanager
from decimal import Decimal

import pytest
from conftest import USUARIO, _schema_de_producto, url_postgres
from libracore.db import core

from libracommerce import migrar
from libracommerce.db.repository import SqliteCommerceRepository, repositorio_de
from libracommerce.domain.entities import Party, PartyType
from libracommerce.erp import Hooks, Insumo, catalogo, compras, stock, vencimientos, ventas

#: «Hoy» del reporte de lotes (no toca lo que se escribe).
HOY = datetime.date(2026, 9, 30)
#: La fecha de las cargas iniciales.
PREVIA = "2026-09-01"
#: La fecha que llevan las ventas de estos tests (`fecha` de `crear_venta_directa`).
FECHA_VENTA = "2026-09-10"
#: La fecha que `anular_venta` y `devolver_items` toman solas (`libracore.db.core._ar_now`, fijada por `hoy_fijo`).
FECHA_HOY = "2026-09-20"

PERFILES = ("sin_marcar", "sin_marcar_con_lotes", "marcado_sin_lotes")

#: Las columnas de cada tupla del ledger, en este orden (todas las de `stock_movements` salvo `id` y `created_at`).
COLUMNAS = ("movement_type, quantity_delta, location_id, item_id, variant_id, reason_code, source_type, source_id, "
            "lot_code, expires_at, note, created_by, occurred_at, unit_cost")

#: `add_movimiento_stock` sin lote ni vencimiento: el mismo `INSERT` de `tests/test_vencimientos.py`.
_INSERT_DE_SIEMPRE = """INSERT INTO stock_movements
           (item_id, variant_id, location_id, movement_type, quantity_delta, occurred_at,
            source_type, source_id, note, created_by, reason_code)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)"""

#: El `INSERT` del repositorio (`append_stock_movement`), por donde pasa la transferencia: escribe siempre todas las
#: columnas, con el lote y el vencimiento en `None` si no vienen.
_INSERT_DEL_REPOSITORIO = """
            INSERT INTO stock_movements
                (item_id, variant_id, location_id, movement_type, quantity_delta, occurred_at,
                 source_type, source_id, unit_cost, lot_code, expires_at,
                 note, created_by, reason_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """


# ── Fixtures: una base por motor con los dos schemas de un producto y la revisión 0002 (lote y vencimiento) ──


def _liberar():
    core._db_path = None
    core._database_url = None


@pytest.fixture(params=["sqlite", "postgres"])
def abrir_ledger(request, tmp_path):
    """Un `conexion()` como el de un producto (LibraCore + LibraCommerce, el depósito principal, la revisión `0002`
    aplicada). Contra PostgreSQL limpia el schema `public`: sólo corre contra la base de test EXCLUSIVA."""
    if request.param == "sqlite":
        destino = str(tmp_path / "ledger.db")
    else:
        destino = url_postgres()
    core.configure(destino)
    conn = core.get_connection()
    try:
        if request.param == "postgres":
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.commit()
        _schema_de_producto(conn)
    finally:
        conn.close()
    _liberar()
    migrar.upgrade(destino)
    core.configure(destino)
    yield core.get_connection
    _liberar()


@pytest.fixture(params=PERFILES)
def perfil(request):
    return request.param


@pytest.fixture(autouse=True)
def hoy_fijo(monkeypatch):
    """`anular_venta` y `devolver_items` toman «hoy» de `libracore.db.core._ar_now`: se fija para que `occurred_at`
    sea un literal más del ledger y el test no dependa de la hora ni de la medianoche."""
    monkeypatch.setattr(core, "_ar_now", lambda: f"{FECHA_HOY} 12:00:00")


# ── Helpers ─────────────────────────────────────────────────────────────


def _recibir(conn, pid, lotes, deposito):
    """Una recepción de compra REAL con lote y vencimiento por línea: escribe las filas `purchase` del ledger."""
    proveedor = repositorio_de(conn).save_party(Party(None, PartyType.ORGANIZATION, "Distribuidora SA")).id
    rid = compras.crear_recepcion(conn, supplier_party_id=proveedor)["id"]
    for lote, vence, cantidad in lotes:
        compras.agregar_linea_recepcion(conn, rid, item_id=pid, quantity=cantidad, unit_cost=Decimal("65"),
                                        lot_code=lote, expires_at=vence)
    compras.confirmar_recepcion(conn, rid, location_id=deposito, occurred_at=datetime.datetime(2026, 9, 1, 9))


def _producto(abrir, perfil, nombre="Yogur", cantidad=10, *, tipo="producto"):
    """Un producto con `cantidad` en el depósito principal, según el perfil. Con lotes, el 60 % entra en `L1` (vence
    2026-10-05) y el 40 % en `L2` (vence 2026-11-01): con `cantidad=10`, 6 y 4."""
    with abrir() as conn:
        pid = catalogo.create_producto(conn, nombre, precio_venta=100.0, precio_costo=60.0, tipo=tipo)
        if perfil.startswith("marcado"):
            vencimientos.marcar_vence(conn, pid, True)
        if perfil.endswith("con_lotes"):
            primero = Decimal(str(cantidad)) * Decimal("0.6")
            _recibir(conn, pid, [("L1", datetime.datetime(2026, 10, 5), primero),
                                 ("L2", datetime.datetime(2026, 11, 1), Decimal(str(cantidad)) - primero)],
                     catalogo.get_default_deposito_id(conn))
        else:
            stock.add_movimiento_stock(conn, pid, "entrada", cantidad, "carga", fecha=PREVIA)
        conn.commit()
    return pid


def _depositos(abrir):
    """`(principal, norte)`: el depósito por defecto y uno más, vacío."""
    with abrir() as conn:
        norte = catalogo.create_deposito(conn, "Norte")
        conn.commit()
        return catalogo.get_default_deposito_id(conn), norte


def _marca(abrir) -> int:
    """El último id del ledger: lo que escriba la operación va después."""
    with abrir() as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM stock_movements").fetchone()[0]


def _ledger(conn, desde: int = 0) -> list[tuple]:
    """Lo escrito en el ledger después de la marca `desde`: todas las columnas de `COLUMNAS`, en orden de escritura."""
    filas = conn.execute(f"SELECT {COLUMNAS} FROM stock_movements WHERE id > ? ORDER BY id", (desde,)).fetchall()
    return [tuple(float(f[i]) if i == 1 else f[i] for i in range(len(COLUMNAS.split(",")))) for f in filas]


def _linea(pid, qty, *, precio=100.0, **extra):
    return {"nombre": "Yogur", "qty": qty, "precio": precio, "subtotal": round(qty * precio, 2),
            "producto_id": pid, **extra}


def _vender(abrir, items, *, deposito=None, hooks=None, usuario=USUARIO["id"]):
    """Una venta de mostrador por el camino real de `POST /api/ventas` (`erp.ventas.crear_venta_directa`)."""
    total = round(sum(i["subtotal"] for i in items), 2)
    pagos = [{"medio": "efectivo", "monto": total, "estado": "aprobado"}]
    kw = {"hooks": hooks} if hooks is not None else {}
    return ventas.crear_venta_directa(
        abrir, fecha=FECHA_VENTA, items=items, subtotal=total, descuento=0.0, total=total, cliente_id=None,
        cliente_nombre="", usuario_id=usuario, observaciones="", estado=ventas.estado_segun_pagos(total, pagos),
        pagos=pagos, stock_habilitado=True, deposito_id=deposito, **kw,
    )


def _lineas(abrir, vid) -> list[int]:
    """Los ids de `sale_items` de una venta, en el orden en que se cargaron."""
    with abrir() as conn:
        return [f[0] for f in conn.execute("SELECT id FROM sale_items WHERE sale_id = ? ORDER BY id", (vid,))]


def _anular(abrir, vid, **kw):
    with abrir() as conn:
        resultado = ventas.anular_venta(conn, vid, **kw)
        conn.commit()
    return resultado


def _devolver(abrir, vid, pedido, deposito, **kw):
    with abrir() as conn:
        resultado = ventas.devolver_items(conn, vid, pedido, deposito, **kw)
        conn.commit()
    return resultado


def _stock(abrir, pid, deposito=None, variante=None) -> float:
    with abrir() as conn:
        return stock.get_stock_actual(conn, pid, deposito, variante)


def _receta(plato, insumos):
    """El gancho `resolver_receta` de Restolibra: `plato` se descuenta por `insumos` (`[(item_id, cantidad), ...]`)."""
    def resolver(item_id, item):
        if item_id == plato:
            return [Insumo(item_id=i, cantidad=Decimal(c)) for i, c in insumos]
        return None
    return Hooks(resolver_receta=resolver)


# ═══════════════════════════════════════════════════════ La venta ════════


def test_venta_simple(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    dep, _ = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 3)])
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("sale", -3.0, dep, pid, None, "venta", "venta", vid, None, None, f"Venta ID {vid}", 7,
             "2026-09-10T00:00:00", None),
        ]
    assert _stock(abrir_ledger, pid) == 7.0


def test_venta_con_varias_lineas_no_agrupa_ni_toca_servicios(abrir_ledger, perfil):
    """Una fila por línea de producto, en el orden de la venta: el mismo producto en dos líneas son DOS filas; un
    servicio y una línea libre (sin `producto_id`) no escriben nada."""
    a = _producto(abrir_ledger, perfil, "Yogur")
    b = _producto(abrir_ledger, perfil, "Leche")
    with abrir_ledger() as conn:
        servicio = catalogo.create_producto(conn, "Flete", precio_venta=500.0, tipo="servicio")
        conn.commit()
    dep, _ = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    vid = _vender(abrir_ledger, [
        _linea(a, 2), _linea(b, 1.5), _linea(servicio, 1, precio=500.0), _linea(a, 1),
        {"nombre": "Envío", "qty": 1, "precio": 300.0, "subtotal": 300.0, "producto_id": None},
    ])
    nota = f"Venta ID {vid}"
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("sale", -2.0, dep, a, None, "venta", "venta", vid, None, None, nota, 7, "2026-09-10T00:00:00", None),
            ("sale", -1.5, dep, b, None, "venta", "venta", vid, None, None, nota, 7, "2026-09-10T00:00:00", None),
            ("sale", -1.0, dep, a, None, "venta", "venta", vid, None, None, nota, 7, "2026-09-10T00:00:00", None),
        ]
        assert [f[0] for f in conn.execute("SELECT kind FROM sale_items WHERE sale_id = ? ORDER BY id", (vid,))] == [
            "product", "product", "product", "product", "service"]   # el servicio del catálogo es 'product' en la línea
    assert (_stock(abrir_ledger, a), _stock(abrir_ledger, b), _stock(abrir_ledger, servicio)) == (7.0, 8.5, 0.0)


def test_venta_con_receta_una_fila_por_insumo_y_ninguna_por_el_plato(abrir_ledger, perfil):
    """El gancho `resolver_receta` (Restolibra): el plato se descuenta por sus insumos, receta × cantidad, con la nota
    `(receta)` y sin variante. Un ítem sin receta en la misma venta sale como siempre."""
    i1 = _producto(abrir_ledger, perfil, "Pan", 20)
    i2 = _producto(abrir_ledger, perfil, "Carne", 20)
    plato = _producto(abrir_ledger, "sin_marcar", "Hamburguesa", 0)
    suelto = _producto(abrir_ledger, perfil, "Gaseosa", 10)
    dep, _ = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(plato, 3), _linea(suelto, 2)],
                  hooks=_receta(plato, [(i1, "0.5"), (i2, "2")]))
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("sale", -1.5, dep, i1, None, "venta", "venta", vid, None, None, f"Venta ID {vid} (receta)", 7,
             "2026-09-10T00:00:00", None),
            ("sale", -6.0, dep, i2, None, "venta", "venta", vid, None, None, f"Venta ID {vid} (receta)", 7,
             "2026-09-10T00:00:00", None),
            ("sale", -2.0, dep, suelto, None, "venta", "venta", vid, None, None, f"Venta ID {vid}", 7,
             "2026-09-10T00:00:00", None),
        ]
    assert (_stock(abrir_ledger, i1), _stock(abrir_ledger, i2), _stock(abrir_ledger, plato)) == (18.5, 14.0, 0.0)


def test_venta_que_excede_el_stock_queda_negativa_y_no_se_frena(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    dep, _ = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 14)])
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("sale", -14.0, dep, pid, None, "venta", "venta", vid, None, None, f"Venta ID {vid}", 7,
             "2026-09-10T00:00:00", None),
        ]
        assert stock.get_stock_por_deposito(conn) == {pid: {dep: -4.0}}


def test_venta_con_deposito_descuenta_de_ese_deposito(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    dep, norte = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 3)], deposito=norte)
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("sale", -3.0, norte, pid, None, "venta", "venta", vid, None, None, f"Venta ID {vid}", 7,
             "2026-09-10T00:00:00", None),
        ]
        assert stock.get_stock_por_deposito(conn) == {pid: {dep: 10.0, norte: -3.0}}


def test_venta_con_receta_y_deposito_descuenta_los_insumos_de_ese_deposito(abrir_ledger, perfil):
    insumo = _producto(abrir_ledger, perfil, "Pan", 20)
    plato = _producto(abrir_ledger, "sin_marcar", "Tostado", 0)
    _, norte = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(plato, 2)], deposito=norte, hooks=_receta(plato, [(insumo, "2")]))
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("sale", -4.0, norte, insumo, None, "venta", "venta", vid, None, None, f"Venta ID {vid} (receta)", 7,
             "2026-09-10T00:00:00", None),
        ]


def test_venta_con_variante_viaja_a_la_fila(abrir_ledger, perfil):
    """El `variante_id` de la línea llega a `stock_movements.variant_id`, y el saldo de la variante baja solo."""
    pid = _producto(abrir_ledger, perfil)
    with abrir_ledger() as conn:
        variante = catalogo.create_variante(conn, pid, "Y-1", "Frutilla")["id"]
        stock.add_movimiento_stock(conn, pid, "entrada", 6, "carga variante", fecha=PREVIA, variant_id=variante)
        conn.commit()
    dep, _ = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 2, variante_id=variante)])
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("sale", -2.0, dep, pid, variante, "venta", "venta", vid, None, None, f"Venta ID {vid}", 7,
             "2026-09-10T00:00:00", None),
        ]
    assert (_stock(abrir_ledger, pid, variante=variante), _stock(abrir_ledger, pid)) == (4.0, 14.0)


# ═══════════════════════════════════════════════════════ La anulación ════


def test_anulacion_repone_fila_por_fila(abrir_ledger, perfil):
    """Una reposición por CADA fila de venta, en el orden en que se escribieron, con la nota y el `created_by` de
    quien anula. Anular dos veces no escribe nada la segunda."""
    a = _producto(abrir_ledger, perfil, "Yogur")
    b = _producto(abrir_ledger, perfil, "Leche")
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(a, 2), _linea(b, 1.5), _linea(a, 3)])
    antes = _marca(abrir_ledger)
    assert _anular(abrir_ledger, vid, usuario_id=7) is True
    nota = f"Anulación venta ID {vid}"
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("return", 2.0, dep, a, None, "anulacion", "venta", vid, None, None, nota, 7, "2026-09-20T00:00:00", None),
            ("return", 1.5, dep, b, None, "anulacion", "venta", vid, None, None, nota, 7, "2026-09-20T00:00:00", None),
            ("return", 3.0, dep, a, None, "anulacion", "venta", vid, None, None, nota, 7, "2026-09-20T00:00:00", None),
        ]
        assert ventas.obtener_venta(conn, vid)["estado"] == "anulada"
    assert (_stock(abrir_ledger, a), _stock(abrir_ledger, b)) == (10.0, 10.0)
    despues = _marca(abrir_ledger)
    assert _anular(abrir_ledger, vid, usuario_id=7) is False
    with abrir_ledger() as conn:
        assert _ledger(conn, despues) == []


def test_anulacion_sin_usuario_deja_created_by_en_none(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 4)])
    antes = _marca(abrir_ledger)
    assert _anular(abrir_ledger, vid) is True
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("return", 4.0, dep, pid, None, "anulacion", "venta", vid, None, None, f"Anulación venta ID {vid}", None,
             "2026-09-20T00:00:00", None),
        ]


def test_anulacion_con_deposito_repone_en_el_deposito_que_descuento(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    dep, norte = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 4)], deposito=norte)
    antes = _marca(abrir_ledger)
    assert _anular(abrir_ledger, vid, usuario_id=7) is True
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("return", 4.0, norte, pid, None, "anulacion", "venta", vid, None, None, f"Anulación venta ID {vid}", 7,
             "2026-09-20T00:00:00", None),
        ]
        assert stock.get_stock_por_deposito(conn) == {pid: {dep: 10.0, norte: 0.0}}


def test_anulacion_con_receta_repone_un_insumo_por_fila_sin_agrupar(abrir_ledger, perfil):
    """Dos platos que descuentan el MISMO insumo: dos filas de venta y dos de reposición (no una sumada)."""
    insumo = _producto(abrir_ledger, perfil, "Pan", 20)
    plato_a = _producto(abrir_ledger, "sin_marcar", "Plato A", 0)
    plato_b = _producto(abrir_ledger, "sin_marcar", "Plato B", 0)
    dep, _ = _depositos(abrir_ledger)

    def resolver(item_id, item):
        return [Insumo(item_id=insumo, cantidad=Decimal("1"))] if item_id in (plato_a, plato_b) else None

    vid = _vender(abrir_ledger, [_linea(plato_a, 2), _linea(plato_b, 3)], hooks=Hooks(resolver_receta=resolver))
    antes = _marca(abrir_ledger)
    assert _anular(abrir_ledger, vid, usuario_id=7) is True
    nota = f"Anulación venta ID {vid}"
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("return", 2.0, dep, insumo, None, "anulacion", "venta", vid, None, None, nota, 7,
             "2026-09-20T00:00:00", None),
            ("return", 3.0, dep, insumo, None, "anulacion", "venta", vid, None, None, nota, 7,
             "2026-09-20T00:00:00", None),
        ]
    assert _stock(abrir_ledger, insumo) == 20.0


def test_anulacion_con_variante_repone_la_misma_variante(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    with abrir_ledger() as conn:
        variante = catalogo.create_variante(conn, pid, "Y-1", "Frutilla")["id"]
        stock.add_movimiento_stock(conn, pid, "entrada", 6, "carga variante", fecha=PREVIA, variant_id=variante)
        conn.commit()
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 2, variante_id=variante)])
    antes = _marca(abrir_ledger)
    assert _anular(abrir_ledger, vid, usuario_id=7) is True
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("return", 2.0, dep, pid, variante, "anulacion", "venta", vid, None, None, f"Anulación venta ID {vid}", 7,
             "2026-09-20T00:00:00", None),
        ]
    assert _stock(abrir_ledger, pid, variante=variante) == 6.0


# ═══════════════════════════════════════════════════════ La devolución ═══


def test_devolucion_parcial_repone_con_tipo_devolucion(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 4)])
    (linea,) = _lineas(abrir_ledger, vid)
    antes = _marca(abrir_ledger)
    resultado = _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7)
    assert (resultado["importe"], resultado["venta"]["estado"]) == (100.0, "devuelta_parcial")
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("return", 1.0, dep, pid, None, "devolucion", "venta", vid, None, None, f"Devolución venta ID {vid}", 7,
             "2026-09-20T00:00:00", None),
        ]
    assert _stock(abrir_ledger, pid) == 7.0


def test_devolucion_a_otro_deposito_repone_donde_se_le_dice(abrir_ledger, perfil):
    """El depósito de la devolución es un parámetro, no el de la venta: la fila cae en el que se pidió."""
    pid = _producto(abrir_ledger, perfil)
    dep, norte = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 4)])
    (linea,) = _lineas(abrir_ledger, vid)
    antes = _marca(abrir_ledger)
    _devolver(abrir_ledger, vid, {linea: 2.0}, norte)
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("return", 2.0, norte, pid, None, "devolucion", "venta", vid, None, None, f"Devolución venta ID {vid}",
             None, "2026-09-20T00:00:00", None),
        ]
        assert stock.get_stock_por_deposito(conn) == {pid: {dep: 6.0, norte: 2.0}}


def test_devolucion_de_dos_lineas_escribe_una_fila_por_linea_en_el_orden_pedido(abrir_ledger, perfil):
    a = _producto(abrir_ledger, perfil, "Yogur")
    b = _producto(abrir_ledger, perfil, "Leche")
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(a, 2), _linea(b, 3)])
    la, lb = _lineas(abrir_ledger, vid)
    antes = _marca(abrir_ledger)
    resultado = _devolver(abrir_ledger, vid, {lb: 1.0, la: 2.0}, dep, usuario_id=7)
    assert (resultado["importe"], resultado["venta"]["estado"]) == (300.0, "devuelta_parcial")
    nota = f"Devolución venta ID {vid}"
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("return", 1.0, dep, b, None, "devolucion", "venta", vid, None, None, nota, 7, "2026-09-20T00:00:00", None),
            ("return", 2.0, dep, a, None, "devolucion", "venta", vid, None, None, nota, 7, "2026-09-20T00:00:00", None),
        ]


def test_devolucion_acumulada_respeta_el_tope_y_no_deja_filas_a_medias(abrir_ledger, perfil):
    """El tope es por (ítem, variante) sobre TODAS las líneas, restando lo ya devuelto. Una devolución que no pasa no
    deja ninguna fila (la transacción del caller se revierte: `devolver_items` escribe línea por línea)."""
    pid = _producto(abrir_ledger, perfil)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 3), _linea(pid, 2)])
    l1, l2 = _lineas(abrir_ledger, vid)
    antes = _marca(abrir_ledger)
    assert _devolver(abrir_ledger, vid, {l1: 2.0}, dep, usuario_id=7)["venta"]["estado"] == "devuelta_parcial"
    assert _devolver(abrir_ledger, vid, {l2: 2.0}, dep, usuario_id=7)["venta"]["estado"] == "devuelta_parcial"
    quedaron = _marca(abrir_ledger)
    with pytest.raises(ValueError, match=r"quedan 1\.0 sin devolver"):
        _devolver(abrir_ledger, vid, {l1: 2.0}, dep, usuario_id=7)
    with pytest.raises(ValueError, match=r"quedan 0\.0 sin devolver"):      # lo pedido en ESTA llamada también cuenta
        _devolver(abrir_ledger, vid, {l1: 1.0, l2: 2.0}, dep, usuario_id=7)   # la primera pasa, la segunda no
    with abrir_ledger() as conn:
        assert _ledger(conn, quedaron) == []
    assert _devolver(abrir_ledger, vid, {l2: 1.0}, dep, usuario_id=7)["venta"]["estado"] == "devuelta"
    nota = f"Devolución venta ID {vid}"
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("return", 2.0, dep, pid, None, "devolucion", "venta", vid, None, None, nota, 7, "2026-09-20T00:00:00", None),
            ("return", 2.0, dep, pid, None, "devolucion", "venta", vid, None, None, nota, 7, "2026-09-20T00:00:00", None),
            ("return", 1.0, dep, pid, None, "devolucion", "venta", vid, None, None, nota, 7, "2026-09-20T00:00:00", None),
        ]
    assert _stock(abrir_ledger, pid) == 10.0


def test_una_venta_con_devoluciones_no_se_anula_y_no_escribe(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 4)])
    (linea,) = _lineas(abrir_ledger, vid)
    _devolver(abrir_ledger, vid, {linea: 1.0}, dep)
    despues = _marca(abrir_ledger)
    with pytest.raises(ventas.VentaConDevoluciones):
        _anular(abrir_ledger, vid)
    with abrir_ledger() as conn:
        assert _ledger(conn, despues) == []


def test_devolucion_con_variante_repone_la_misma_variante(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    with abrir_ledger() as conn:
        variante = catalogo.create_variante(conn, pid, "Y-1", "Frutilla")["id"]
        stock.add_movimiento_stock(conn, pid, "entrada", 6, "carga variante", fecha=PREVIA, variant_id=variante)
        conn.commit()
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 3, variante_id=variante)])
    (linea,) = _lineas(abrir_ledger, vid)
    antes = _marca(abrir_ledger)
    _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7)
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("return", 1.0, dep, pid, variante, "devolucion", "venta", vid, None, None, f"Devolución venta ID {vid}",
             7, "2026-09-20T00:00:00", None),
        ]


def test_devolucion_de_una_venta_con_receta_repone_el_plato_y_no_los_insumos(abrir_ledger, perfil):
    """Documentado en `devolver_items` («no es receta-aware»), y fijado acá porque A-4 no puede asumir lo contrario:
    la venta descontó el INSUMO, pero la devolución repone el PLATO (un ítem que nunca tuvo stock) y deja al insumo
    sin reponer."""
    insumo = _producto(abrir_ledger, perfil, "Pan", 20)
    plato = _producto(abrir_ledger, "sin_marcar", "Tostado", 0)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(plato, 3)], hooks=_receta(plato, [(insumo, "0.5")]))
    (linea,) = _lineas(abrir_ledger, vid)
    antes = _marca(abrir_ledger)
    _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7)
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("return", 1.0, dep, plato, None, "devolucion", "venta", vid, None, None, f"Devolución venta ID {vid}", 7,
             "2026-09-20T00:00:00", None),
        ]
    assert (_stock(abrir_ledger, insumo), _stock(abrir_ledger, plato)) == (18.5, 1.0)


def test_devolucion_de_un_servicio_del_catalogo_repone_stock_de_un_servicio(abrir_ledger):
    """Comportamiento sorprendente, fijado tal cual: una línea con un servicio DEL CATÁLOGO queda en `sale_items` como
    `kind='product'` (el `kind` sólo mira si hay `producto_id`), la venta no escribe stock (`_es_servicio`) pero
    `devolver_items` la trata como producto y escribe +1 de `devolucion` sobre un ítem sin inventario."""
    with abrir_ledger() as conn:
        servicio = catalogo.create_producto(conn, "Flete", precio_venta=500.0, tipo="servicio")
        conn.commit()
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 2, precio=500.0)])
    (linea,) = _lineas(abrir_ledger, vid)
    with abrir_ledger() as conn:
        assert _ledger(conn) == []                 # la venta no escribió nada
    _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7)
    with abrir_ledger() as conn:
        assert _ledger(conn) == [
            ("return", 1.0, dep, servicio, None, "devolucion", "venta", vid, None, None, f"Devolución venta ID {vid}",
             7, "2026-09-20T00:00:00", None),
        ]


# ═══════════════════════════════════════════════════════ La transferencia ═


def test_transferencia_entre_depositos_es_un_par_con_source_id_de_la_salida(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    dep, norte = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    with abrir_ledger() as conn:
        catalogo.transferir_stock(conn, pid, dep, norte, 4, usuario_id=7, fecha="2026-09-12")
        conn.commit()
    with abrir_ledger() as conn:
        (salida_id, entrada_id) = [f[0] for f in conn.execute(
            "SELECT id FROM stock_movements WHERE id > ? ORDER BY id", (antes,))]
        assert _ledger(conn, antes) == [
            ("transfer_out", -4.0, dep, pid, None, "transferencia_salida", "transfer", None, None, None,
             "Transferencia entre depósitos", 7, "2026-09-12T00:00:00", None),
            ("transfer_in", 4.0, norte, pid, None, "transferencia_entrada", "transfer", salida_id, None, None,
             "Transferencia entre depósitos", 7, "2026-09-12T00:00:00", None),
        ]
        assert entrada_id == salida_id + 1
        assert catalogo.get_transferencias(conn) == [{
            "id": salida_id, "producto_id": pid, "producto": "Yogur", "variant_id": None, "cantidad": 4.0,
            "origen_id": dep, "origen": "Depósito principal", "destino_id": norte, "destino": "Norte",
            "fecha": "2026-09-12T00:00:00", "observaciones": "Transferencia entre depósitos", "usuario_id": 7,
        }]
        assert stock.get_stock_por_deposito(conn) == {pid: {dep: 6.0, norte: 4.0}}


def test_transferencia_con_observaciones_y_variante(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    with abrir_ledger() as conn:
        variante = catalogo.create_variante(conn, pid, "Y-1", "Frutilla")["id"]
        stock.add_movimiento_stock(conn, pid, "entrada", 6, "carga variante", fecha=PREVIA, variant_id=variante)
        conn.commit()
    dep, norte = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    with abrir_ledger() as conn:
        catalogo.transferir_stock(conn, pid, dep, norte, 2.5, fecha="2026-09-12", observaciones="Reposición norte",
                                  variant_id=variante)
        conn.commit()
    with abrir_ledger() as conn:
        (salida_id, _) = [f[0] for f in conn.execute("SELECT id FROM stock_movements WHERE id > ? ORDER BY id",
                                                     (antes,))]
        assert _ledger(conn, antes) == [
            ("transfer_out", -2.5, dep, pid, variante, "transferencia_salida", "transfer", None, None, None,
             "Reposición norte", None, "2026-09-12T00:00:00", None),
            ("transfer_in", 2.5, norte, pid, variante, "transferencia_entrada", "transfer", salida_id, None, None,
             "Reposición norte", None, "2026-09-12T00:00:00", None),
        ]
        assert [(t["variant_id"], t["cantidad"], t["observaciones"]) for t in catalogo.get_transferencias(conn)] == [
            (variante, 2.5, "Reposición norte")]


def test_transferencia_sin_stock_en_el_origen_es_valueerror_y_no_escribe(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    dep, norte = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    with abrir_ledger() as conn:
        with pytest.raises(ValueError, match=r"Stock insuficiente en depósito origen \(disponible: 10\.0\)\."):
            catalogo.transferir_stock(conn, pid, dep, norte, 11, fecha="2026-09-12")
        with pytest.raises(ValueError, match=r"disponible: 0\.0"):
            catalogo.transferir_stock(conn, pid, norte, dep, 1, fecha="2026-09-12")
        assert _ledger(conn, antes) == []
        catalogo.transferir_stock(conn, pid, dep, norte, 10, fecha="2026-09-12")      # todo lo que hay, sí pasa
        conn.commit()
    with abrir_ledger() as conn:
        assert stock.get_stock_por_deposito(conn) == {pid: {dep: 0.0, norte: 10.0}}


# ═══════════════════════════════════════════════════════ El ajuste ═══════


def test_ajuste_positivo_y_negativo_escriben_la_diferencia(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    dep, _ = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    with abrir_ledger() as conn:
        stock.ajustar_stock(conn, pid, 15.0, "conteo", usuario_id=7, fecha="2026-09-12")      # +5
        stock.ajustar_stock(conn, pid, 15.0, "sin cambios", usuario_id=7, fecha="2026-09-12")  # delta 0: no escribe
        stock.ajustar_stock(conn, pid, 8.0, "rotura", fecha="2026-09-13")                      # −7, sin usuario
        conn.commit()
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("adjustment", 5.0, dep, pid, None, "ajuste", None, None, None, None, "conteo", 7,
             "2026-09-12T00:00:00", None),
            ("adjustment", -7.0, dep, pid, None, "ajuste", None, None, None, None, "rotura", None,
             "2026-09-13T00:00:00", None),
        ]
    assert _stock(abrir_ledger, pid) == 8.0


def test_ajuste_con_deposito_y_con_variante_mira_ese_saldo(abrir_ledger, perfil):
    pid = _producto(abrir_ledger, perfil)
    with abrir_ledger() as conn:
        variante = catalogo.create_variante(conn, pid, "Y-1", "Frutilla")["id"]
        stock.add_movimiento_stock(conn, pid, "entrada", 6, "carga variante", fecha=PREVIA, variant_id=variante)
        conn.commit()
    dep, norte = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    with abrir_ledger() as conn:
        stock.ajustar_stock(conn, pid, 3.0, "conteo norte", usuario_id=7, fecha="2026-09-12", deposito_id=norte)
        stock.ajustar_stock(conn, pid, 4.0, "conteo variante", fecha="2026-09-12", variant_id=variante)
        conn.commit()
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("adjustment", 3.0, norte, pid, None, "ajuste", None, None, None, None, "conteo norte", 7,
             "2026-09-12T00:00:00", None),
            ("adjustment", -2.0, dep, pid, variante, "ajuste", None, None, None, None, "conteo variante", None,
             "2026-09-12T00:00:00", None),
        ]


def test_ajuste_sin_deposito_compara_contra_el_total_y_escribe_en_el_default(abrir_ledger, perfil):
    """Sin `deposito_id`, `ajustar_stock` lleva el TOTAL (suma de todos los depósitos) al valor pedido, pero la fila
    cae en el depósito por defecto, no en el que tenga la diferencia."""
    pid = _producto(abrir_ledger, perfil)
    dep, norte = _depositos(abrir_ledger)
    with abrir_ledger() as conn:
        catalogo.transferir_stock(conn, pid, dep, norte, 4, fecha="2026-09-12")
        conn.commit()
    antes = _marca(abrir_ledger)
    with abrir_ledger() as conn:
        stock.ajustar_stock(conn, pid, 12.0, "conteo", usuario_id=7, fecha="2026-09-13")
        conn.commit()
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("adjustment", 2.0, dep, pid, None, "ajuste", None, None, None, None, "conteo", 7,
             "2026-09-13T00:00:00", None),
        ]
        assert stock.get_stock_por_deposito(conn) == {pid: {dep: 8.0, norte: 4.0}}


def test_salida_y_merma_directas_no_llevan_lote(abrir_ledger, perfil):
    """Los tipos `salida` y `merma` de `add_movimiento_stock` (la pantalla de movimientos de Contalibra y Restolibra)."""
    pid = _producto(abrir_ledger, perfil)
    dep, _ = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    with abrir_ledger() as conn:
        stock.add_movimiento_stock(conn, pid, "salida", -2, "uso interno", fecha="2026-09-12", usuario_id=7)
        stock.add_movimiento_stock(conn, pid, "merma", -1, "Merma: rotura", fecha="2026-09-12")
        conn.commit()
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("adjustment", -2.0, dep, pid, None, "salida", None, None, None, None, "uso interno", 7,
             "2026-09-12T00:00:00", None),
            ("waste", -1.0, dep, pid, None, "merma", None, None, None, None, "Merma: rotura", None,
             "2026-09-12T00:00:00", None),
        ]


# ═══════════════════════════ Todo junto: los tres perfiles escriben lo mismo, y nadie manda lote ═══


def _bateria(abrir, pid, dep, norte):
    """Una corrida de todas las salidas sobre `pid` (venta, venta a otro depósito, anulación, devolución,
    transferencia, ajustes)."""
    _vender(abrir, [_linea(pid, 3)])
    otro = _vender(abrir, [_linea(pid, 2)], deposito=norte)
    _anular(abrir, otro, usuario_id=7)
    tercera = _vender(abrir, [_linea(pid, 4), _linea(pid, 1)])
    primera, _ = _lineas(abrir, tercera)
    _devolver(abrir, tercera, {primera: 1.0}, dep, usuario_id=7)
    with abrir() as conn:
        catalogo.transferir_stock(conn, pid, dep, norte, 1, usuario_id=7, fecha="2026-09-12")
        stock.ajustar_stock(conn, pid, 9.0, "conteo", usuario_id=7, fecha="2026-09-13")
        stock.ajustar_stock(conn, pid, 2.0, "rotura", fecha="2026-09-14", deposito_id=norte)
        stock.add_movimiento_stock(conn, pid, "merma", -1, "Merma: rotura", fecha="2026-09-15")
        conn.commit()


def _normalizado(conn, desde, pid):
    """El ledger desde `desde` sin lo que cambia de un producto a otro: el producto y los ids de venta / de la salida
    (que se reemplazan por su orden de aparición)."""
    ids: dict = {}
    salida = []
    for f in _ledger(conn, desde):
        fila = list(f)
        fila[3] = "P" if f[3] == pid else f[3]
        if f[7] is not None:
            fila[7] = ids.setdefault(f[7], len(ids) + 1)
        fila[10] = re.sub(r"\d+", "#", f[10])
        salida.append(tuple(fila))
    return salida


def test_los_tres_perfiles_escriben_exactamente_el_mismo_ledger(abrir_ledger):
    """Un producto sin marcar, uno sin marcar con lotes de recepción y uno marcado sin lotes, sometidos a la misma
    batería: la misma lista de filas (salvo ids). Un marcado sin lotes se comporta igual."""
    dep, norte = _depositos(abrir_ledger)
    ledgers = {}
    for p in PERFILES:
        pid = _producto(abrir_ledger, p, f"Yogur {p}")
        antes = _marca(abrir_ledger)
        _bateria(abrir_ledger, pid, dep, norte)
        with abrir_ledger() as conn:
            ledgers[p] = _normalizado(conn, antes, pid)
            assert stock.get_stock_actual(conn, pid) == 9.0   # 10 −3 (venta) −5 +1 (devolución) +6 +1 (ajustes) −1 (merma)
    assert len(ledgers["sin_marcar"]) == 11
    assert ledgers["marcado_sin_lotes"] == ledgers["sin_marcar"]
    assert ledgers["sin_marcar_con_lotes"] == ledgers["sin_marcar"]
    assert all(f[8] is None and f[9] is None for f in ledgers["sin_marcar"])


class _Espia:
    """Anota cada llamada a `stock.add_movimiento_stock` con TODOS sus argumentos (los defaults incluidos)."""

    def __init__(self, monkeypatch):
        self.original = stock.add_movimiento_stock
        self.firma = inspect.signature(self.original)
        self.llamadas: list[dict] = []
        self.movimientos_del_repositorio: list = []
        monkeypatch.setattr(stock, "add_movimiento_stock", self)
        monkeypatch.setattr(ventas, "add_movimiento_stock", self)   # `erp.ventas` lo importó por nombre
        append = SqliteCommerceRepository.append_stock_movement

        def espiar_repositorio(repo, movimiento):
            self.movimientos_del_repositorio.append(movimiento)
            return append(repo, movimiento)

        monkeypatch.setattr(SqliteCommerceRepository, "append_stock_movement", espiar_repositorio)

    def __call__(self, *args, **kwargs):
        ligados = self.firma.bind(*args, **kwargs)
        ligados.apply_defaults()
        self.llamadas.append(dict(ligados.arguments))
        return self.original(*args, **kwargs)

    def con_lote(self):
        return [c for c in self.llamadas if c["lot_code"] is not None or c["expires_at"] is not None]


def test_para_un_producto_sin_marcar_nunca_se_manda_lote_ni_vencimiento(abrir_ledger, perfil, monkeypatch):
    """El espía sobre `add_movimiento_stock` (y sobre el `append_stock_movement` del repositorio, por donde pasa la
    transferencia) no ve NUNCA un `lot_code` ni un `expires_at` distinto de `None` en ninguna de las salidas."""
    pid = _producto(abrir_ledger, perfil, cantidad=20)
    plato = _producto(abrir_ledger, "sin_marcar", "Plato", 0)
    dep, norte = _depositos(abrir_ledger)
    espia = _Espia(monkeypatch)
    _bateria(abrir_ledger, pid, dep, norte)
    _vender(abrir_ledger, [_linea(plato, 2)], hooks=_receta(plato, [(pid, "1.5")]))
    assert {c["tipo"] for c in espia.llamadas} == {"venta", "anulacion", "devolucion", "ajuste", "merma"}
    assert len(espia.llamadas) >= 10
    assert espia.con_lote() == []
    assert len(espia.movimientos_del_repositorio) == 2          # el par de la transferencia
    assert all(m.lot_code is None and m.expires_at is None for m in espia.movimientos_del_repositorio)


def test_el_espia_ve_un_lote_si_alguien_lo_manda(abrir_ledger, monkeypatch):
    """Para que el test de arriba no pase por un espía muerto: con un lote explícito, `con_lote()` no queda vacío."""
    pid = _producto(abrir_ledger, "sin_marcar")
    espia = _Espia(monkeypatch)
    with abrir_ledger() as conn:
        stock.add_movimiento_stock(conn, pid, "venta", -1, "con lote", fecha="2026-09-12", lot_code="L1",
                                   expires_at="2026-10-05")
        stock.add_movimiento_stock(conn, pid, "venta", -1, "sin lote", fecha="2026-09-12")
        conn.commit()
    assert [(c["lot_code"], c["expires_at"]) for c in espia.con_lote()] == [("L1", "2026-10-05")]
    assert len(espia.llamadas) == 2


class _Grabadora:
    """Una conexión (o un cursor) que anota los `INSERT` a `stock_movements` que se le ejecutan y delega todo lo
    demás. El repositorio escribe por `conn.cursor().execute`, así que `cursor()` devuelve otra grabadora."""

    def __init__(self, conn, registro):
        self._conn = conn
        self._registro = registro

    def execute(self, sql, params=()):
        if "INSERT INTO stock_movements" in sql:
            self._registro.append((sql, tuple(params)))
        return self._conn.execute(sql, params)

    def cursor(self):
        return _Grabadora(self._conn.cursor(), self._registro)

    def __getattr__(self, nombre):
        return getattr(self._conn, nombre)


def test_el_insert_sin_lote_sigue_siendo_el_de_siempre_en_cada_operacion(abrir_ledger, perfil):
    """El SQL carácter por carácter: venta, anulación, devolución, ajuste y merma escriben con el `INSERT` de 11
    columnas de siempre (sin `lot_code` ni `expires_at`). La transferencia pasa por el del repositorio."""
    pid = _producto(abrir_ledger, perfil)
    dep, norte = _depositos(abrir_ledger)
    registro: list[tuple[str, tuple]] = []

    @contextmanager
    def grabando():
        with abrir_ledger() as conn:
            yield _Grabadora(conn, registro)

    vid = _vender(grabando, [_linea(pid, 3)])
    (linea,) = _lineas(abrir_ledger, vid)
    with grabando() as g:
        ventas.devolver_items(g, vid, {linea: 1.0}, dep, usuario_id=7)
        g.commit()
    otra = _vender(abrir_ledger, [_linea(pid, 2)])
    with grabando() as g:
        ventas.anular_venta(g, otra, usuario_id=7)
        stock.ajustar_stock(g, pid, 9.0, "conteo", usuario_id=7, fecha="2026-09-13")
        stock.add_movimiento_stock(g, pid, "merma", -1, "Merma: rotura", fecha="2026-09-15")
        g.commit()
    assert [s for s, _ in registro] == [_INSERT_DE_SIEMPRE] * 5
    assert [(p[3], float(p[4]), p[5][:10], p[6], p[7], p[8], p[9], p[10]) for _, p in registro] == [
        ("sale", -3.0, "2026-09-10", "venta", vid, f"Venta ID {vid}", 7, "venta"),
        ("return", 1.0, FECHA_HOY, "venta", vid, f"Devolución venta ID {vid}", 7, "devolucion"),
        ("return", 2.0, FECHA_HOY, "venta", otra, f"Anulación venta ID {otra}", 7, "anulacion"),
        ("adjustment", 1.0, "2026-09-13", None, None, "conteo", 7, "ajuste"),
        ("waste", -1.0, "2026-09-15", None, None, "Merma: rotura", None, "merma"),
    ]
    transferencia: list[tuple[str, tuple]] = []
    with abrir_ledger() as conn:
        catalogo.transferir_stock(_Grabadora(conn, transferencia), pid, dep, norte, 1, usuario_id=7,
                                  fecha="2026-09-12")
        conn.commit()
    assert [s for s, _ in transferencia] == [_INSERT_DEL_REPOSITORIO] * 2
    assert [(p[3], p[9], p[10]) for _, p in transferencia] == [("transfer_out", None, None),
                                                                 ("transfer_in", None, None)]


# ═══════════════════ Comportamiento actual de un marcado CON lotes (A-4 lo cambia a propósito) ═══════════════════
#
# 🔴 Esta familia NO es una red de seguridad: documenta lo que hoy es una LIMITACIÓN (ADR-018, «hasta A-4»). Un producto
# marcado con lotes en el ledger vende, anula, devuelve, transfiere y ajusta SIN elegir lote: todas las filas van al
# bucket «sin lote» y los lotes quedan enteros, así que el saldo por lote sobreestima y el bucket queda negativo. El
# test hermano de VentaLibra dice lo mismo (`test_hasta_a4_una_venta_de_un_producto_marcado_descuenta_del_sin_lote`, en
# `ventalibra/tests/test_vencimientos.py`; ese test no se toca desde acá). Cuando A-4 haga FEFO hay que INVERTIR
# `_hoy_la_salida_no_descuenta_por_lote` —es el único lugar que afirma «los lotes quedan enteros»— y revisar las filas
# de ledger de cada test (hoy: `lot_code` y `expires_at` en `None`; después: una fila por lote consumido).


def _hoy_la_salida_no_descuenta_por_lote(conn, pid, *, dep, sin_lote, norte=None, sin_lote_norte=None):
    """EL punto a invertir en A-4. Hoy: `L1` (6) y `L2` (4) del depósito principal quedan intactos y todo lo que salió
    (o entró) sin elegir lote está en el bucket «sin lote» con el saldo `sin_lote` (`None` = el bucket ya no existe)."""
    saldos = {(f["deposito_id"], f["lote"]): f["saldo"] for f in vencimientos.lotes_de(conn, pid, hoy=HOY)}
    esperado = {(dep, "L1"): 6, (dep, "L2"): 4}
    if sin_lote is not None:
        esperado[(dep, None)] = sin_lote
    if sin_lote_norte is not None:
        esperado[(norte, None)] = sin_lote_norte
    assert saldos == esperado


def test_a4_cambia_la_venta_de_un_marcado_con_lotes_descuenta_del_sin_lote(abrir_ledger):
    pid = _producto(abrir_ledger, "marcado_con_lotes")
    dep, _ = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 3)])
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("sale", -3.0, dep, pid, None, "venta", "venta", vid, None, None, f"Venta ID {vid}", 7,
             "2026-09-10T00:00:00", None),
        ]
        _hoy_la_salida_no_descuenta_por_lote(conn, pid, dep=dep, sin_lote=-3)
        assert stock.get_stock_actual(conn, pid) == 7.0


def test_a4_cambia_vender_todo_de_un_marcado_con_lotes_deja_los_lotes_enteros_y_el_total_en_cero(abrir_ledger):
    pid = _producto(abrir_ledger, "marcado_con_lotes")
    dep, _ = _depositos(abrir_ledger)
    _vender(abrir_ledger, [_linea(pid, 10)])
    with abrir_ledger() as conn:
        _hoy_la_salida_no_descuenta_por_lote(conn, pid, dep=dep, sin_lote=-10)
        assert stock.get_stock_actual(conn, pid) == 0.0


def test_a4_cambia_la_venta_con_receta_descuenta_el_insumo_marcado_del_sin_lote(abrir_ledger):
    insumo = _producto(abrir_ledger, "marcado_con_lotes", "Pan")
    plato = _producto(abrir_ledger, "sin_marcar", "Tostado", 0)
    dep, _ = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(plato, 3)], hooks=_receta(plato, [(insumo, "0.5")]))
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("sale", -1.5, dep, insumo, None, "venta", "venta", vid, None, None, f"Venta ID {vid} (receta)", 7,
             "2026-09-10T00:00:00", None),
        ]
        _hoy_la_salida_no_descuenta_por_lote(conn, insumo, dep=dep, sin_lote=-1.5)


def test_a4_cambia_la_anulacion_repone_en_el_sin_lote_aunque_no_haya_salido_de_ahi_ningun_lote(abrir_ledger):
    pid = _producto(abrir_ledger, "marcado_con_lotes")
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 3)])
    antes = _marca(abrir_ledger)
    assert _anular(abrir_ledger, vid, usuario_id=7) is True
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("return", 3.0, dep, pid, None, "anulacion", "venta", vid, None, None, f"Anulación venta ID {vid}", 7,
             "2026-09-20T00:00:00", None),
        ]
        _hoy_la_salida_no_descuenta_por_lote(conn, pid, dep=dep, sin_lote=None)   # −3 + 3: el bucket se cancela


def test_a4_cambia_la_devolucion_repone_en_el_sin_lote(abrir_ledger):
    pid = _producto(abrir_ledger, "marcado_con_lotes")
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(pid, 3)])
    (linea,) = _lineas(abrir_ledger, vid)
    antes = _marca(abrir_ledger)
    _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7)
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("return", 1.0, dep, pid, None, "devolucion", "venta", vid, None, None, f"Devolución venta ID {vid}", 7,
             "2026-09-20T00:00:00", None),
        ]
        _hoy_la_salida_no_descuenta_por_lote(conn, pid, dep=dep, sin_lote=-2)


def test_a4_cambia_la_transferencia_mueve_el_sin_lote_y_no_los_lotes(abrir_ledger):
    pid = _producto(abrir_ledger, "marcado_con_lotes")
    dep, norte = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    with abrir_ledger() as conn:
        catalogo.transferir_stock(conn, pid, dep, norte, 4, usuario_id=7, fecha="2026-09-12")
        conn.commit()
    with abrir_ledger() as conn:
        (salida_id, _) = [f[0] for f in conn.execute("SELECT id FROM stock_movements WHERE id > ? ORDER BY id",
                                                     (antes,))]
        assert _ledger(conn, antes) == [
            ("transfer_out", -4.0, dep, pid, None, "transferencia_salida", "transfer", None, None, None,
             "Transferencia entre depósitos", 7, "2026-09-12T00:00:00", None),
            ("transfer_in", 4.0, norte, pid, None, "transferencia_entrada", "transfer", salida_id, None, None,
             "Transferencia entre depósitos", 7, "2026-09-12T00:00:00", None),
        ]
        _hoy_la_salida_no_descuenta_por_lote(conn, pid, dep=dep, sin_lote=-4, norte=norte, sin_lote_norte=4)


def test_a4_cambia_el_ajuste_de_un_marcado_con_lotes_escribe_en_el_sin_lote(abrir_ledger):
    pid = _producto(abrir_ledger, "marcado_con_lotes")
    dep, _ = _depositos(abrir_ledger)
    antes = _marca(abrir_ledger)
    with abrir_ledger() as conn:
        stock.ajustar_stock(conn, pid, 12.0, "conteo", usuario_id=7, fecha="2026-09-12")    # +2
        _hoy_la_salida_no_descuenta_por_lote(conn, pid, dep=dep, sin_lote=2)
        stock.ajustar_stock(conn, pid, 7.0, "rotura", fecha="2026-09-13")                   # −5
        _hoy_la_salida_no_descuenta_por_lote(conn, pid, dep=dep, sin_lote=-3)
        conn.commit()
    with abrir_ledger() as conn:
        assert _ledger(conn, antes) == [
            ("adjustment", 2.0, dep, pid, None, "ajuste", None, None, None, None, "conteo", 7,
             "2026-09-12T00:00:00", None),
            ("adjustment", -5.0, dep, pid, None, "ajuste", None, None, None, None, "rotura", None,
             "2026-09-13T00:00:00", None),
        ]
