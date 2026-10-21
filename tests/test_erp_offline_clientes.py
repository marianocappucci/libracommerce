"""Los clientes que se dan de alta en un nodo offline: del alta en el nodo a la venta, el central y el reapuntado (ADR-070).

El recorrido que se prueba, con DOS bases (el nodo y el central) y las piezas reales de las tres capas —`libracore` (id
provisorio y gancho), `libracommerce` (`cliente.alta` y su aplicador) y `libraedge` (`SyncReceiver`, el mapeo y el
`MirrorApplier`)—:

1. en el nodo se da de alta un cliente: nace con un **id provisorio negativo** (nunca choca con uno del central) y su alta
   se encola en el outbox **antes** que la venta que se le hace;
2. suben **en orden**; el central crea al cliente **con su propio id**, anota el mapeo y la venta queda **a su nombre**;
3. el cliente (y su mapeo) **bajan** por el espejo y el nodo reapunta lo que tenía a nombre del provisorio (la venta, el
   libro de cuenta corriente) y **borra la fila provisoria**.

Más: el central no duplica a un cliente que ya tenía (mismo CUIT) ni cuando lo dan de alta **dos nodos**; un id provisorio
sin mapeo se rechaza con un mensaje claro y se recupera reintentando en orden; y fuera de un nodo nada cambia. Los ids del
central se hacen **distintos a propósito** (ya tiene clientes propios) para que una traducción que copie el id del nodo no
pase por casualidad.
"""

from __future__ import annotations

import contextlib

import pytest
from conftest import USUARIO

# Las piezas de las dos bases son las mismas que las del test de las ventas.
from test_erp_offline import HOY, _base, _pendientes, _subir
from test_erp_offline import puntas as puntas  # noqa: F401, PLC0414

from libracommerce.erp import SIN_GANCHOS, offline, offline_clientes, ventas

libraedge = pytest.importorskip("libraedge")

NODO_A = offline.NodoLocal(node_id="nodo-s02", prefijo="S02")
NODO_B = offline.NodoLocal(node_id="nodo-s03", prefijo="S03")

#: Quién es «el nodo» en este momento. El proceso del test hace de las dos puntas (en producción son procesos distintos y
#: el central no tiene `LIBRAEDGE_NODE_ID`), así que la identidad se enciende sólo mientras se trabaja en el nodo.
_ACTUAL: dict[str, offline.NodoLocal | None] = {"nodo": None}
OUTBOX = offline.Outbox(nodo=lambda: _ACTUAL["nodo"])


@pytest.fixture(autouse=True)
def _altas_en_nodo():
    """Lo que hace el producto al armar la app: registrar las altas en el nodo, igual en las dos puntas."""
    from libracore import altas_en_nodo

    altas_en_nodo.configurar(offline_clientes.altas_en_nodo(OUTBOX))
    _ACTUAL["nodo"] = None
    yield
    altas_en_nodo.configurar(None)
    _ACTUAL["nodo"] = None


@contextlib.contextmanager
def en_el_nodo(base, nodo=NODO_A):
    """Trabajar en el nodo: la identidad encendida y `core` apuntando a su base."""
    _ACTUAL["nodo"] = nodo
    try:
        with base.abrir() as conn:
            yield conn
    finally:
        _ACTUAL["nodo"] = None


def _alta(nodo, nombre, cuit="", *, quien=NODO_A, **kw):
    from libracore.db import clients

    with en_el_nodo(nodo, quien):
        return clients.create_client(nombre, cuit_dni=cuit, **kw)


def _alta_en_el_central(central, nombre, cuit=""):
    from libracore.db import clients

    with central.abrir():
        return clients.create_client(nombre, cuit_dni=cuit)


def _vender_a(nodo, cliente_id, nombre, *, medio="efectivo", monto=200.0, quien=NODO_A):
    """Una venta de mostrador de 2 yerbas a `cliente_id`, con los ganchos de un nodo (como la hace el POS)."""
    pagos = [{"medio": medio, "monto": monto, "estado": "aprobado"}]
    items = [{"nombre": "Yerba", "qty": 2.0, "precio": monto / 2, "subtotal": monto, "producto_id": 1}]
    with en_el_nodo(nodo, quien):
        hooks = offline.con_outbox(SIN_GANCHOS, OUTBOX)
        return ventas.crear_venta_directa(
            nodo.fabrica(), fecha=HOY, items=items, subtotal=monto, descuento=0.0, total=monto, cliente_id=cliente_id,
            cliente_nombre=nombre, usuario_id=USUARIO["id"], observaciones="", estado="cobrada", pagos=pagos,
            stock_habilitado=True, hooks=hooks,
        )


