"""`true`/`false` no son un número en los cuerpos de los routers (ADR-026, ADR-027), contra los DOS motores.

Los campos `int`/`float` de pydantic convierten `true` en `1` y `false` en `0` antes de que el motor (que en varios lugares los rechaza a propósito) los vea:
`{"proveedor_id": true}` entraba como el proveedor 1 y `{"descuento": true}` como un descuento de 1. ADR-026 lo cerró para la reposición con `sin_booleanos`; ADR-027 lo
aplica en el resto de los routers, **sólo** donde un 1 o un 0 cambian algo del negocio (ids, cantidades, precios, porcentajes).

Cada caso es un endpoint con un cuerpo válido y la lista de sus campos numéricos. Para cada campo, `true` y `false` dan **422 (con «<campo> tiene que ser un número, no un
booleano») y no escriben nada** (se compara el contenido de TODAS las tablas antes y después); el cuerpo numérico y el mismo cuerpo con los números como texto (`"2"`) siguen
funcionando. El relevamiento de los routers que no necesitaron el arreglo (los `Decimal`, que pydantic ya rechaza solo, y los que no tienen campos numéricos) queda en ADR-027.
"""

from __future__ import annotations

import copy
import itertools
import sqlite3

import pytest
import test_vencimientos as _vto
from conftest import USUARIO, _usuario
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from test_vencimientos import HOY, _dias, _entrada, _hoy_fijo, _k, _libre, _producto

from libracommerce.db.repository import repositorio_de
from libracommerce.domain.entities import Party, PartyType
from libracommerce.erp import catalogo
from libracommerce.web.catalogo_router import (
    OpcionesStock,
    build_depositos_router,
    build_productos_router,
    build_stock_router,
    build_sucursales_router,
)
from libracommerce.web.compras_router import build_compras_router
from libracommerce.web.listas_router import (
    build_cliente_lista_router,
    build_listas_precio_router,
    build_precios_vigentes_router,
    build_quiebres_router,
)
from libracommerce.web.promociones_router import build_promociones_calculo_router, build_promociones_router
from libracommerce.web.vencimientos_router import build_vencimientos_escritura_router
from libracommerce.web.ventas_router import OpcionesVentas, build_ventas_router

# Las fixtures de `tests/test_vencimientos.py` (una base por motor con la revisión 0002) para los tres POST del ledger de vencimientos.
abrir_vto = _vto.abrir_vto
destino = _vto.destino

_HOY = HOY.isoformat()


# ═══════════════════════════════════════════════════ El mecanismo: un caso es un endpoint


