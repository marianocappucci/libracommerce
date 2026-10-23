"""El vínculo propio del producto sobrevive a la venta que sube de un nodo offline (ADR-072).

El defecto que cierra (hallazgo 5 del relevamiento de LibraCargo y LibraClub en LibraEdge): el buffet de LibraClub ata el consumo a la
reserva con `sales.source_type = 'reserva'` y `sales.source_id = <reserva>`, y el aplicador de `venta.registrada` del central **pisaba** esas dos
columnas con `libraedge:<node_id>` / id de la venta en el nodo (su clave de idempotencia): el consumo dejaba de ser de su reserva.

Mismas dos bases que `test_erp_offline.py` (el nodo y el central, en las tres combinaciones de motores) y el `SyncReceiver` de verdad. El central
de estos tests trae la revisión `0010_vinculo_de_la_venta` salvo donde se dice lo contrario; su propio test, al final, corre la revisión de verdad.
"""

from __future__ import annotations

import json

import pytest
import test_vencimientos as _vto
from conftest import USUARIO
from test_erp_offline import HOY, NODO, _pendientes, _vender, puntas  # noqa: F401
from test_vencimientos import _con_conexion, _crear_base_al_dia_de_0001, migrar

from libracommerce.erp import SIN_GANCHOS, Hooks, offline, ventas

libraedge = pytest.importorskip("libraedge")

destino = _vto.destino