def _filas(base, sql, params=()):
    with base.abrir() as conn:
        return [tuple(f) for f in conn.execute(sql, params).fetchall()]


def _espejo_de_clientes():
    """Lo que el nodo espeja de esto: el cliente, su party y el mapeo; las altas, declaradas como las declara un producto
    en su `Aprovisionamiento` (y como le llegan al nodo por la carga)."""
    from libracore.altas_en_nodo import REFERENCIAS_DE_CLIENTES
    from libraedge.altas import formatear_altas, parsear_altas
    from libraedge.mapeo import CLAVE, TABLA

    declaradas = [("clients", REFERENCIAS_DE_CLIENTES), "parties"]
    return {"clients": "id", "parties": "id", TABLA: CLAVE}, parsear_altas(formatear_altas(declaradas))


def _bajar(central, nodo, quien=NODO_A):
    """Lo que el nodo trae del central: el estado de `clients`, `parties` y el mapeo, en ese orden (como lo captura el
    trigger del changelog en la transacción del alta), aplicado con el `MirrorApplier` de LibraEdge."""
    from libraedge.db.changelog import init_changelog_schema, listar_cambios, sembrar
    from libraedge.mapeo import CLAVE, TABLA
    from libraedge.sync.pull import MirrorApplier

    with central.abrir() as conn:
        init_changelog_schema(conn)
        sembrar(conn, "clients", "id")
        sembrar(conn, "parties", "id")
        sembrar(conn, TABLA, CLAVE)
        conn.commit()
        cambios = listar_cambios(conn, 0, 1000)
    espejo, altas = _espejo_de_clientes()
    with nodo.abrir() as conn:
        aplicador = MirrorApplier(conn, espejo, node_id=quien.node_id, altas=altas)
        for cambio in cambios:
            aplicador.aplicar(cambio)
        aplicador.reconciliar_pendientes()
        conn.commit()
    return aplicador


# ── El recorrido completo ───────────────────────────────────────────────────


def test_el_cliente_del_nodo_nace_provisorio_sube_antes_que_su_venta_y_el_central_le_asigna_el_suyo(puntas):  # noqa: F811
    nodo, central = puntas
    _alta_en_el_central(central, "Juan", "20-11111111-2")
    _alta_en_el_central(central, "Maria", "27-22222222-3")

    ana = _alta(nodo, "Ana", "20-31781916-2", email="ana@example.com", phone="221-555", address="Calle 1")
    assert ana == -1, "el id provisorio es negativo: no se cruza con ningún id del central"
    assert _filas(nodo, "SELECT id, name FROM clients") == [(-1, "Ana")]
    assert _filas(nodo, "SELECT id, display_name FROM parties") == [(-1, "Ana")], "su party, con el mismo id"
    vid = _vender_a(nodo, ana, "Ana")

    # Sube en el orden en que pasó: el alta primero, así la venta encuentra a su cliente.
    ops = _pendientes(nodo)
    assert [(o.sequence, o.operation_type) for o in ops] == [(1, "cliente.alta"), (2, "venta.registrada")]
    alta, venta = ops
    assert alta.aggregate_type == "cliente" and alta.aggregate_id == "nodo-s02:cliente:-1"
    assert alta.payload["cliente_id"] == -1 and alta.payload["version"] == 1
    assert (alta.payload["name"], alta.payload["cuit_dni"], alta.payload["email"]) == (
        "Ana", "20-31781916-2", "ana@example.com")
    assert venta.payload["cliente_id"] == -1 and venta.aggregate_id == f"nodo-s02:venta:{vid}"

    assert [r.result for r in _subir(central, ops)] == ["accepted", "accepted"]

    # El central creó a Ana con SU secuencia (después de Juan y Maria) y la venta quedó a su nombre.
    assert _filas(central, "SELECT id, name, cuit_dni FROM clients ORDER BY id") == [
        (1, "Juan", "20-11111111-2"), (2, "Maria", "27-22222222-3"), (3, "Ana", "20-31781916-2")]
    assert _filas(central, "SELECT id, display_name FROM parties ORDER BY id") == [(1, "Juan"), (2, "Maria"), (3, "Ana")]
    assert _filas(central, "SELECT node_id, tabla, id_nodo, id_central FROM libraedge_mapeo ORDER BY tabla") == [
        ("nodo-s02", "clients", -1, 3), ("nodo-s02", "parties", -1, 3)]
    assert _filas(central, "SELECT customer_party_id, customer_name_snapshot FROM sales") == [(3, "Ana")]

    # Mientras el central no le devuelve el 3, el nodo sigue vendiéndole a -1: la venta de después también se traduce.
    _vender_a(nodo, ana, "Ana")
    segunda = _pendientes(nodo)[-1]
    assert segunda.payload["cliente_id"] == -1
    assert [r.result for r in _subir(central, [segunda])] == ["accepted"]
    assert _filas(central, "SELECT customer_party_id FROM sales ORDER BY id") == [(3,), (3,)]

    # Baja el cliente y su mapeo: el nodo reapunta lo suyo y borra la provisoria.
    aplicador = _bajar(central, nodo)
    assert aplicador.sin_reconciliar == {}
    assert _filas(nodo, "SELECT customer_party_id FROM sales ORDER BY id") == [(3,), (3,)]
    assert _filas(nodo, "SELECT id, name FROM clients ORDER BY id") == [(1, "Juan"), (2, "Maria"), (3, "Ana")]
    assert _filas(nodo, "SELECT id FROM parties ORDER BY id") == [(1,), (2,), (3,)], "sin ningún id negativo"
    assert _filas(nodo, "SELECT node_id, id_nodo, id_central FROM libraedge_mapeo WHERE tabla = 'clients'") == [
        ("nodo-s02", -1, 3)]