def _instantanea(abrir) -> dict:
    """El contenido de TODAS las tablas, para afirmar que un 422 no escribió nada en ninguna."""
    with abrir() as conn:
        if isinstance(conn, sqlite3.Connection):
            tablas = [f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        else:
            tablas = [f[0] for f in conn.execute("SELECT table_name FROM information_schema.tables "
                                                 "WHERE table_schema='public' AND table_type='BASE TABLE'").fetchall()]
        return {t: sorted(repr(tuple(f)) for f in conn.execute(f"SELECT * FROM {t}").fetchall()) for t in sorted(tablas)}


def _poner(cuerpo: dict, ruta: tuple, valor) -> dict:
    copia = copy.deepcopy(cuerpo)
    destino_ = copia
    for clave in ruta[:-1]:
        destino_ = destino_[clave]
    destino_[ruta[-1]] = valor
    return copia


def _nombre(ruta: tuple) -> str:
    """El nombre del campo en el mensaje: el último que es texto y no una clave numérica (`("precios", "7")` es `precios`)."""
    return [p for p in ruta if isinstance(p, str) and not p.isdigit()][-1]


def _como_texto(cuerpo: dict, ruta: tuple) -> dict:
    """El mismo cuerpo con el número de `ruta` escrito como texto (`2` pasa a `"2"`): pydantic lo convierte igual que antes."""
    destino_ = cuerpo
    for clave in ruta[:-1]:
        destino_ = destino_[clave]
    return _poner(cuerpo, ruta, str(destino_[ruta[-1]]))


def _verificar(client, abrir, metodo: str, url: str, cuerpo_ok, campos: list[tuple], *, veces: int = 2, estado_ok: tuple = (200,)):
    """`cuerpo_ok(n)` arma el cuerpo válido del intento `n` (para lo que no se puede repetir con el mismo texto: nombres, claves)."""
    base = cuerpo_ok(0)
    antes = _instantanea(abrir)
    for campo in campos:
        for valor in (True, False):
            r = client.request(metodo, url, json=_poner(base, campo, valor))
            assert r.status_code == 422, (campo, valor, r.status_code, r.text)
            assert f"{_nombre(campo)} tiene que ser un número, no un booleano" in r.text, (campo, valor, r.text)
    assert _instantanea(abrir) == antes, "un 422 por booleano escribió algo"
    # El cuerpo numérico de siempre sigue funcionando...
    r = client.request(metodo, url, json=base)
    assert r.status_code in estado_ok, r.text
    if veces < 2:
        return
    # ...y, con los números como texto, cada campo por separado.
    intento = itertools.count(1)
    for campo in campos:
        n = next(intento)
        otro = cuerpo_ok(n)
        try:
            cuerpo = _como_texto(otro, campo)
        except (KeyError, TypeError):
            continue        # el campo no viene en el cuerpo válido (un opcional): sólo se prueba el rechazo
        r = client.request(metodo, url, json=cuerpo)
        assert r.status_code in estado_ok, (campo, r.status_code, r.text)


def _rechaza_siempre(client, abrir, metodo: str, url: str, cuerpo: dict, campos: list[tuple]):
    """Los `Decimal` ya rechazaban `true`/`false` solos (otro mensaje de pydantic); queda fijado que siga así y que no escriben."""
    antes = _instantanea(abrir)
    for campo in campos:
        for valor in (True, False):
            r = client.request(metodo, url, json=_poner(cuerpo, campo, valor))
            assert r.status_code == 422, (campo, valor, r.status_code, r.text)
    assert _instantanea(abrir) == antes


# ═══════════════════════════════════════════════════ El mundo de las pruebas


class Mundo:
    """Los routers montados como los monta un producto, y los ids que sus cuerpos necesitan."""


@pytest.fixture
def mundo(abrir_ventas):
    abrir = abrir_ventas
    app = FastAPI()
    app.include_router(build_productos_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_depositos_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_sucursales_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_stock_router(conexion=abrir, usuario_actual=_usuario, opciones=OpcionesStock(por_deposito=True)))
    app.include_router(build_ventas_router(conexion=abrir, usuario_actual=_usuario,
                                           opciones=OpcionesVentas(promociones=True, con_avisos_de_vencimiento=True)))
    app.include_router(build_listas_precio_router(conexion=abrir))
    app.include_router(build_quiebres_router(conexion=abrir))
    app.include_router(build_precios_vigentes_router(conexion=abrir))
    app.include_router(build_cliente_lista_router(conexion=abrir))
    app.include_router(build_compras_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_promociones_router(conexion=abrir))
    app.include_router(build_promociones_calculo_router(conexion=abrir))
    w = Mundo()
    w.abrir, w.client = abrir, TestClient(app)
    c = w.client

    def post(url, **cuerpo):
        r = c.post(url, json=cuerpo)
        assert r.status_code == 200, (url, r.text)
        return r.json()

    w.p1 = post("/api/productos", nombre="Yerba", precio_venta=100.0, precio_costo=60.0)["id"]
    w.p2 = post("/api/productos", nombre="Azúcar", precio_venta=50.0, precio_costo=30.0)["id"]
    w.dep1 = c.get("/api/depositos").json()[0]["id"]
    w.dep2 = post("/api/depositos", nombre="Otro")["id"]
    sucursal = post("/api/sucursales", nombre="Casa Central")
    w.suc = sucursal["id"]
    w.dep_suc = sucursal["deposito_predeterminado_id"]
    post(f"/api/stock/{w.p1}/ajuste", modo="entrada", cantidad=100, deposito_id=w.dep1)
    w.lista = post("/api/listas-precio", nombre="Mayorista")["id"]
    w.lista2 = post("/api/listas-precio", nombre="Otra")["id"]
    assert c.put(f"/api/listas-precio/{w.lista}/items", json={"precios": {str(w.p1): 90}}).status_code == 200
    w.cliente = 41
    with abrir() as conn:
        conn.execute("INSERT INTO clients (id, name) VALUES (?, ?)", (w.cliente, "Cliente"))
        conn.execute("ALTER TABLE ventas_pagos ADD COLUMN recibido NUMERIC")   # la agrega el producto (VentaLibra); sin ella `recibido` no se escribe
        conn.commit()
        w.proveedor = repositorio_de(conn).save_party(Party(None, PartyType.ORGANIZATION, "Distribuidora SA")).id
    venta = post("/api/ventas", fecha=_HOY, items=[{"nombre": "Yerba", "qty": 5, "precio": 100.0, "producto_id": w.p1}],
                 pagos=[{"medio": "efectivo", "monto": 500.0}], deposito_id=w.dep1)
    w.venta = venta["id"]
    with abrir() as conn:
        w.linea = conn.execute("SELECT id FROM sale_items WHERE sale_id = ?", (w.venta,)).fetchone()["id"]
    return w


@pytest.fixture(autouse=True)
def _admin():
    USUARIO["role"] = "admin"
    yield
    USUARIO.pop("role", None)


# ═══════════════════════════════════════════════════ Catálogo, depósitos, sucursales y stock


def test_productos_alta_y_edicion(mundo):
    w = mundo
    campos = [("precio_venta",), ("precio_costo",), ("stock_minimo",)]
    ok = lambda n: {"nombre": f"Fideos {n}", "precio_venta": 120, "precio_costo": 70, "stock_minimo": 3}  # noqa: E731
    _verificar(w.client, w.abrir, "POST", "/api/productos", ok, campos)
    _verificar(w.client, w.abrir, "PUT", f"/api/productos/{w.p2}", ok, campos)


def test_un_booleano_no_queda_como_precio_ni_como_minimo(mundo):
    """El defecto de punta a punta: antes `precio_venta: true` dejaba el producto en 1.0."""
    w = mundo
    r = w.client.put(f"/api/productos/{w.p2}", json={"nombre": "Azúcar", "precio_venta": True, "precio_costo": 30, "stock_minimo": 2})
    assert r.status_code == 422
    with w.abrir() as conn:
        assert catalogo.get_producto(conn, w.p2)["precio_venta"] == 50.0


def test_deposito_con_sucursal(mundo):
    w = mundo
    ok = lambda n: {"nombre": f"Depósito {n}", "branch_id": w.suc}  # noqa: E731
    _verificar(w.client, w.abrir, "POST", "/api/depositos", ok, [("branch_id",)])


def test_transferencia_entre_depositos(mundo):
    w = mundo
    ok = lambda n: {"producto_id": w.p1, "origen_id": w.dep1, "destino_id": w.dep2, "cantidad": 1}  # noqa: E731
    # `variant_id` no viene en el cuerpo válido (el producto no tiene variantes): sólo se prueba que `true`/`false` se rechazan.
    _verificar(w.client, w.abrir, "POST", "/api/depositos/transferir", ok,
               [("producto_id",), ("origen_id",), ("destino_id",), ("cantidad",), ("variant_id",)])


def test_deposito_predeterminado_de_una_sucursal(mundo):
    w = mundo
    ok = lambda n: {"deposito_id": w.dep_suc}  # noqa: E731
    _verificar(w.client, w.abrir, "POST", f"/api/sucursales/{w.suc}/deposito-predeterminado", ok, [("deposito_id",)])


def test_ajuste_de_stock(mundo):
    w = mundo
    ok = lambda n: {"modo": "entrada", "cantidad": 2, "factor": 1, "deposito_id": w.dep1}  # noqa: E731
    _verificar(w.client, w.abrir, "POST", f"/api/stock/{w.p1}/ajuste", ok,
               [("cantidad",), ("factor",), ("deposito_id",), ("variant_id",)])
    # El conteo en cero es legítimo (`0` numérico); `false` no es un cero.
    assert w.client.post(f"/api/stock/{w.p2}/ajuste", json={"modo": "absoluto", "cantidad": 0}).status_code == 200
    assert w.client.post(f"/api/stock/{w.p2}/ajuste", json={"modo": "absoluto", "cantidad": False}).status_code == 422


# ═══════════════════════════════════════════════════ Ventas


def test_venta(mundo):
    w = mundo
    ok = lambda n: {"fecha": _HOY, "items": [{"nombre": "Yerba", "qty": 2, "precio": 100.0, "producto_id": w.p1}],  # noqa: E731
                    "descuento": 0, "cliente_id": w.proveedor, "deposito_id": w.dep1,   # `sales.customer_party_id` es una FK a `parties`
                    "pagos": [{"medio": "efectivo", "monto": 200.0, "recibido": 200.0}]}
    campos = [("items", 0, "qty"), ("items", 0, "precio"), ("items", 0, "producto_id"), ("items", 0, "variante_id"),
              ("descuento",), ("cliente_id",), ("deposito_id",), ("pagos", 0, "monto"), ("pagos", 0, "recibido")]
    _verificar(w.client, w.abrir, "POST", "/api/ventas", ok, campos)


def test_devolucion(mundo):
    w = mundo
    ok = lambda n: {"lineas": [{"sale_item_id": w.linea, "cantidad": 1}], "deposito_id": w.dep1}  # noqa: E731
    _verificar(w.client, w.abrir, "POST", f"/api/ventas/{w.venta}/devolver", ok,
               [("lineas", 0, "sale_item_id"), ("lineas", 0, "cantidad"), ("deposito_id",)])


def test_plan_de_salida_es_lectura_pero_alimenta_el_cobro(mundo):
    w = mundo
    ok = lambda n: {"items": [{"producto_id": w.p1, "qty": 1}], "deposito_id": w.dep1}  # noqa: E731
    _verificar(w.client, w.abrir, "POST", "/api/ventas/plan-salida", ok,
               [("items", 0, "producto_id"), ("items", 0, "qty"), ("items", 0, "variante_id"), ("deposito_id",)],
               estado_ok=(200,))


# ═══════════════════════════════════════════════════ Listas de precio, quiebres, precios con vigencia y lista del cliente


def test_listas_de_precio(mundo):
    w = mundo
    _verificar(w.client, w.abrir, "PUT", f"/api/listas-precio/{w.lista}/items",
               lambda n: {"precios": {str(w.p1): 90, str(w.p2): 40}}, [("precios", str(w.p1)), ("precios", str(w.p2))])
    _verificar(w.client, w.abrir, "POST", f"/api/listas-precio/{w.lista}/ajuste-porcentual",
               lambda n: {"porcentaje": 10, "base": "lista"}, [("porcentaje",)])
    _verificar(w.client, w.abrir, "POST", f"/api/listas-precio/{w.lista2}/importar",
               lambda n: {"fuente": "lista", "fuente_lista_id": w.lista}, [("fuente_lista_id",)])


def test_un_ajuste_porcentual_booleano_no_mueve_los_precios(mundo):
    """El defecto de punta a punta: `porcentaje: true` aplicaba +1 % a toda la lista."""
    w = mundo
    antes = w.client.get(f"/api/listas-precio/{w.lista}/items").json()
    assert w.client.post(f"/api/listas-precio/{w.lista}/ajuste-porcentual", json={"porcentaje": True, "base": "lista"}).status_code == 422
    assert w.client.get(f"/api/listas-precio/{w.lista}/items").json() == antes


def test_quiebres_por_cantidad(mundo):
    w = mundo
    ok = lambda n: {"quiebres": [{"min_quantity": 10 + n, "amount": 80}]}  # noqa: E731
    _verificar(w.client, w.abrir, "PUT", f"/api/listas-precio/{w.lista}/items/{w.p1}/quiebres", ok,
               [("quiebres", 0, "min_quantity"), ("quiebres", 0, "amount")])


def test_precio_vigente(mundo):
    w = mundo
    ok = lambda n: {"monto": 75 + n, "sucursal_id": w.suc, "cantidad_minima": 2}  # noqa: E731
    _verificar(w.client, w.abrir, "POST", f"/api/listas-precio/{w.lista}/items/{w.p1}/precio-vigente", ok,
               [("monto",), ("sucursal_id",), ("cantidad_minima",)])


def test_lista_del_cliente(mundo):
    w = mundo
    _verificar(w.client, w.abrir, "PUT", f"/api/clientes/{w.cliente}/lista-precio", lambda n: {"lista_id": w.lista}, [("lista_id",)])
    # Quitar la lista es `null`, no `false`.
    assert w.client.put(f"/api/clientes/{w.cliente}/lista-precio", json={"lista_id": None}).status_code == 200


# ═══════════════════════════════════════════════════ Compras


def test_compras(mundo):
    w = mundo
    c = w.client
    _verificar(c, w.abrir, "POST", "/api/purchase-orders", lambda n: {"proveedor_id": w.proveedor, "branch_id": w.suc},
               [("proveedor_id",), ("branch_id",)])
    orden = c.post("/api/purchase-orders", json={"proveedor_id": w.proveedor}).json()["id"]
    _verificar(c, w.abrir, "POST", f"/api/purchase-orders/{orden}/items",
               lambda n: {"item_id": w.p1, "quantity_ordered": 10, "unit_cost": 100}, [("item_id",)])
    # Los `Decimal` ya rechazaban el booleano solos: queda fijado.
    _rechaza_siempre(c, w.abrir, "POST", f"/api/purchase-orders/{orden}/items", {"item_id": w.p1, "quantity_ordered": 10, "unit_cost": 100, "tax_rate": 0},
                     [("quantity_ordered",), ("unit_cost",), ("tax_rate",)])
    _verificar(c, w.abrir, "POST", "/api/purchase-receipts", lambda n: {"proveedor_id": w.proveedor, "purchase_order_id": orden},
               [("proveedor_id",), ("purchase_order_id",)])
    recepcion = c.post("/api/purchase-receipts", json={"proveedor_id": w.proveedor}).json()["id"]
    _verificar(c, w.abrir, "POST", f"/api/purchase-receipts/{recepcion}/items",
               lambda n: {"item_id": w.p1, "quantity": 2, "unit_cost": 100}, [("item_id",)])
    _rechaza_siempre(c, w.abrir, "POST", f"/api/purchase-receipts/{recepcion}/items", {"item_id": w.p1, "quantity": 2, "unit_cost": 100},
                     [("quantity",), ("unit_cost",)])
    # Confirmar es una sola vez: el cuerpo válido va último.
    _verificar(c, w.abrir, "POST", f"/api/purchase-receipts/{recepcion}/confirm", lambda n: {"deposito_id": w.dep1},
               [("deposito_id",)], veces=1)


# ═══════════════════════════════════════════════════ Promociones


def test_promociones(mundo):
    w = mundo
    nxm = lambda n: {"nombre": f"2x1 {n}", "tipo": "nxm", "paga": 1, "items": [{"producto_id": w.p1, "cantidad": 2}]}  # noqa: E731
    campos_nxm = [("paga",), ("items", 0, "producto_id"), ("items", 0, "cantidad")]
    _verificar(w.client, w.abrir, "POST", "/api/promociones", nxm, campos_nxm)
    combo = lambda n: {"nombre": f"Combo {n}", "tipo": "combo", "precio": 120,  # noqa: E731
                       "items": [{"producto_id": w.p1, "cantidad": 1}, {"producto_id": w.p2, "cantidad": 1}]}
    _verificar(w.client, w.abrir, "POST", "/api/promociones", combo, [("precio",)])
    promocion = w.client.post("/api/promociones", json=nxm(9)).json()["id"]
    _verificar(w.client, w.abrir, "PUT", f"/api/promociones/{promocion}", nxm, campos_nxm)


def test_calculo_de_promociones(mundo):
    w = mundo
    ok = lambda n: {"items": [{"producto_id": w.p1, "qty": 2, "precio": 100}]}  # noqa: E731
    _verificar(w.client, w.abrir, "POST", "/api/promociones/calcular", ok,
               [("items", 0, "producto_id"), ("items", 0, "qty"), ("items", 0, "precio")])


# ═══════════════════════════════════════════════════ Vencimientos: los tres POST que escriben el ledger


def test_vencimientos_asignar_entrada_y_merma(abrir_vto, monkeypatch):
    _hoy_fijo(monkeypatch)
    with abrir_vto() as conn:
        pid = _producto(conn, "Yogur")
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 50)                                            # «sin lote», para asignar
        _entrada(conn, pid, 20, lote="L1", vence=_dias(30))                # un lote, para dar de baja
    app = FastAPI()
    app.include_router(build_vencimientos_escritura_router(
        conexion=abrir_vto, usuario_actual=lambda: USUARIO, dependencias_marcar=[Depends(_libre)], dependencias_movimientos=[Depends(_libre)]))
    c = TestClient(app)
    campos = [("producto_id",), ("deposito_id",), ("variante_id",)]
    base = {"producto_id": pid, "deposito_id": deposito, "cantidad": 1}
    asignar = lambda n: {**base, "lote": f"L{n + 5}", "vence": _dias(40), "clave_operacion": _k()}  # noqa: E731
    entrada = lambda n: {**base, "lote": f"E{n}", "vence": _dias(40), "clave_operacion": _k()}  # noqa: E731
    merma = lambda n: {**base, "lote": "L1", "vence": _dias(30), "clave_operacion": _k()}  # noqa: E731
    for url, cuerpo in (("asignar", asignar), ("entrada", entrada), ("merma", merma)):
        # Con `clave_operacion` distinta en cada intento (`_k()`): un reintento con la misma clave sería «repetida», no un error.
        _verificar(c, abrir_vto, "POST", f"/api/vencimientos/{url}", cuerpo, campos)
        # `cantidad` es `Decimal`: ya rechazaba el booleano solo.
        _rechaza_siempre(c, abrir_vto, "POST", f"/api/vencimientos/{url}", cuerpo(0), [("cantidad",)])
