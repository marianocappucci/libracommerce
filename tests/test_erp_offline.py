"""Las ventas de la capa ERP en un nodo offline: ganchos nuevos, traductor, encolado y aplicadores del central (ADR-067).

Los tests de punta a punta tienen DOS bases —el nodo y el central— y corren la venta en una y la aplican en la otra
pasando por el `SyncReceiver` de LibraEdge, como en producción. La capa de conexión de LibraCore es global (la caja
default se resuelve con una conexión propia), así que cada fase reconfigura `core` a su base. Hay una sola base de
PostgreSQL por worker, así que las combinaciones son: las dos en SQLite, el nodo en PostgreSQL (el encolado en la
transacción de psycopg) y el central en PostgreSQL (los aplicadores).
"""

from __future__ import annotations

import contextlib
import datetime
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, timedelta

import pytest
from conftest import USUARIO, _restaurar, _schema_de_producto, url_postgres

from libracommerce.erp import SIN_GANCHOS, Hooks, RequiereConexion, catalogo, offline, offline_caja, stock, ventas

libraedge = pytest.importorskip("libraedge")

HOY = datetime.date.today().isoformat()
NODO = offline.NodoLocal(node_id="nodo-s02", prefijo="S02")


# ── Las dos bases ──────────────────────────────────────────────────────────


class _Base:
    def __init__(self, destino: str):
        self.destino = destino

    @contextlib.contextmanager
    def abrir(self):
        from libracore.db import core

        core.configure(self.destino)
        conn = core.get_connection()
        try:
            yield conn
        finally:
            conn.close()

    def fabrica(self):
        """Un `conexion()` como el de un producto, apuntando a esta base (para `crear_venta_directa`)."""
        from libracore.db import core

        core.configure(self.destino)
        return core.get_connection


def _armar(conn):
    from libraedge.db.schema import init_schema as init_edge

    init_edge(conn)
    # El mismo producto, con el mismo id, en las dos puntas: el catálogo es del central y el nodo lo espeja.
    pid = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
    stock.ajustar_stock(conn, pid, 10.0, "inicial", usuario_id=USUARIO["id"], fecha=HOY)
    conn.commit()


def _base(motor: str, ruta) -> _Base:
    from libracore.db import core

    if motor == "sqlite":
        base = _Base(str(ruta))
        core.configure(base.destino)
        conn = core.get_connection()
        try:
            _schema_de_producto(conn)
        finally:
            conn.close()
    else:
        url = url_postgres()
        _restaurar("ventas", _schema_de_producto)
        base = _Base(url)
    with base.abrir() as conn:
        _armar(conn)
    return base


@pytest.fixture(params=[("sqlite", "sqlite"), ("postgres", "sqlite"), ("sqlite", "postgres")],
                ids=["nodo-sqlite-central-sqlite", "nodo-postgres-central-sqlite", "nodo-sqlite-central-postgres"])
def puntas(request, tmp_path):
    from libracore.db import core

    motor_nodo, motor_central = request.param
    nodo = _base(motor_nodo, tmp_path / "nodo.db")
    central = _base(motor_central, tmp_path / "central.db")
    yield nodo, central
    core._db_path = None
    core._database_url = None


def _hooks_de_nodo(en_linea=False, base: Hooks = SIN_GANCHOS) -> Hooks:
    return offline.con_outbox(base, offline.Outbox(nodo=lambda: NODO, en_linea=lambda: en_linea))


def _vender(base: _Base, hooks: Hooks, pagos, *, qty=2.0, precio=100.0):
    items = [{"nombre": "Yerba", "qty": qty, "precio": precio, "subtotal": qty * precio, "producto_id": 1}]
    total = qty * precio
    return ventas.crear_venta_directa(
        base.fabrica(), fecha=HOY, items=items, subtotal=total, descuento=0.0, total=total, cliente_id=None,
        cliente_nombre="", usuario_id=USUARIO["id"], observaciones="", estado=ventas.estado_segun_pagos(total, pagos),
        pagos=pagos, stock_habilitado=True, hooks=hooks,
    )


def _pendientes(base: _Base):
    from libraedge.db.repository import NodeRepository

    with base.abrir() as conn:
        return list(NodeRepository(conn).list_pending_operations(limit=1000))