@pytest.fixture
def libro_sobre_sales():
    """El libro de clientes lee las ventas fiadas de `sales` (lo que registra un producto con LibraCommerce al armar su
    cuenta corriente); el default del motor es su tabla `ventas`."""
    from libracore.db import libro_de_clientes
    from libracore.db.cuenta_corriente import VENTAS_LIBRACOMMERCE, VENTAS_LIBRACORE

    libro_de_clientes.registrar_origen_de_ventas(VENTAS_LIBRACOMMERCE)
    yield
    libro_de_clientes.registrar_origen_de_ventas(VENTAS_LIBRACORE)


def test_el_fiado_a_un_cliente_provisorio_llega_a_su_cuenta_corriente_en_las_dos_puntas(  # noqa: F811
        puntas, libro_sobre_sales):
    """El libro de cuenta corriente guarda al cliente sin clave foránea (`tercero_id`): el nodo lo reapunta porque el
    producto lo declara (`REFERENCIAS_DE_CLIENTES`), y el central lo escribe a nombre del cliente traducido."""
    nodo, central = puntas
    _alta_en_el_central(central, "Juan")
    ana = _alta(nodo, "Ana", "20-31781916-2")
    _vender_a(nodo, ana, "Ana", medio="cuenta_corriente")
    assert _filas(nodo, "SELECT tercero_id, rol, debe FROM cc_asientos") == [(-1, "cliente", 200.0)]

    assert [r.result for r in _subir(central, _pendientes(nodo))] == ["accepted", "accepted"]
    assert _filas(central, "SELECT tercero_id, rol, debe FROM cc_asientos") == [(2, "cliente", 200.0)]

    _bajar(central, nodo)
    assert _filas(nodo, "SELECT tercero_id, rol, debe FROM cc_asientos") == [(2, "cliente", 200.0)]
    assert _filas(nodo, "SELECT id FROM clients WHERE id < 0") == []


