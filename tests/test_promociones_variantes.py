"""Promociones y variantes (ADR-045): una variante recibe las promociones de su producto.

Las promociones se definen sobre PRODUCTOS (`promotion_items.item_id` es `catalog_items.id`) y la
variante no tiene precio ni categoría propios (`item_variants` es sólo `item_id`, `sku`, `name`,
atributos), así que `erp.promociones.calcular` agrupa por `producto_id` y la `variante_id` de la línea
no interviene: las variantes de un mismo producto **cuentan juntas**. Estos tests fijan esa regla en
los dos motores, por el cálculo (`POST /api/promociones/calcular`) y por la venta
(`OpcionesVentas.promociones`), porque lo que el POS muestra y lo que el servidor cobra tienen que ser
lo mismo. Si alguien un día agrupa por `(producto_id, variante_id)`, los de acá se ponen rojos.
"""

from __future__ import annotations

from test_promociones import HOY, _admin, _app, _calcular, _combo, _linea, _nxm, _producto  # noqa: F401

from libracommerce.web.ventas_router import OpcionesVentas


def _variantes(abrir, pid, *nombres):
    """Crea variantes del producto y devuelve sus ids, en el orden de los nombres."""
    with abrir() as conn:
        ids = [
            conn.execute(
                "INSERT INTO item_variants (item_id, sku, name) VALUES (?, ?, ?)",
                (pid, f"SKU-{pid}-{n}", n),
            ).lastrowid
            for n in nombres
        ]
        conn.commit()
    return ids


def _de_variante(pid, vid, qty, precio=100.0):
    return {"producto_id": pid, "variante_id": vid, "qty": qty, "precio": precio}


def _item_venta(pid, vid, qty, precio=100.0):
    return {"nombre": f"P{pid} (V{vid})", "qty": qty, "precio": precio, "producto_id": pid, "variante_id": vid}


def _vender(client, items, monto):
    return client.post("/api/ventas", json={
        "fecha": HOY, "items": items, "pagos": [{"medio": "efectivo", "monto": monto}]})


# ── el cálculo ───────────────────────────────────────────────────────────

def test_2x1_cuenta_juntas_a_dos_variantes_del_mismo_producto(abrir_ventas):
    client = _app(abrir_ventas)
    pid = _producto(client, "Remera")
    _nxm(client, pid)
    chico, grande = _variantes(abrir_ventas, pid, "Chico", "Grande")
    # El POS manda sólo el producto; igual acepta (e ignora) la variante.
    r = _calcular(client, [_linea(pid, 1), _linea(pid, 1)])
    assert r["ahorro"] == 100.0 and r["aplicadas"][0]["veces"] == 1
    r = _calcular(client, [_de_variante(pid, chico, 1), _de_variante(pid, grande, 1)])
    assert r["ahorro"] == 100.0 and r["aplicadas"][0]["veces"] == 1


def test_una_sola_variante_con_dos_unidades_tambien_arma_el_2x1(abrir_ventas):
    client = _app(abrir_ventas)
    pid = _producto(client, "Remera")
    _nxm(client, pid)
    (chico,) = _variantes(abrir_ventas, pid, "Chico")
    assert _calcular(client, [_de_variante(pid, chico, 2)])["ahorro"] == 100.0


def test_3x2_con_tres_variantes_distintas_y_una_suelta(abrir_ventas):
    client = _app(abrir_ventas)
    pid = _producto(client, "Remera")
    _nxm(client, pid, lleva=3, paga=2)
    a, b, c = _variantes(abrir_ventas, pid, "A", "B", "C")
    # 3 + 1 = 4 unidades del producto: un paquete de 3 y una suelta que se paga entera.
    r = _calcular(client, [_de_variante(pid, a, 1), _de_variante(pid, b, 1), _de_variante(pid, c, 1),
                           _de_variante(pid, a, 1)])
    assert r["aplicadas"][0]["veces"] == 1 and r["ahorro"] == 100.0


def test_una_variante_de_otro_producto_no_suma_a_la_promocion(abrir_ventas):
    client = _app(abrir_ventas)
    a, b = _producto(client, "Remera"), _producto(client, "Buzo")
    _nxm(client, a)
    (va,) = _variantes(abrir_ventas, a, "Chico")
    (vb,) = _variantes(abrir_ventas, b, "Chico")
    r = _calcular(client, [_de_variante(a, va, 1), _de_variante(b, vb, 1)])
    assert r == {"aplicadas": [], "ahorro": 0}


def test_combo_con_un_producto_con_variantes_y_otro_sin(abrir_ventas):
    client = _app(abrir_ventas)
    remera, gorra = _producto(client, "Remera", 500), _producto(client, "Gorra", 300)
    _combo(client, [remera, gorra], precio=650.0)
    (chico,) = _variantes(abrir_ventas, remera, "Chico")
    r = _calcular(client, [_de_variante(remera, chico, 1, 500.0), _linea(gorra, 1, 300.0)])
    assert r["ahorro"] == 150.0
    # Dos remeras de talles distintos y una gorra: el combo se arma una vez.
    (grande,) = _variantes(abrir_ventas, remera, "Grande")
    r = _calcular(client, [_de_variante(remera, chico, 1, 500.0), _de_variante(remera, grande, 1, 500.0),
                           _linea(gorra, 1, 300.0)])
    assert r["aplicadas"][0]["veces"] == 1 and r["ahorro"] == 150.0


# ── la venta ─────────────────────────────────────────────────────────────

def test_la_venta_con_dos_variantes_aplica_el_2x1_y_no_toca_las_lineas(abrir_ventas):
    client = _app(abrir_ventas, OpcionesVentas(promociones=True))
    pid = _producto(client, "Remera")
    promo = _nxm(client, pid)
    chico, grande = _variantes(abrir_ventas, pid, "Chico", "Grande")
    r = _vender(client, [_item_venta(pid, chico, 1), _item_venta(pid, grande, 1)], monto=100.0)
    assert r.status_code == 200, r.text
    venta = r.json()
    assert venta["subtotal"] == 200.0 and venta["descuento"] == 100.0 and venta["total"] == 100.0
    assert venta["promociones"] == [{"promocion_id": promo["id"], "nombre": "2x1", "veces": 1, "ahorro": 100.0}]
    # Una línea por variante, a su precio de lista: el stock, el costo y las devoluciones siguen siendo por variante.
    assert [(i["variante_id"], i["qty"], i["precio"]) for i in venta["items"]] == [
        (chico, 1, 100.0), (grande, 1, 100.0)]


def test_el_servidor_calcula_lo_mismo_que_mostro_el_calculo(abrir_ventas):
    client = _app(abrir_ventas, OpcionesVentas(promociones=True))
    pid = _producto(client, "Remera")
    _nxm(client, pid, lleva=3, paga=2)
    a, b = _variantes(abrir_ventas, pid, "A", "B")
    lineas = [_de_variante(pid, a, 2), _de_variante(pid, b, 1)]
    ahorro = _calcular(client, lineas)["ahorro"]
    assert ahorro == 100.0
    r = _vender(client, [_item_venta(pid, a, 2), _item_venta(pid, b, 1)], monto=300.0 - ahorro)
    assert r.status_code == 200, r.text
    assert r.json()["descuento"] == ahorro