def _subir(central: _Base, operaciones, hooks: Hooks = SIN_GANCHOS, aplicar_factura=None):
    """Cada operación por el receptor de LibraEdge, con los aplicadores registrados en su `Despachador` (v0.7.0)."""
    from libraedge.sync.despacho import Despachador
    from libraedge.sync.receiver import SyncReceiver

    despachador = offline.registrar_en(Despachador(), hooks, aplicar_factura=aplicar_factura)
    resultados = []
    with central.abrir() as conn:
        receptor = SyncReceiver(conn, despachador=despachador)
        for op in operaciones:
            resultados.append(receptor.accept(op))
    return resultados


def _stock(base: _Base) -> float:
    with base.abrir() as conn:
        return float(stock.get_stock_actual(conn, 1))


def _caja(base: _Base, numero: str):
    with base.abrir() as conn:
        return [(f["tipo"], float(f["monto"])) for f in conn.execute(
            "SELECT tipo, monto FROM caja_movimientos WHERE concepto LIKE ? ORDER BY id", (f"%{numero}%",)).fetchall()]


# ── Composición y piezas puras ─────────────────────────────────────────────


def test_fuera_de_un_nodo_con_outbox_no_cambia_nada():
    """En el central (o una instalación común) `nodo()` es `None`: los ganchos del producto corren igual y nada se
    encola; el numerador es el del producto."""
    llamados = []
    base = Hooks(al_anular_venta=lambda c, v: llamados.append(("anular", v)), numerador=lambda c: "POS-000009")
    hooks = offline.con_outbox(base, offline.Outbox(nodo=lambda: None))
    hooks.al_anular_venta("conn", {"id": 1, "numero": "X", "factura_id": 5})
    hooks.al_registrar_venta("conn", {"id": 1})
    assert llamados == [("anular", {"id": 1, "numero": "X", "factura_id": 5})]
    assert hooks.numerador("conn") == "POS-000009"
    # Lo que no toca, queda: la composición no pisa ningún otro gancho.
    assert hooks.turno_para is base.turno_para and hooks.al_confirmar_venta is base.al_confirmar_venta


def test_en_un_nodo_el_numero_lleva_el_prefijo_de_la_sucursal():
    hooks = offline.con_outbox(Hooks(numerador=lambda c: "POS-000123"), offline.Outbox(nodo=lambda: NODO))
    assert hooks.numerador("conn") == "S02-POS-000123"


@pytest.mark.parametrize("prefijo", ["", "s02", "S-02", "SUCURSAL02"])
def test_el_prefijo_de_sucursal_se_valida(prefijo):
    with pytest.raises(ValueError, match="prefijo"):
        offline.NodoLocal(node_id="n", prefijo=prefijo)


def test_nodo_desde_entorno(monkeypatch):
    leer = offline.nodo_desde_entorno("X_EDGE_NODE_ID", "X_EDGE_PREFIJO")
    monkeypatch.delenv("X_EDGE_NODE_ID", raising=False)
    assert leer() is None
    monkeypatch.setenv("X_EDGE_NODE_ID", "nodo-1")
    monkeypatch.setenv("X_EDGE_PREFIJO", "S03")
    assert leer() == offline.NodoLocal("nodo-1", "S03")
    monkeypatch.delenv("X_EDGE_PREFIJO")
    with pytest.raises(ValueError, match="prefijo"):
        leer()