def test_el_cliente_que_el_central_ya_tenia_no_se_duplica(puntas):  # noqa: F811
    """El nodo (con el espejo atrasado) da de alta a alguien que el central ya tiene, con el CUIT escrito de otra forma:
    el central usa al que tiene y **no toca** sus datos. El nodo termina con el cliente del central, no con el suyo."""
    nodo, central = puntas
    existente = _alta_en_el_central(central, "Ana Gomez SRL", "20-31781916-2")
    ana = _alta(nodo, "ana gomez", "20317819162")
    _vender_a(nodo, ana, "ana gomez")

    assert [r.result for r in _subir(central, _pendientes(nodo))] == ["accepted", "accepted"]

    assert _filas(central, "SELECT id, name FROM clients") == [(existente, "Ana Gomez SRL")], "no se creó otro"
    assert _filas(central, "SELECT customer_party_id FROM sales") == [(existente,)]
    assert _filas(central, "SELECT tabla, id_nodo, id_central FROM libraedge_mapeo ORDER BY tabla") == [
        ("clients", -1, existente), ("parties", -1, existente)]

    _bajar(central, nodo)
    assert _filas(nodo, "SELECT id, name FROM clients") == [(existente, "Ana Gomez SRL")]
    assert _filas(nodo, "SELECT customer_party_id FROM sales") == [(existente,)]


def test_dos_nodos_que_dan_de_alta_al_mismo_cliente_terminan_en_uno_solo(tmp_path):
    """Cada nodo lo llama -1 en su base. En el central es el mismo cliente (mismo CUIT) y cada nodo reapunta lo suyo."""
    nodo_a, nodo_b = _base("sqlite", tmp_path / "a.db"), _base("sqlite", tmp_path / "b.db")
    central = _base("sqlite", tmp_path / "central.db")
    for base in (nodo_a, nodo_b):
        assert _alta(base, "Ana", "20-31781916-2", quien=NODO_A if base is nodo_a else NODO_B) == -1
    _vender_a(nodo_a, -1, "Ana", quien=NODO_A)
    _vender_a(nodo_b, -1, "Ana", quien=NODO_B, monto=300.0)

    for base in (nodo_a, nodo_b):
        assert [r.result for r in _subir(central, _pendientes(base))] == ["accepted", "accepted"]

    assert _filas(central, "SELECT id, name FROM clients") == [(1, "Ana")]
    assert _filas(central, "SELECT node_id, tabla, id_nodo, id_central FROM libraedge_mapeo ORDER BY node_id, tabla") == [
        ("nodo-s02", "clients", -1, 1), ("nodo-s02", "parties", -1, 1),
        ("nodo-s03", "clients", -1, 1), ("nodo-s03", "parties", -1, 1)]
    assert _filas(central, "SELECT number, customer_party_id FROM sales ORDER BY number") == [
        ("S02-V-00001", 1), ("S03-V-00001", 1)]

    for base, quien in ((nodo_a, NODO_A), (nodo_b, NODO_B)):
        aplicador = _bajar(central, base, quien)
        assert aplicador.sin_reconciliar == {}
        assert _filas(base, "SELECT id FROM clients") == [(1,)]
        assert _filas(base, "SELECT customer_party_id FROM sales") == [(1,)]
        # El mapeo del otro nodo no le sirve a éste: se queda con el suyo.
        assert _filas(base, "SELECT node_id FROM libraedge_mapeo WHERE tabla = 'clients'") == [(quien.node_id,)]


def test_dos_clientes_sin_documento_con_el_mismo_nombre_son_dos_clientes(puntas):  # noqa: F811
    """Sin CUIT ni `external_ref` no hay con qué saber que son la misma persona: se prefiere duplicar a fusionar."""
    nodo, central = puntas
    _alta_en_el_central(central, "Juan Perez")
    juan = _alta(nodo, "Juan Perez")
    assert [r.result for r in _subir(central, _pendientes(nodo))] == ["accepted"]
    assert _filas(central, "SELECT id, name FROM clients ORDER BY id") == [(1, "Juan Perez"), (2, "Juan Perez")]
    assert _filas(central, "SELECT id_nodo, id_central FROM libraedge_mapeo WHERE tabla = 'clients'") == [(juan, 2)]


# ── Lo que falta o choca: rechazo, no reintento ─────────────────────────────