RESERVA = 7
EFECTIVO = [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}]
PARCIAL = [{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"},
           {"medio": "mercadopago_qr", "monto": 100.0, "estado": "pendiente"}]


# ── Las bases y los ganchos ─────────────────────────────────────────────────


def _revision_0010(conn):
    """Lo que hace la revisión `0010_vinculo_de_la_venta` (su propio test la corre de verdad, abajo)."""
    conn.execute(
        "CREATE TABLE venta_origen_nodo (node_id TEXT NOT NULL, venta_id_nodo BIGINT NOT NULL, "
        "venta_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE, PRIMARY KEY (node_id, venta_id_nodo))")
    conn.execute("CREATE UNIQUE INDEX idx_venta_origen_nodo_venta ON venta_origen_nodo(venta_id)")


@pytest.fixture
def bases(puntas):  # noqa: F811
    """El nodo y el central; el central con la revisión `0010`."""
    nodo, central = puntas
    with central.abrir() as conn:
        _revision_0010(conn)
        conn.commit()
    return nodo, central


def _hooks_del_producto(tipo="reserva", id_=RESERVA, extra=None):
    """Los ganchos de un producto en el nodo: al registrar la venta le pone su vínculo (`source_type` / `source_id`), como el buffet de
    LibraClub, y `extra_de_venta` lleva lo suyo."""

    def al_registrar(conn, venta):
        conn.execute("UPDATE sales SET source_type = ?, source_id = ? WHERE id = ?", (tipo, id_, venta["id"]))

    outbox = offline.Outbox(nodo=lambda: NODO, extra_de_venta=(lambda conn, venta: extra) if extra else None)
    return offline.con_outbox(Hooks(al_registrar_venta=al_registrar), outbox)


def _subir(central, operaciones, *, hooks=SIN_GANCHOS, al_materializar=None, tablas_de_origen=None):
    """Cada operación por el receptor de LibraEdge, con los aplicadores del central en su `Despachador`."""
    from libraedge.sync.despacho import Despachador
    from libraedge.sync.receiver import SyncReceiver

    despachador = offline.registrar_en(Despachador(), hooks, al_materializar=al_materializar,
                                       tablas_de_origen=tablas_de_origen)
    resultados = []
    with central.abrir() as conn:
        receptor = SyncReceiver(conn, despachador=despachador)
        for op in operaciones:
            resultados.append(receptor.accept(op))
    return resultados


def _reintentar(central, operacion, **kwargs):
    """Lo que hace `libraedge.sync.revision.reintentar` sobre una operación en revisión manual: aplicarla de nuevo."""
    from libraedge.sync.despacho import Despachador
    from libraedge.sync.receiver import SyncReceiver

    despachador = offline.registrar_en(Despachador(), **kwargs)
    with central.abrir() as conn:
        return SyncReceiver(conn, despachador=despachador).aplicar(operacion)


def _ventas_del_central(central):
    with central.abrir() as conn:
        return [tuple(f) for f in conn.execute(
            "SELECT number, source_type, source_id FROM sales ORDER BY id").fetchall()]


def _origenes(central):
    with central.abrir() as conn:
        return [tuple(f) for f in conn.execute(
            "SELECT node_id, venta_id_nodo, venta_id FROM venta_origen_nodo ORDER BY venta_id").fetchall()]


def _cuenta(base, tabla):
    with base.abrir() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


def _stock_del_central(central):
    from libracommerce.erp import stock

    with central.abrir() as conn:
        return float(stock.get_stock_actual(conn, 1))


# ── Lo que viaja ────────────────────────────────────────────────────────────


def test_sin_vinculo_el_payload_es_el_de_antes_y_la_venta_queda_marcada_del_nodo(bases):
    nodo, central = bases
    vid = _vender(nodo, offline.con_outbox(SIN_GANCHOS, offline.Outbox(nodo=lambda: NODO)), EFECTIVO)
    ops = _pendientes(nodo)
    assert "source_type" not in ops[0].payload and "source_id" not in ops[0].payload

    assert [r.result for r in _subir(central, ops)] == ["accepted"]
    assert _ventas_del_central(central) == [("S02-V-00001", "libraedge:nodo-s02", vid)]
    assert _origenes(central) == []                       # sin vínculo propio no se escribe en la tabla


def test_el_vinculo_del_producto_viaja_en_el_payload_junto_con_su_extra(bases):
    nodo, _central = bases
    _vender(nodo, _hooks_del_producto(extra={"reserva_id": RESERVA}), EFECTIVO)
    payload = _pendientes(nodo)[0].payload
    assert (payload["source_type"], payload["source_id"]) == ("reserva", RESERVA)
    assert payload["extra"] == {"reserva_id": RESERVA}
    json.dumps(payload)


def test_un_source_type_sin_source_id_tambien_es_un_vinculo(bases):
    """El buffet de mostrador (`source_type = 'mostrador'`, sin reserva) también se conserva."""
    nodo, central = bases
    _vender(nodo, _hooks_del_producto(tipo="mostrador", id_=None), EFECTIVO)
    payload = _pendientes(nodo)[0].payload
    assert (payload["source_type"], payload["source_id"]) == ("mostrador", None)
    assert [r.result for r in _subir(central, _pendientes(nodo))] == ["accepted"]
    assert _ventas_del_central(central) == [("S02-V-00001", "mostrador", None)]


# ── El central conserva el vínculo ──────────────────────────────────────────


def test_el_central_conserva_el_vinculo_y_guarda_la_identidad_del_nodo_aparte(bases):
    nodo, central = bases
    vid = _vender(nodo, _hooks_del_producto(), EFECTIVO)
    assert [r.result for r in _subir(central, _pendientes(nodo))] == ["accepted"]

    with central.abrir() as conn:
        vid_central = offline.venta_del_central(conn, NODO.node_id, vid)
        assert vid_central is not None
        # Lo que busca el buffet de LibraClub: sus consumos por reserva.
        consumos = conn.execute(
            "SELECT id FROM sales WHERE source_type = 'reserva' AND source_id = ?", (RESERVA,)).fetchall()
        assert [c["id"] for c in consumos] == [vid_central]
    assert _ventas_del_central(central) == [("S02-V-00001", "reserva", RESERVA)]
    assert _origenes(central) == [("nodo-s02", vid, vid_central)]
    assert _stock_del_central(central) == 8.0


def test_las_operaciones_siguientes_encuentran_la_venta_por_su_identidad_de_nodo(bases):
    """El pago acreditado, la devolución y la anulación llegan después y buscan la venta del nodo: ya no está marcada
    `libraedge:<node>`, así que la encuentran por `venta_origen_nodo`."""
    nodo, central = bases
    hooks = _hooks_del_producto()
    a = _vender(nodo, hooks, PARCIAL)
    b = _vender(nodo, hooks, EFECTIVO)
    with nodo.abrir() as conn:
        assert ventas.acreditar_pago_qr(conn, a, "MP-77", hooks=hooks) is True
        linea = conn.execute("SELECT id FROM sale_items WHERE sale_id=?", (a,)).fetchone()[0]
        deposito = conn.execute("SELECT id FROM locations WHERE is_default = 1").fetchone()[0]
        ventas.devolver_items(conn, a, {linea: 1}, deposito, usuario_id=USUARIO["id"], hooks=hooks)
        assert ventas.anular_venta(conn, b, usuario_id=USUARIO["id"], hooks=hooks) is True
        conn.commit()
    ops = _pendientes(nodo)
    assert [o.operation_type for o in ops] == [
        "venta.registrada", "venta.registrada", "venta.pago_acreditado", "venta.devolucion", "venta.anulada"]

    assert [r.result for r in _subir(central, ops)] == ["accepted"] * 5
    with central.abrir() as conn:
        estados = {f["number"]: (f["status"], f["status_detail"], f["source_type"], f["source_id"]) for f in conn.execute(
            "SELECT number, status, status_detail, source_type, source_id FROM sales").fetchall()}
        pagos = [f[0] for f in conn.execute(
            "SELECT estado FROM ventas_pagos WHERE venta_id = ? ORDER BY id", (offline.venta_del_central(conn, "nodo-s02", a),)
        ).fetchall()]
    assert estados == {"S02-V-00001": ("confirmed", "devuelta_parcial", "reserva", RESERVA),
                       "S02-V-00002": ("cancelled", "anulada", "reserva", RESERVA)}
    assert pagos == ["aprobado", "aprobado"]                                  # el pago QR se acreditó en la venta correcta
    assert _stock_del_central(central) == 9.0                                 # 10 - 2 - 2, +1 devuelto, +2 de la anulada


def test_sin_gancho_registrado_el_vinculo_igual_se_conserva(bases):
    nodo, central = bases
    _vender(nodo, _hooks_del_producto(extra={"reserva_id": RESERVA}), EFECTIVO)
    assert [r.result for r in _subir(central, _pendientes(nodo), al_materializar=None)] == ["accepted"]
    assert _ventas_del_central(central) == [("S02-V-00001", "reserva", RESERVA)]


def test_una_venta_con_vinculo_no_corre_la_numeracion_del_central(bases):
    """`siguiente_numero` ignora las ventas que subieron de un nodo; las de vínculo propio ya no se reconocen por el
    `source_type`, así que se reconocen por `venta_origen_nodo`."""
    nodo, central = bases
    _vender(central, SIN_GANCHOS, EFECTIVO)                                   # V-00001, del central
    _vender(nodo, _hooks_del_producto(), EFECTIVO)                            # S02-V-00001
    _vender(nodo, _hooks_del_producto(), EFECTIVO)                            # S02-V-00002
    assert [r.result for r in _subir(central, _pendientes(nodo))] == ["accepted", "accepted"]
    with central.abrir() as conn:
        assert ventas.siguiente_numero(conn) == "V-00002"


# ── El gancho del producto: al_materializar ─────────────────────────────────


def _tabla_del_producto(central):
    """La tabla propia del producto que `al_materializar` rehace (el consumo de la reserva)."""
    with central.abrir() as conn:
        conn.execute("CREATE TABLE consumos_de_reserva (venta_id INTEGER PRIMARY KEY, reserva_id INTEGER NOT NULL, "
                     "desde_nodo TEXT NOT NULL)")
        conn.commit()


def _filas_del_producto(central):
    with central.abrir() as conn:
        return [tuple(f) for f in conn.execute(
            "SELECT venta_id, reserva_id, desde_nodo FROM consumos_de_reserva ORDER BY venta_id").fetchall()]


def test_con_gancho_el_producto_rehace_su_vinculo_dentro_de_la_misma_transaccion(bases):
    nodo, central = bases
    _tabla_del_producto(central)
    _vender(nodo, _hooks_del_producto(extra={"reserva_id": RESERVA}), EFECTIVO)
    vistas = []

    def al_materializar(conn, venta_id, operacion):
        # La venta ya está creada, con el vínculo puesto, en la transacción del receptor (todavía sin confirmar).
        fila = conn.execute("SELECT source_type, source_id FROM sales WHERE id = ?", (venta_id,)).fetchone()
        vistas.append((venta_id, tuple(fila)))
        extra = operacion.payload["extra"]
        conn.execute("INSERT INTO consumos_de_reserva (venta_id, reserva_id, desde_nodo) VALUES (?, ?, ?)",
                     (venta_id, extra["reserva_id"], operacion.node_id))

    assert [r.result for r in _subir(central, _pendientes(nodo), al_materializar=al_materializar)] == ["accepted"]
    with central.abrir() as conn:
        vid_central = conn.execute("SELECT id FROM sales").fetchone()[0]
    assert vistas == [(vid_central, ("reserva", RESERVA))]
    assert _filas_del_producto(central) == [(vid_central, RESERVA, "nodo-s02")]


def test_si_el_gancho_falla_no_queda_nada_y_la_operacion_se_recupera_al_reintentar(bases):
    nodo, central = bases
    _tabla_del_producto(central)
    _vender(nodo, _hooks_del_producto(extra={"reserva_id": RESERVA}), PARCIAL)
    ops = _pendientes(nodo)

    def roto(conn, venta_id, operacion):
        # Escribe lo suyo y recién después levanta: lo escrito también se tiene que deshacer.
        conn.execute("INSERT INTO consumos_de_reserva (venta_id, reserva_id, desde_nodo) VALUES (?, 7, 'x')", (venta_id,))
        raise ValueError("la reserva 7 ya no existe en el central")

    resultado = _subir(central, ops, al_materializar=roto)[0]
    assert resultado.result == "rejected" and "la reserva 7 ya no existe" in resultado.error
    # Ni la venta, ni sus pagos, ni la caja, ni el stock, ni la identidad de nodo, ni lo que escribió el gancho.
    for tabla in ("sales", "sale_items", "ventas_pagos", "caja_movimientos", "venta_origen_nodo", "consumos_de_reserva"):
        assert _cuenta(central, tabla) == 0, tabla
    assert _stock_del_central(central) == 10.0

    # Revisión manual: arreglado el central, el reintento aplica la misma operación y queda todo.
    def bueno(conn, venta_id, operacion):
        conn.execute("INSERT INTO consumos_de_reserva (venta_id, reserva_id, desde_nodo) VALUES (?, ?, ?)",
                     (venta_id, operacion.payload["extra"]["reserva_id"], operacion.node_id))

    assert _reintentar(central, ops[0], al_materializar=bueno).result == "accepted"
    assert _ventas_del_central(central) == [("S02-V-00001", "reserva", RESERVA)]
    assert len(_origenes(central)) == 1 and len(_filas_del_producto(central)) == 1
    assert _stock_del_central(central) == 8.0


def test_un_error_inesperado_del_gancho_tampoco_deja_nada(bases):
    """No es un `ValueError`: el receptor no lo atrapa y se propaga; la transacción sin confirmar se deshace al cerrar."""
    nodo, central = bases
    _vender(nodo, _hooks_del_producto(), EFECTIVO)
    ops = _pendientes(nodo)

    def roto(conn, venta_id, operacion):
        raise RuntimeError("se cayó el producto")

    with pytest.raises(RuntimeError, match="se cayó"):
        _subir(central, ops, al_materializar=roto)
    for tabla in ("sales", "sale_items", "ventas_pagos", "caja_movimientos", "venta_origen_nodo"):
        assert _cuenta(central, tabla) == 0, tabla
    assert _stock_del_central(central) == 10.0
    assert [r.result for r in _subir(central, ops)] == ["accepted"]           # y sigue siendo aplicable


# ── Idempotencia al reaplicar ───────────────────────────────────────────────


def test_reaplicar_la_operacion_no_duplica_la_venta_ni_vuelve_a_llamar_al_gancho(bases):
    nodo, central = bases
    _vender(nodo, _hooks_del_producto(extra={"reserva_id": RESERVA}), EFECTIVO)
    ops = _pendientes(nodo)
    llamadas = []
    assert [r.result for r in _subir(central, ops, al_materializar=lambda c, v, o: llamadas.append(v))] == ["accepted"]
    assert [r.result for r in _subir(central, ops)] == ["duplicate"]          # el receptor descarta el operation_id

    # Y sin el receptor (un reintento de revisión manual, o dos workers): el aplicador es idempotente por la identidad de nodo.
    with central.abrir() as conn:
        offline.aplicar_venta_registrada(conn, ops[0], al_materializar=lambda c, v, o: llamadas.append(v))
        offline.aplicar_venta_registrada(conn, ops[0], al_materializar=lambda c, v, o: llamadas.append(v))
        conn.commit()
    assert _cuenta(central, "sales") == 1 and _cuenta(central, "venta_origen_nodo") == 1
    assert _cuenta(central, "caja_movimientos") == 1
    assert len(llamadas) == 1
    assert _stock_del_central(central) == 8.0


# ── Un source_id provisorio se traduce ──────────────────────────────────────

PROVISORIO = -3
RESERVA_DEL_CENTRAL = 55
TABLAS = {"reserva": "reservas"}


def _mapear_reserva(central, id_nodo=PROVISORIO, id_central=RESERVA_DEL_CENTRAL, tabla="reservas"):
    """Lo que hace el aplicador del alta de la reserva del producto (`cliente.alta` para los clientes): anota el mapeo."""
    from libraedge.mapeo import anotar_mapeo

    with central.abrir() as conn:
        anotar_mapeo(conn, NODO.node_id, tabla, id_nodo, id_central)
        conn.commit()


def test_un_source_id_provisorio_se_traduce_con_el_mapeo(bases):
    nodo, central = bases
    _vender(nodo, _hooks_del_producto(id_=PROVISORIO), EFECTIVO)
    ops = _pendientes(nodo)
    assert ops[0].payload["source_id"] == PROVISORIO                           # el nodo manda el provisorio tal cual
    _mapear_reserva(central)

    assert [r.result for r in _subir(central, ops, tablas_de_origen=TABLAS)] == ["accepted"]
    assert _ventas_del_central(central) == [("S02-V-00001", "reserva", RESERVA_DEL_CENTRAL)]
    assert _origenes(central)[0][:2] == ("nodo-s02", ops[0].payload["venta_id"])


def test_un_source_id_provisorio_sin_mapeo_se_rechaza_y_se_recupera_cuando_llega_el_alta(bases):
    nodo, central = bases
    _vender(nodo, _hooks_del_producto(id_=PROVISORIO), EFECTIVO)
    ops = _pendientes(nodo)

    resultado = _subir(central, ops, tablas_de_origen=TABLAS)[0]
    assert resultado.result == "rejected" and "no está en el central" in resultado.error
    for tabla in ("sales", "venta_origen_nodo", "caja_movimientos"):
        assert _cuenta(central, tabla) == 0, tabla
    assert _stock_del_central(central) == 10.0

    _mapear_reserva(central)
    assert _reintentar(central, ops[0], tablas_de_origen=TABLAS).result == "accepted"
    assert _ventas_del_central(central) == [("S02-V-00001", "reserva", RESERVA_DEL_CENTRAL)]


def test_un_source_id_provisorio_sin_tabla_declarada_se_rechaza_en_vez_de_atarse_a_otra_cosa(bases):
    nodo, central = bases
    _vender(nodo, _hooks_del_producto(id_=PROVISORIO), EFECTIVO)
    resultado = _subir(central, _pendientes(nodo))[0]                          # el producto no declaró `tablas_de_origen`
    assert resultado.result == "rejected" and "tablas_de_origen" in resultado.error
    assert _cuenta(central, "sales") == 0


def test_el_id_del_central_traduce_lo_que_viaja_en_el_extra(bases):
    """Un producto cuyo `extra` lleva el id de una entidad del nodo lo traduce en su `al_materializar` con `id_del_central`."""
    nodo, central = bases
    _tabla_del_producto(central)
    _vender(nodo, _hooks_del_producto(tipo="mostrador", id_=None, extra={"reserva_id": PROVISORIO}), EFECTIVO)
    _mapear_reserva(central)

    def al_materializar(conn, venta_id, operacion):
        reserva = offline.id_del_central(conn, operacion.node_id, "reservas", operacion.payload["extra"]["reserva_id"])
        conn.execute("INSERT INTO consumos_de_reserva (venta_id, reserva_id, desde_nodo) VALUES (?, ?, ?)",
                     (venta_id, reserva, operacion.node_id))

    assert [r.result for r in _subir(central, _pendientes(nodo), al_materializar=al_materializar)] == ["accepted"]
    assert [f[1] for f in _filas_del_producto(central)] == [RESERVA_DEL_CENTRAL]
    with central.abrir() as conn:
        assert offline.id_del_central(conn, "nodo-s02", "reservas", None) is None
        assert offline.id_del_central(conn, "nodo-s02", "reservas", 12) == 12      # un id del central pasa igual
        with pytest.raises(offline.VinculoNoSincronizado):
            offline.id_del_central(conn, "nodo-s02", "reservas", -99)


# ── Lo que se rechaza ───────────────────────────────────────────────────────


def test_el_marcador_reservado_del_nodo_no_puede_ser_un_vinculo(bases):
    nodo, central = bases
    _vender(nodo, _hooks_del_producto(tipo="libraedge:otro-nodo", id_=3), EFECTIVO)
    resultado = _subir(central, _pendientes(nodo))[0]
    assert resultado.result == "rejected" and "marcador reservado" in resultado.error
    assert _cuenta(central, "sales") == 0


def test_un_source_id_que_no_es_un_entero_se_rechaza(bases):
    nodo, central = bases
    _vender(nodo, _hooks_del_producto(), EFECTIVO)
    op = _pendientes(nodo)[0]
    from dataclasses import replace

    mala = replace(op, payload={**op.payload, "source_id": "siete"})
    resultado = _subir(central, [mala])[0]
    assert resultado.result == "rejected" and "no es un entero" in resultado.error
    assert _cuenta(central, "sales") == 0


def test_sin_la_revision_0010_la_venta_con_vinculo_pide_migrar_y_no_escribe_nada(puntas):  # noqa: F811
    nodo, central = puntas                                                     # el central todavía no migró
    _vender(nodo, _hooks_del_producto(), EFECTIVO)
    resultado = _subir(central, _pendientes(nodo))[0]
    assert resultado.result == "rejected"
    assert "0010_vinculo_de_la_venta" in resultado.error and "libracommerce-migrar" in resultado.error
    for tabla in ("sales", "caja_movimientos"):
        assert _cuenta(central, tabla) == 0, tabla
    assert _stock_del_central(central) == 10.0


def test_sin_la_revision_0010_una_venta_sin_vinculo_se_aplica_como_siempre(puntas):  # noqa: F811
    nodo, central = puntas
    vid = _vender(nodo, offline.con_outbox(SIN_GANCHOS, offline.Outbox(nodo=lambda: NODO)), PARCIAL)
    with nodo.abrir() as conn:
        hooks = offline.con_outbox(SIN_GANCHOS, offline.Outbox(nodo=lambda: NODO))
        ventas.acreditar_pago_qr(conn, vid, "MP-1", hooks=hooks)
        conn.commit()
    ops = _pendientes(nodo)
    assert [r.result for r in _subir(central, ops)] == ["accepted", "accepted"]
    assert _ventas_del_central(central) == [("S02-V-00001", "libraedge:nodo-s02", vid)]


# ── La revisión 0010 ────────────────────────────────────────────────────────


def _origen_de_nodo(conn):
    return [tuple(f) for f in conn.execute(
        "SELECT node_id, venta_id_nodo, venta_id FROM venta_origen_nodo ORDER BY venta_id").fetchall()]


def test_la_revision_0010_crea_la_tabla_vacia_sin_tocar_las_ventas(destino):
    _crear_base_al_dia_de_0001(destino)

    def venta_previa(conn):
        conn.execute("INSERT INTO sales (number, source_type, source_id) VALUES ('S02-V-1', 'libraedge:n', 4)")
        conn.commit()
        return [tuple(f) for f in conn.execute("SELECT number, source_type, source_id FROM sales").fetchall()]

    def sin_la_tabla(conn):
        assert not _vto._columnas_de(conn, "venta_origen_nodo")                  # `init_schema()` sigue congelado

    antes = _con_conexion(destino, venta_previa)
    _con_conexion(destino, sin_la_tabla)
    migrar.upgrade(destino)

    def verificar(conn):
        assert _vto._columnas_de(conn, "venta_origen_nodo") == {"node_id", "venta_id_nodo", "venta_id"}
        assert _origen_de_nodo(conn) == []
        assert [tuple(f) for f in conn.execute("SELECT number, source_type, source_id FROM sales").fetchall()] == antes
        return [f[0] for f in conn.execute("SELECT version_num FROM alembic_version_libracommerce").fetchall()]

    assert _con_conexion(destino, verificar) == ["0010_vinculo_de_la_venta"]
    migrar.upgrade(destino)                                                    # idempotente
    assert _con_conexion(destino, verificar) == ["0010_vinculo_de_la_venta"]


def test_la_tabla_de_la_revision_0010_tiene_la_forma_que_usa_el_aplicador(destino):
    """Clave `(node_id, venta_id_nodo)`, `venta_id` único y en cascada con la venta."""
    import sqlite3

    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)

    def verificar(conn):
        conn.execute("INSERT INTO sales (number) VALUES ('A')")
        conn.execute("INSERT INTO sales (number) VALUES ('B')")
        a, b = (f[0] for f in conn.execute("SELECT id FROM sales ORDER BY id").fetchall())
        conn.execute("INSERT INTO venta_origen_nodo VALUES ('n', 1, ?)", (a,))
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):                            # la misma venta del nodo en otra del central
            conn.execute("INSERT INTO venta_origen_nodo VALUES ('n', 1, ?)", (b,))
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):                            # la misma del central desde otra del nodo
            conn.execute("INSERT INTO venta_origen_nodo VALUES ('m', 2, ?)", (a,))
        conn.rollback()
        conn.execute("DELETE FROM sales WHERE id = ?", (a,))
        assert _origen_de_nodo(conn) == []                                     # en cascada con la venta

    _con_conexion(destino, verificar)


def test_la_revision_0010_baja_sin_tocar_las_ventas_y_vuelve_a_subir(destino):
    from alembic import command

    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)

    def con_una_venta(conn):
        conn.execute("INSERT INTO sales (number, source_type, source_id) VALUES ('S02-V-1', 'reserva', 7)")
        vid = conn.execute("SELECT id FROM sales").fetchone()[0]
        conn.execute("INSERT INTO venta_origen_nodo VALUES ('n', 4, ?)", (vid,))
        conn.commit()

    _con_conexion(destino, con_una_venta)
    command.downgrade(migrar.configuracion(destino), "0009_sucursal_del_nodo")

    def bajada(conn):
        assert _vto._columnas_de(conn, "venta_origen_nodo") == set()
        assert [tuple(f) for f in conn.execute("SELECT number, source_type, source_id FROM sales").fetchall()] == [
            ("S02-V-1", "reserva", 7)]                                         # el vínculo del producto queda

    _con_conexion(destino, bajada)
    migrar.upgrade(destino)
    assert _con_conexion(destino, _origen_de_nodo) == []                       # vuelve vacía: no reconstruye lo anotado