def test_en_linea_segun_el_archivo_de_estado_del_nodo(tmp_path):
    from libraedge.nodo import EstadoNodo, escribir_estado

    ruta = str(tmp_path / "estado.json")
    en_linea = offline.en_linea_segun_estado(ruta, vigencia=timedelta(minutes=5))
    assert en_linea() is False  # sin archivo: sin conexión
    reciente = datetime.datetime.now(UTC).isoformat()
    escribir_estado(ruta, EstadoNodo(node_id="n", en_linea=True, pendientes=0, cursor=0,
                                     ultima_sincronizacion_ok=reciente))
    assert en_linea() is True
    viejo = (datetime.datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    escribir_estado(ruta, EstadoNodo(node_id="n", en_linea=True, pendientes=0, cursor=0,
                                     ultima_sincronizacion_ok=viejo))
    assert en_linea() is False  # el proceso del nodo se cayó: el archivo quedó viejo
    escribir_estado(ruta, EstadoNodo(node_id="n", en_linea=False, pendientes=3, cursor=0,
                                     ultima_sincronizacion_ok=reciente))
    assert en_linea() is False


def test_registrar_en_un_despachador_de_libraedge():
    from libraedge.sync.despacho import Despachador

    despachador = offline.registrar_en(Despachador())
    # Los cinco de venta, los cinco de caja (ADR-069, `test_erp_offline_caja.py`), el alta de clientes (ADR-070,
    # `test_erp_offline_clientes.py`) y los dos de stock (ADR-071, `test_erp_offline_stock.py`).
    from libracommerce.erp import offline_clientes, offline_stock

    todos = (*offline.TIPOS, *offline_caja.TIPOS_DE_CAJA, *offline_clientes.TIPOS_DE_CLIENTES, *offline_stock.TIPOS_DE_STOCK)
    assert len(todos) == 13
    assert despachador.tipos() == tuple(sorted(todos))
    assert all(despachador.versiones(t) == (offline.SCHEMA_VERSION,) for t in todos)
    with pytest.raises(ValueError, match="ya hay un manejador"):
        offline.registrar_en(despachador)


def test_el_despachador_rechaza_un_tipo_desconocido():
    from libraedge.domain.sync import OutboxOperation

    op = OutboxOperation(operation_id="n:1", node_id="n", sequence=1, operation_type="pedido.cobrado",
                         aggregate_type="venta", aggregate_id="n:venta:1", occurred_at="x", schema_version=1,
                         payload={})
    with pytest.raises(ValueError, match="desconocido"):
        offline.despachar(offline.aplicadores())("conn", op)


def test_la_clave_de_la_devolucion_sale_de_la_operacion_y_entra_en_64():
    corto = _op("n:7")
    assert offline.clave_de_operacion(corto) == "nodo:n:7"
    largo = _op("x" * 80 + ":7")
    clave = offline.clave_de_operacion(largo)
    assert len(clave) <= 64 and clave.startswith("nodo:")


def _op(operation_id):
    from libraedge.domain.sync import OutboxOperation

    return OutboxOperation(operation_id=operation_id, node_id="n", sequence=7, operation_type="venta.devolucion",
                           aggregate_type="venta", aggregate_id="n:venta:1", occurred_at="x", schema_version=1,
                           payload={})


# ── Los ganchos nuevos del motor ──────────────────────────────────────────


def test_al_registrar_venta_corre_para_toda_venta_y_al_acreditar_con_los_pagos(abrir_ventas):
    """El gancho nuevo ve la venta PARCIAL (al_confirmar_venta no); al acreditar el QR llegan los pagos acreditados."""
    vistos = []
    hooks = Hooks(
        al_registrar_venta=lambda c, v: vistos.append(("registrada", v["estado"])),
        al_acreditar_pago=lambda c, v, p: vistos.append(("acreditado", v["estado"], [x["medio"] for x in p],
                                                         p[0]["payment_id"])),
        al_confirmar_venta=lambda c, v: vistos.append(("confirmada", v["estado"])),
    )
    pagos = [{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"},
             {"medio": "mercadopago_qr", "monto": 100.0, "estado": "pendiente"}]
    items = [{"nombre": "Suelto", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": None}]
    vid = ventas.crear_venta_directa(
        abrir_ventas, fecha=HOY, items=items, subtotal=200.0, descuento=0.0, total=200.0, cliente_id=None,
        cliente_nombre="", usuario_id=USUARIO["id"], observaciones="", estado=ventas.estado_segun_pagos(200.0, pagos),
        pagos=pagos, stock_habilitado=False, hooks=hooks,
    )
    assert vistos == [("registrada", "parcial")]
    with abrir_ventas() as conn:
        assert ventas.acreditar_pago_qr(conn, vid, "MP-1", hooks=hooks) is True
        conn.commit()
    assert vistos[1:] == [("acreditado", "cobrada", ["mercadopago_qr"], "MP-1"), ("confirmada", "cobrada")]


def test_al_devolver_items_no_corre_en_un_reintento(abrir_ventas):
    with abrir_ventas() as conn:
        pid = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        stock.ajustar_stock(conn, pid, 10.0, "inicial", usuario_id=USUARIO["id"], fecha=HOY)
        conn.commit()
    devueltas = []
    hooks = Hooks(al_devolver_items=lambda c, v, d: devueltas.append(d))
    items = [{"nombre": "Yerba", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": pid}]
    pagos = [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}]
    vid = ventas.crear_venta_directa(
        abrir_ventas, fecha=HOY, items=items, subtotal=200.0, descuento=0.0, total=200.0, cliente_id=None,
        cliente_nombre="", usuario_id=USUARIO["id"], observaciones="", estado="cobrada", pagos=pagos,
        stock_habilitado=True, hooks=hooks,
    )
    with abrir_ventas() as conn:
        linea = conn.execute("SELECT id FROM sale_items WHERE sale_id=?", (vid,)).fetchone()[0]
        deposito = conn.execute("SELECT id FROM locations WHERE is_default = 1").fetchone()[0]
        for _ in range(2):
            ventas.devolver_items(conn, vid, {linea: 1}, deposito, usuario_id=USUARIO["id"], hooks=hooks,
                                  clave_operacion="dev-1")
            conn.commit()
    assert len(devueltas) == 1
    assert devueltas[0]["lineas"] == {linea: 1.0} and devueltas[0]["importe"] == 100.0
    assert devueltas[0]["clave_operacion"] == "dev-1" and devueltas[0]["usuario_id"] == USUARIO["id"]


# ── De punta a punta: el nodo vende, el central aplica ─────────────────────


def test_venta_parcial_sube_y_se_completa_al_acreditarse(puntas):
    """Decisión 5: la venta parcial sube al nacer (`venta.registrada`) y el central la completa con
    `venta.pago_acreditado`. Una sola venta en el central, con el número del nodo y la caja de los dos pagos."""
    nodo, central = puntas
    hooks = _hooks_de_nodo()
    pagos = [{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"},
             {"medio": "mercadopago_qr", "monto": 100.0, "estado": "pendiente"}]
    vid = _vender(nodo, hooks, pagos)

    ops = _pendientes(nodo)
    assert [o.operation_type for o in ops] == ["venta.registrada"]
    registrada = ops[0]
    assert registrada.operation_id == "nodo-s02:1" and registrada.aggregate_id == f"nodo-s02:venta:{vid}"
    assert registrada.payload["numero"] == "S02-V-00001" and registrada.payload["estado"] == "parcial"
    # Plata y cantidades como texto: la forma exacta depende del motor (`-2.0` en SQLite, `-2.000` en PostgreSQL).
    assert [float(s["cantidad"]) for s in registrada.payload["stock"]] == [-2.0]
    json.dumps(registrada.payload)  # viaja como JSON

    with nodo.abrir() as conn:
        assert ventas.acreditar_pago_qr(conn, vid, "MP-77", hooks=hooks) is True
        conn.commit()
    ops = _pendientes(nodo)
    assert [o.operation_type for o in ops] == ["venta.registrada", "venta.pago_acreditado"]
    acreditados = ops[1].payload["pagos"]
    assert [(p["posicion"], p["medio"], float(p["monto"])) for p in acreditados] == [(1, "mercadopago_qr", 100.0)]
    assert ops[1].payload["payment_id"] == "MP-77" and ops[1].payload["estado"] == "cobrada"

    assert [r.result for r in _subir(central, ops)] == ["accepted", "accepted"]
    with central.abrir() as conn:
        filas = conn.execute("SELECT id, number, source_type, source_id FROM sales").fetchall()
        assert [(f["number"], f["source_type"], f["source_id"]) for f in filas] == [
            ("S02-V-00001", "libraedge:nodo-s02", vid)]
        venta = ventas.obtener_venta(conn, filas[0]["id"])
        assert venta["estado"] == "cobrada"
        assert [(p["medio"], p["estado"]) for p in venta["pagos"]] == [
            ("efectivo", "aprobado"), ("mercadopago_qr", "aprobado")]
    assert _caja(central, "S02-V-00001") == [("ingreso", 100.0), ("ingreso", 100.0)]
    assert _stock(central) == _stock(nodo) == 8.0

    # Reintento: el receptor descarta por operation_id, y el aplicador solo es idempotente por el agregado.
    assert [r.result for r in _subir(central, ops)] == ["duplicate", "duplicate"]
    with central.abrir() as conn:
        offline.aplicar_venta_registrada(conn, ops[0])
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1
    assert _stock(central) == 8.0


def test_devolucion_y_anulacion_suben_y_el_central_queda_igual(puntas):
    nodo, central = puntas
    hooks = _hooks_de_nodo()
    efectivo = [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}]
    a = _vender(nodo, hooks, efectivo)
    b = _vender(nodo, hooks, efectivo)
    with nodo.abrir() as conn:
        linea = conn.execute("SELECT id FROM sale_items WHERE sale_id=?", (a,)).fetchone()[0]
        deposito = conn.execute("SELECT id FROM locations WHERE is_default = 1").fetchone()[0]
        ventas.devolver_items(conn, a, {linea: 1}, deposito, usuario_id=USUARIO["id"], hooks=hooks)
        assert ventas.anular_venta(conn, b, usuario_id=USUARIO["id"], hooks=hooks) is True
        conn.commit()
    ops = _pendientes(nodo)
    assert [o.operation_type for o in ops] == [
        "venta.registrada", "venta.registrada", "venta.devolucion", "venta.anulada"]
    assert [o.sequence for o in ops] == [1, 2, 3, 4]

    assert [r.result for r in _subir(central, ops)] == ["accepted"] * 4
    assert _stock(central) == _stock(nodo) == 9.0
    with central.abrir() as conn:
        estados = {f["number"]: (f["status"], f["status_detail"]) for f in conn.execute(
            "SELECT number, status, status_detail FROM sales").fetchall()}
    assert estados == {"S02-V-00001": ("confirmed", "devuelta_parcial"), "S02-V-00002": ("cancelled", "anulada")}
    assert [m for m in _caja(central, "S02-V-00001") if m[0] == "egreso"] == \
        [m for m in _caja(nodo, "S02-V-00001") if m[0] == "egreso"]
    assert [m for m in _caja(central, "S02-V-00002") if m[0] == "egreso"] == [("egreso", 200.0)]

    # La devolución reaplicada directo (sin el receptor) contesta `repetida` y no escribe nada.
    with central.abrir() as conn:
        offline.aplicar_venta_devolucion(conn, ops[2])
        offline.aplicar_venta_anulada(conn, ops[3])
        conn.commit()
    assert _stock(central) == 9.0


def test_colision_de_numero_va_a_revision_manual(puntas):
    """Si el número del nodo ya lo tiene otra venta del central, se rechaza (no se lee como duplicado)."""
    nodo, central = puntas
    _vender(nodo, _hooks_de_nodo(), [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    # Una venta del central que casualmente tiene ese número.
    _vender(central, Hooks(numerador=lambda c: "S02-V-00001"),
            [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    ops = _pendientes(nodo)
    resultado = _subir(central, ops)[0]
    assert resultado.result == "rejected" and "colisión de numeración" in resultado.error
    with central.abrir() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1


def test_una_operacion_sobre_una_venta_que_no_subio_se_rechaza(puntas):
    nodo, central = puntas
    hooks = _hooks_de_nodo()
    vid = _vender(nodo, hooks, [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with nodo.abrir() as conn:
        ventas.anular_venta(conn, vid, hooks=hooks)
        conn.commit()
    anulada = _pendientes(nodo)[1]
    resultado = _subir(central, [anulada])[0]
    assert resultado.result == "rejected" and "no está en el central" in resultado.error


def test_el_central_no_numera_desde_las_ventas_del_nodo(puntas):
    nodo, central = puntas
    _vender(central, SIN_GANCHOS, [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    _vender(nodo, _hooks_de_nodo(), [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    _subir(central, _pendientes(nodo))
    with central.abrir() as conn:
        assert ventas.siguiente_numero(conn) == "V-00002"


def _facturar(base: _Base, vid: int) -> None:
    """Una factura sin CAE (no la tiene ARCA): `VentaConFacturaCAE` no salta, así que lo que decide es la conexión."""
    with base.abrir() as conn:
        conn.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, cliente_iva_cond, "
            "items, subtotal, iva_amount, total, ambiente, cae) VALUES (11, 5, 1, ?, '', 'CF', 5, '[]', 200, 0, 200, "
            "'homologacion', '')", (HOY,))
        fid = conn.execute("SELECT MAX(id) FROM facturas").fetchone()[0]
        ventas.vincular_factura(conn, vid, fid)
        conn.commit()


def test_anular_una_venta_facturada_sin_conexion_se_rechaza(puntas):
    nodo, _central = puntas
    sin_conexion = _hooks_de_nodo(en_linea=False)
    vid = _vender(nodo, sin_conexion, [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    _facturar(nodo, vid)
    with nodo.abrir() as conn:
        with pytest.raises(RequiereConexion, match="sin conexión"):
            ventas.anular_venta(conn, vid, hooks=sin_conexion)
        conn.rollback()
        assert ventas.obtener_venta(conn, vid)["status"] == "confirmed"
    assert _stock(nodo) == 8.0
    assert [o.operation_type for o in _pendientes(nodo)] == ["venta.registrada"]

    # Con conexión sí se anula, y sube.
    con_conexion = _hooks_de_nodo(en_linea=True)
    with nodo.abrir() as conn:
        assert ventas.anular_venta(conn, vid, hooks=con_conexion) is True
        conn.commit()
    assert [o.operation_type for o in _pendientes(nodo)] == ["venta.registrada", "venta.anulada"]


def test_si_el_gancho_del_producto_falla_no_queda_ni_la_venta_ni_la_operacion(puntas):
    """Atomicidad: el encolado está en la transacción de la venta. Un gancho del producto que levanta después se
    lleva todo (venta, stock y outbox)."""
    nodo, _central = puntas

    def roto(conn, venta):
        raise RuntimeError("falla del producto")

    hooks = offline.con_outbox(Hooks(al_confirmar_venta=roto), offline.Outbox(nodo=lambda: NODO))
    with pytest.raises(RuntimeError):
        _vender(nodo, hooks, [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    assert _pendientes(nodo) == []
    with nodo.abrir() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0
    assert _stock(nodo) == 10.0


def test_lo_propio_del_producto_viaja_en_extra_y_lo_materializa_el_central(puntas):
    nodo, central = puntas
    outbox = offline.Outbox(nodo=lambda: NODO, extra_de_venta=lambda conn, venta: {"pedido": {"numero": "P-0009"}})
    _vender(nodo, offline.con_outbox(SIN_GANCHOS, outbox), [{"medio": "efectivo", "monto": 200.0,
                                                              "estado": "aprobado"}])
    ops = _pendientes(nodo)
    assert ops[0].payload["extra"] == {"pedido": {"numero": "P-0009"}}
    vistos = []
    por_tipo = offline.aplicadores(al_materializar=lambda conn, vid, op: vistos.append((vid, op.payload["extra"])))
    with central.abrir() as conn:
        offline.despachar(por_tipo)(conn, ops[0])
        conn.commit()
        vid = conn.execute("SELECT id FROM sales").fetchone()[0]
    assert vistos == [(vid, {"pedido": {"numero": "P-0009"}})]


def test_el_schema_desconocido_se_rechaza():
    op = replace(_op("n:1"), operation_type="venta.anulada", schema_version=2)
    with pytest.raises(ValueError, match="schema 2"):
        offline.aplicar_venta_anulada(sqlite3.connect(":memory:"), op)


# ── venta.facturada (ADR-068) ──────────────────────────────────────────────
#
# El payload y la escritura de la factura son de libracore (`facturas_del_nodo`, ADR-074 del motor, todavía sin tag):
# acá se inyectan dobles con la misma forma. CAE y números ficticios.

CAE = "70000000000001"


def _factura_del_nodo(base: _Base, vid: int, *, numero=1, cae=CAE) -> dict:
    """La factura que emitió el nodo, ya vinculada a la venta (como la deja `facturar_venta` antes de `al_facturar`)."""
    with base.abrir() as conn:
        conn.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, cliente_iva_cond, "
            "items, subtotal, iva_amount, total, ambiente, cae) VALUES (11, 7, ?, ?, '', 'CF', 5, '[]', 200, 0, 200, "
            "'homologacion', ?)", (numero, HOY, cae))
        fid = conn.execute("SELECT MAX(id) FROM facturas").fetchone()[0]
        ventas.vincular_factura(conn, vid, fid)
        conn.commit()
        return dict(conn.execute("SELECT * FROM facturas WHERE id=?", (fid,)).fetchone())


def _payload_factura(factura) -> dict:
    """Como `libracore.facturas_del_nodo.payload_factura`: campos de la factura, `id_en_el_nodo` y `version`."""
    campos = ("tipo", "punto_venta", "numero", "fecha", "cliente_razon", "subtotal", "iva_amount", "total", "cae",
              "ambiente")
    datos = {c: (str(factura[c]) if c == "fecha" else factura[c]) for c in campos}
    datos["subtotal"], datos["iva_amount"], datos["total"] = (float(datos[k]) for k in ("subtotal", "iva_amount",
                                                                                          "total"))
    return {**datos, "items": [], "id_en_el_nodo": factura["id"], "version": 1}


def _aplicar_factura(payload, *, conn):
    """Como `aplicar_factura_del_nodo`: idempotente por (tipo, punto de venta, número, ambiente)."""
    fila = conn.execute("SELECT id FROM facturas WHERE tipo=? AND punto_venta=? AND numero=? AND ambiente=?",
                        (payload["tipo"], payload["punto_venta"], payload["numero"], payload["ambiente"])).fetchone()
    if fila:
        return fila[0], False
    conn.execute(
        "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, cliente_iva_cond, "
        "items, subtotal, iva_amount, total, ambiente, cae) VALUES (?, ?, ?, ?, '', ?, 5, '[]', ?, ?, ?, ?, ?)",
        (payload["tipo"], payload["punto_venta"], payload["numero"], payload["fecha"], payload["cliente_razon"],
         payload["subtotal"], payload["iva_amount"], payload["total"], payload["ambiente"], payload["cae"]))
    return conn.execute("SELECT MAX(id) FROM facturas").fetchone()[0], True


def _al_facturar(base: _Base, **kwargs):
    return offline.al_facturar_con_outbox(offline.Outbox(nodo=lambda: NODO), base.abrir,
                                          payload_factura=_payload_factura, **kwargs)


def test_la_factura_del_nodo_sube_y_queda_vinculada_a_la_venta(puntas):
    nodo, central = puntas
    vid = _vender(nodo, _hooks_de_nodo(), [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    factura = _factura_del_nodo(nodo, vid)
    with nodo.abrir() as conn:
        venta = ventas.obtener_venta(conn, vid)
    _al_facturar(nodo)(venta, factura)

    ops = _pendientes(nodo)
    assert [o.operation_type for o in ops] == ["venta.registrada", "venta.facturada"]
    facturada = ops[1]
    assert facturada.aggregate_id == f"nodo-s02:venta:{vid}"
    assert facturada.payload["venta_id"] == vid and facturada.payload["numero"] == "S02-V-00001"
    assert facturada.payload["factura"]["cae"] == CAE
    json.dumps(facturada.payload)

    assert [r.result for r in _subir(central, ops, aplicar_factura=_aplicar_factura)] == ["accepted", "accepted"]
    with central.abrir() as conn:
        vid_central = offline.venta_del_central(conn, NODO.node_id, vid)
        venta_central = ventas.obtener_venta(conn, vid_central)
        fac = conn.execute("SELECT id, numero, cae FROM facturas").fetchall()
        assert [(f["numero"], f["cae"]) for f in fac] == [(1, CAE)]
        assert venta_central["factura_id"] == fac[0]["id"]
        cobros = conn.execute("SELECT factura_id FROM caja_movimientos WHERE concepto LIKE ?",
                              ("%S02-V-00001%",)).fetchall()
        assert [c["factura_id"] for c in cobros] == [fac[0]["id"]], "el cobro queda como cobro de la factura"

        # Reaplicar no duplica la factura ni cambia el vínculo.
        offline.aplicar_venta_facturada(conn, facturada, aplicar_factura=_aplicar_factura)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM facturas").fetchone()[0] == 1
        assert ventas.obtener_venta(conn, vid_central)["factura_id"] == fac[0]["id"]


def test_venta_con_otra_factura_en_el_central_va_a_revision(puntas):
    nodo, central = puntas
    vid = _vender(nodo, _hooks_de_nodo(), [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    factura = _factura_del_nodo(nodo, vid, numero=3)
    with nodo.abrir() as conn:
        venta = ventas.obtener_venta(conn, vid)
    _al_facturar(nodo)(venta, factura)
    registrada, facturada = _pendientes(nodo)

    assert [r.result for r in _subir(central, [registrada])] == ["accepted"]
    with central.abrir() as conn:
        vid_central = offline.venta_del_central(conn, NODO.node_id, vid)
    _factura_del_nodo(central, vid_central, numero=99, cae="")  # el central la facturó por su lado

    resultado = _subir(central, [facturada], aplicar_factura=_aplicar_factura)[0]
    assert resultado.result == "rejected" and "ya tiene la factura" in resultado.error


def test_la_factura_de_una_venta_que_no_subio_se_rechaza(puntas):
    nodo, central = puntas
    vid = _vender(nodo, _hooks_de_nodo(), [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    factura = _factura_del_nodo(nodo, vid)
    with nodo.abrir() as conn:
        venta = ventas.obtener_venta(conn, vid)
    _al_facturar(nodo)(venta, factura)
    _registrada, facturada = _pendientes(nodo)
    resultado = _subir(central, [facturada], aplicar_factura=_aplicar_factura)[0]
    assert resultado.result == "rejected" and "no está en el central" in resultado.error


def test_al_facturar_fuera_de_un_nodo_no_encola_y_corre_el_del_producto():
    llamados = []

    def abrir():
        raise AssertionError("fuera de un nodo no se abre conexión")

    gancho = offline.al_facturar_con_outbox(offline.Outbox(nodo=lambda: None), abrir,
                                            base=lambda v, f: llamados.append((v["id"], f["id"])))
    gancho({"id": 1, "numero": "V-00001"}, {"id": 5})
    assert llamados == [(1, 5)]


def test_central_sin_facturas_del_nodo_manda_a_revision(monkeypatch):
    """Con un libracore sin `facturas_del_nodo` el aplicador levanta `ValueError` (revisión manual, se reintenta al
    actualizar el motor), no un `ImportError` que el receptor no atrapa."""
    import sys

    monkeypatch.setitem(sys.modules, "libracore.facturas_del_nodo", None)
    op = replace(_op("n:1"), operation_type="venta.facturada", payload={"venta_id": 1, "numero": "S02-V-00001",
                                                                        "factura": {"version": 1}})
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sales (id INTEGER PRIMARY KEY, source_type TEXT, source_id INTEGER)")
    conn.execute("INSERT INTO sales VALUES (1, 'libraedge:n', 1)")
    with pytest.raises(ValueError, match="facturas_del_nodo"):
        offline.aplicar_venta_facturada(conn, op)
    with pytest.raises(RuntimeError, match="facturas_del_nodo"):
        offline.payload_venta_facturada({"id": 1, "numero": "X"}, {"id": 1})


def test_fallo_al_facturar_y_la_reconciliacion_la_encola_una_sola_vez(puntas):
    nodo, central = puntas
    outbox = offline.Outbox(nodo=lambda: NODO)
    vid = _vender(nodo, _hooks_de_nodo(), [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    factura = _factura_del_nodo(nodo, vid)
    with nodo.abrir() as conn:
        venta = ventas.obtener_venta(conn, vid)

    def roto(factura):
        raise RuntimeError("la base del nodo se cayó justo acá")

    with pytest.raises(RuntimeError):  # libracore lo registraría y seguiría: la factura no subió
        offline.al_facturar_con_outbox(outbox, nodo.abrir, payload_factura=roto)(venta, factura)
    assert [o.operation_type for o in _pendientes(nodo)] == ["venta.registrada"]

    reconciliar = offline.reconciliador(outbox, nodo.abrir, payload_factura=_payload_factura)
    assert reconciliar() == [vid]
    assert reconciliar() == [], "la segunda pasada no encuentra nada"
    # Y un al_facturar tardío tampoco la duplica.
    _al_facturar(nodo)(venta, factura)
    ops = _pendientes(nodo)
    assert [o.operation_type for o in ops] == ["venta.registrada", "venta.facturada"]
    assert ops[1].payload["factura"]["cae"] == CAE

    assert [r.result for r in _subir(central, ops, aplicar_factura=_aplicar_factura)] == ["accepted", "accepted"]
    with central.abrir() as conn:
        assert ventas.obtener_venta(conn, offline.venta_del_central(conn, NODO.node_id, vid))["factura_id"]

    # Ya enviada (confirmada en el outbox), tampoco se vuelve a encolar.
    with nodo.abrir() as conn:
        conn.execute("UPDATE sync_outbox SET status='acknowledged'")
        conn.commit()
    assert reconciliar() == []


def test_la_reconciliacion_no_toca_lo_que_no_corresponde(puntas):
    nodo, _central = puntas
    outbox = offline.Outbox(nodo=lambda: NODO)
    pagos = [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}]
    sin_cae = _vender(nodo, _hooks_de_nodo(), pagos)
    _factura_del_nodo(nodo, sin_cae, numero=1, cae="")
    no_subio = _vender(nodo, SIN_GANCHOS, pagos)  # nació antes de que la instancia fuera un nodo
    _factura_del_nodo(nodo, no_subio, numero=2)
    _sin_factura = _vender(nodo, _hooks_de_nodo(), pagos)

    assert offline.reconciliador(outbox, nodo.abrir, payload_factura=_payload_factura)() == []
    assert {o.operation_type for o in _pendientes(nodo)} == {"venta.registrada"}


def test_el_reconciliador_fuera_de_un_nodo_no_abre_conexion():
    def abrir():
        raise AssertionError("fuera de un nodo no se abre conexión")

    assert offline.reconciliador(offline.Outbox(nodo=lambda: None), abrir)() == []