def test_una_venta_a_un_cliente_provisorio_sin_mapeo_se_rechaza_y_se_recupera_en_orden(puntas):  # noqa: F811
    """Si el alta no se aplicó (quedó en revisión), la venta no puede quedar a nombre de otro cliente: se rechaza con un
    mensaje claro, no traba la cola, y al resolver el alta se reintenta la venta."""
    from libraedge.sync.despacho import Despachador
    from libraedge.sync.revision import listar_en_revision, reintentar

    nodo, central = puntas
    _alta_en_el_central(central, "Juan")
    ana = _alta(nodo, "Ana", "20-31781916-2")
    _vender_a(nodo, ana, "Ana")
    alta, venta = _pendientes(nodo)

    resultado = _subir(central, [venta])[0]            # el alta no llegó: la venta sola
    assert resultado.result == "rejected"
    for pista in ("-1", "nodo-s02", "cliente.alta"):
        assert pista in resultado.error
    assert _filas(central, "SELECT COUNT(*) FROM sales") == [(0,)], "no quedó a nombre de nadie"

    assert [r.result for r in _subir(central, [alta])] == ["accepted"]
    with central.abrir() as conn:
        pendientes = [o.operation_id for o in listar_en_revision(conn)]
        assert pendientes == [venta.operation_id]
        despachador = offline.registrar_en(Despachador())
        assert reintentar(conn, venta.operation_id, despachador, por="test").result == "accepted"
    assert _filas(central, "SELECT customer_party_id, customer_name_snapshot FROM sales") == [(2, "Ana")]


def test_el_alta_aplicada_dos_veces_es_un_solo_cliente(puntas):  # noqa: F811
    nodo, central = puntas
    _alta(nodo, "Ana", "20-31781916-2")
    alta = _pendientes(nodo)[0]
    with central.abrir() as conn:
        offline_clientes.aplicar_cliente_alta(conn, alta)
        offline_clientes.aplicar_cliente_alta(conn, alta)
        conn.commit()
    assert _filas(central, "SELECT id, name FROM clients") == [(1, "Ana")]
    assert _filas(central, "SELECT COUNT(*) FROM libraedge_mapeo") == [(2,)]


def test_un_id_provisorio_reusado_por_otro_cliente_no_se_aplica(puntas):  # noqa: F811
    """Una PC reemplazada con el mismo `node_id` que arrancó la secuencia de cero vuelve a dar de alta un -1: las
    ventas a ese id irían al cliente del otro -1. Se rechaza para que lo mire una persona."""
    from dataclasses import replace

    nodo, central = puntas
    _alta(nodo, "Ana", "20-31781916-2")
    alta = _pendientes(nodo)[0]
    otra = replace(alta, operation_id="nodo-s02:900", sequence=900,
                   payload={**alta.payload, "name": "Beto", "cuit_dni": "20-99999999-6"})

    assert [r.result for r in _subir(central, [alta])] == ["accepted"]
    rechazo = _subir(central, [otra])[0]
    assert rechazo.result == "rejected" and "reusó el id provisorio -1" in rechazo.error
    assert _filas(central, "SELECT name FROM clients") == [("Ana",)]


@pytest.mark.parametrize("cambios, error", [
    ({"version": 2}, "versión 2"),
    ({"cliente_id": 5}, "no es un id provisorio"),
    ({"cliente_id": None}, "no es un id provisorio"),
    ({"name": "  "}, "no trae nombre"),
])
def test_un_alta_mal_formada_es_un_rechazo_claro(cambios, error):
    from libraedge.domain.sync import OutboxOperation

    payload = {"version": 1, "cliente_id": -1, "name": "Ana", "cuit_dni": "", **cambios}
    op = OutboxOperation(operation_id="n:1", node_id="n", sequence=1, operation_type="cliente.alta",
                         aggregate_type="cliente", aggregate_id="n:cliente:-1", occurred_at="x", schema_version=1,
                         payload=payload)
    with pytest.raises(offline_clientes.AltaDeClienteInvalida, match=error) as exc:
        offline_clientes.aplicar_cliente_alta(None, op)
    assert isinstance(exc.value, ValueError), "el receptor la manda a revisión manual y no traba la cola"


def test_el_alta_de_una_version_de_esquema_desconocida_se_rechaza():
    from libraedge.domain.sync import OutboxOperation

    op = OutboxOperation(operation_id="n:1", node_id="n", sequence=1, operation_type="cliente.alta",
                         aggregate_type="cliente", aggregate_id="n:cliente:-1", occurred_at="x", schema_version=2,
                         payload={"version": 1, "cliente_id": -1, "name": "Ana"})
    with pytest.raises(ValueError, match="schema 2"):
        offline_clientes.aplicar_cliente_alta(None, op)


# ── Fuera de un nodo nada cambia ────────────────────────────────────────────


def test_en_el_central_las_altas_son_las_de_siempre_y_no_encolan_nada(puntas):  # noqa: F811
    nodo, central = puntas
    juan = _alta_en_el_central(central, "Juan", "20-11111111-2")
    assert juan == 1
    with central.abrir() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM libraedge_mapeo").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM local_sequences").fetchone()[0] == 0


def test_un_cliente_del_central_que_el_nodo_espejo_viaja_con_su_id_y_no_se_traduce(puntas):  # noqa: F811
    """El nodo le vende a un cliente que ya existía en el central: su id es positivo, viaja tal cual y no hay mapeo."""
    nodo, central = puntas
    for base in (nodo, central):                              # el espejo ya lo trajo: mismo id en las dos puntas
        _alta_en_el_central(base, "Juan", "20-11111111-2")
    _vender_a(nodo, 1, "Juan")

    ops = _pendientes(nodo)
    assert [o.operation_type for o in ops] == ["venta.registrada"], "no hay alta que subir"
    assert ops[0].payload["cliente_id"] == 1
    assert [r.result for r in _subir(central, ops)] == ["accepted"]
    assert _filas(central, "SELECT customer_party_id FROM sales") == [(1,)]
    assert _filas(central, "SELECT COUNT(*) FROM libraedge_mapeo") == [(0,)]


def test_un_nodo_con_el_gancho_que_falla_no_deja_al_cliente_sin_su_alta(puntas):  # noqa: F811
    """El alta y su operación viven o mueren juntas: si encolar falla, el cliente no queda."""
    from libracore import altas_en_nodo
    from libracore.altas_en_nodo import AltasEnNodo

    nodo, _central = puntas
    base = offline_clientes.altas_en_nodo(OUTBOX)

    def falla(conn, datos):
        raise RuntimeError("el outbox falló")

    altas_en_nodo.configurar(AltasEnNodo(nodo=base.nodo, id_provisorio=base.id_provisorio, al_crear_cliente=falla))
    with pytest.raises(RuntimeError, match="outbox"):
        _alta(nodo, "Ana")
    assert _filas(nodo, "SELECT COUNT(*) FROM clients") == [(0,)]
    assert _filas(nodo, "SELECT COUNT(*) FROM parties") == [(0,)]
    assert _pendientes(nodo) == []


# ── Con los triggers de verdad (el central en PostgreSQL) ───────────────────


def test_el_changelog_por_trigger_baja_el_cliente_su_party_y_el_mapeo_en_ese_orden(puntas, request):  # noqa: F811
    """En producción lo que baja sale de los triggers del changelog, no de un volcado: el cliente, su party y el mapeo
    se escriben en la misma transacción del alta y tienen que bajar en ese orden (la definitiva antes que el mapeo)."""
    if "central-postgres" not in request.node.callspec.id:
        pytest.skip("los triggers del changelog son de PostgreSQL y el central de producción lo es")
    from libraedge.db.changelog import init_changelog_schema, instalar_trigger, listar_cambios
    from libraedge.mapeo import CLAVE, TABLA
    from libraedge.sync.pull import MirrorApplier

    nodo, central = puntas
    with central.abrir() as conn:
        init_changelog_schema(conn)
        for tabla, clave in (("clients", "id"), ("parties", "id"), (TABLA, CLAVE)):
            instalar_trigger(conn, tabla, clave)
        conn.commit()
    ana = _alta(nodo, "Ana", "20-31781916-2")
    _vender_a(nodo, ana, "Ana")
    assert [r.result for r in _subir(central, _pendientes(nodo))] == ["accepted", "accepted"]

    with central.abrir() as conn:
        cambios = [c for c in listar_cambios(conn, 0, 1000)]
    assert [c.table_name for c in cambios] == ["clients", "parties", TABLA, TABLA]
    assert cambios[2].row_id == '["nodo-s02", "clients", "-1"]'

    espejo, altas = _espejo_de_clientes()
    with nodo.abrir() as conn:
        aplicador = MirrorApplier(conn, espejo, node_id=NODO_A.node_id, altas=altas)
        for cambio in cambios:
            aplicador.aplicar(cambio)
        conn.commit()
    assert aplicador.sin_reconciliar == {}
    assert _filas(nodo, "SELECT id, name FROM clients") == [(1, "Ana")]
    assert _filas(nodo, "SELECT customer_party_id FROM sales") == [(1,)]
