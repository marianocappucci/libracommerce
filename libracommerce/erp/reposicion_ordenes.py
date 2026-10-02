"""Órdenes de compra en borrador desde la reposición sugerida (2026-10-02, ADR-022).

Cierra el circuito de la reposición: lo que `erp.reposicion.sugerencia_reposicion` dice que hay que pedir se convierte en **una orden de
compra en `draft` por proveedor habitual** (`catalog_items.supplier_party_id`, ADR-021), con una línea por producto. **Nunca envía ni
confirma nada**: una orden en borrador es un papel de trabajo que una persona revisa, ajusta y manda; el motor ni siquiera tiene una operación
que pase una orden a `sent`.

**Qué se pide.** Exactamente el `sugerido` de la reposición con los mismos parámetros (los que se le pasan a esta función son los de
`sugerencia_reposicion`), siempre `solo_a_pedir`. `producto_ids` acota a esos productos; uno que no tiene nada que pedir se informa en `omitidos`.
El costo de cada línea es `catalog_items.default_cost` (el costo vigente del producto), que puede ser 0 si nunca se cargó: la línea lo
marca (`costo_cero`) para que la persona lo complete. Sin IVA (`tax_rate` 0): se carga al recibir.

**Qué no se pide.** Un producto sin proveedor habitual no entra en ninguna orden: va en `sin_proveedor`, con su sugerido, para que se le asigne uno.

**Sucursal.** Con `sucursal_id` las órdenes llevan esa sucursal (`purchase_orders.branch_id`) y la reposición se calculó para ella.

**No se duplica.** Las órdenes en borrador cuentan como «en camino» en la reposición (ADR-017), así que generar dos veces seguidas la segunda no
encuentra nada que pedir. Además `clave_operacion` (obligatoria) hace idempotente un reintento exacto: se estampa `[op:<clave>]` en las
`notes` de cada orden creada, y una clave ya usada devuelve esas mismas órdenes (`repetida: true`) sin crear otras.

**Atómica.** Todas las órdenes se crean en la transacción de quien llama (no commitea): si algo falla, no queda ninguna. La numeración la da el
`numerador` que se pase (el default del motor es `MAX(id)+1`, ver `erp.compras.numero_por_defecto`; un producto con concurrencia real pasa el suyo).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from ..domain.purchasing import PurchaseOrder, PurchaseOrderItem, PurchaseOrderStatus
from . import compras, reposicion

MAX_LARGO_CLAVE = 64
_CERO = Decimal("0")


def _clave(clave) -> str:
    if not isinstance(clave, str) or not clave.strip() or len(clave.strip()) > MAX_LARGO_CLAVE:
        raise ValueError(f"clave_operacion tiene que ser un texto de 1 a {MAX_LARGO_CLAVE} caracteres (un UUID por intento): {clave!r}")
    return clave.strip()


def _marca(clave: str) -> str:
    return f"[op:{clave}]"


def _ordenes_de_la_clave(conn, clave: str) -> list[dict]:
    ids = [f[0] for f in conn.execute(
        "SELECT id FROM purchase_orders WHERE notes LIKE ? ORDER BY id", (f"%{_marca(clave)}%",)).fetchall()]
    return [compras.obtener_orden(conn, i) for i in ids]


def _nombres_y_costos(conn, ids: list[int]) -> dict[int, tuple[str, Decimal]]:
    if not ids:
        return {}
    filas = conn.execute(
        f"SELECT id, name, default_cost FROM catalog_items WHERE id IN ({','.join('?' for _ in ids)})", ids).fetchall()
    return {f["id"]: (f["name"], Decimal(str(f["default_cost"] if f["default_cost"] is not None else 0))) for f in filas}


def _vista(conn, orden: dict, nombres: dict[int, tuple[str, Decimal]] | None = None) -> dict:
    """La orden creada con lo que la pantalla necesita: líneas con nombre, costo, subtotal y el total."""
    ids = [i["item_id"] for i in orden["items"]]
    nombres = nombres or _nombres_y_costos(conn, ids)
    lineas = []
    total = _CERO
    for i in orden["items"]:
        costo = Decimal(i["unit_cost"])
        sub = Decimal(i["subtotal"])
        total += sub
        lineas.append({"producto_id": i["item_id"], "nombre": nombres.get(i["item_id"], ("", _CERO))[0],
                       "cantidad": i["quantity_ordered"], "costo_unitario": i["unit_cost"], "subtotal": i["subtotal"],
                       "costo_cero": costo == 0})
    proveedor = conn.execute("SELECT display_name FROM parties WHERE id = ?", (orden["supplier_party_id"],)).fetchall()
    return {"id": orden["id"], "number": orden["number"], "supplier_party_id": orden["supplier_party_id"],
            "proveedor": proveedor[0]["display_name"] if proveedor else None, "branch_id": orden["branch_id"],
            "status": orden["status"], "lineas": lineas, "total": str(total)}


def _insertar(conn, orden: PurchaseOrder) -> int:
    """Escribe la orden y sus líneas **sin commitear**. `repositorio_de(conn).save_purchase_order` commitea cada vez, y con eso las
    órdenes ya creadas quedarían confirmadas si una de las siguientes falla: acá se necesita todo o nada. Las mismas columnas que él."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO purchase_orders (number, supplier_party_id, branch_id, status, ordered_at, expected_at, notes, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (orden.number, orden.supplier_party_id, orden.branch_id, orden.status, None, None, orden.notes, orden.created_by),
    )
    orden_id = cur.lastrowid
    for linea in orden.items:
        cur.execute(
            "INSERT INTO purchase_order_items (purchase_order_id, item_id, quantity_ordered, quantity_received, unit_cost, tax_rate) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (orden_id, linea.item_id, str(linea.quantity_ordered), "0", str(linea.unit_cost), str(linea.tax_rate)),
        )
    return orden_id


def generar_ordenes_borrador(conn, *, clave_operacion, producto_ids: list[int] | None = None, topes: dict[int, object] | None = None,
                             usuario_id: int | None = None,
                             numerador: compras.Numerador = compras.numero_por_defecto,
                             hoy: datetime.date | None = None, **parametros) -> dict:
    """Crea una orden de compra en borrador por proveedor habitual con lo que la reposición dice que hay que pedir. Devuelve
    `{ordenes, sin_proveedor, omitidos, repetida}`: las órdenes creadas (ver `_vista`), los productos a pedir que no tienen proveedor
    habitual (`producto_id`, `nombre`, `sugerido`), los `producto_ids` pedidos que no tienen nada que pedir, y si es el reintento de una
    clave ya usada. `topes` (`{producto_id: cantidad}`) es lo que la persona vio y confirmó: la cantidad de cada línea es el **menor** entre el
    sugerido de ahora y su tope, así que si entre la vista previa y el pedido el stock bajó y el sugerido subió, la orden no se pasa de lo confirmado
    (si bajó, se pide menos); un producto sin tope se pide por el sugerido. `parametros` son los de `sugerencia_reposicion` (`dias_rotacion`, `dias_cobertura`, `plazo_entrega_dias`, `sucursal_id`,
    `categoria`, `proveedor_id` —un `party_id`—, `descontar_vencido`); `solo_a_pedir` no se acepta. `ValueError` con un parámetro inválido o
    una `clave_operacion` mal formada; `reposicion.SinRevision` sin la `0004`. No commitea."""
    clave = _clave(clave_operacion)
    if "solo_a_pedir" in parametros:
        raise ValueError("solo_a_pedir no se acepta: las órdenes salen siempre de lo que hay que pedir")
    if not reposicion.tiene_proveedor(conn):
        raise reposicion.SinRevision("Falta la revisión 0004_proveedor_por_producto del motor: corré `libracommerce-migrar upgrade` "
                                     "(--prefijo del producto) antes de generar órdenes desde la reposición.")
    if producto_ids is not None and (not isinstance(producto_ids, list) or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in producto_ids)):
        raise ValueError("producto_ids tiene que ser una lista de ids de producto")

    limites: dict[int, Decimal] = {}
    for k, v in (topes or {}).items():
        if isinstance(k, bool) or not isinstance(k, int) or isinstance(v, bool) or not isinstance(v, (int, float, str, Decimal)):
            raise ValueError(f"topes tiene que ser {{producto_id: cantidad}}: {k!r}: {v!r}")
        try:
            tope = Decimal(str(v))
        except ArithmeticError as e:
            raise ValueError(f"el tope del producto {k} no es un número: {v!r}") from e
        if not tope.is_finite() or tope <= 0:
            raise ValueError(f"el tope del producto {k} tiene que ser mayor que 0: {v!r}")
        limites[k] = tope

    previas = _ordenes_de_la_clave(conn, clave)
    if previas:
        return {"ordenes": [_vista(conn, o) for o in previas], "sin_proveedor": [], "omitidos": [], "repetida": True}

    filas = reposicion.sugerencia_reposicion(conn, solo_a_pedir=True, hoy=hoy, **parametros)
    omitidos: list[int] = []
    if producto_ids is not None:
        pedidos = set(producto_ids)
        omitidos = sorted(pedidos - {f["producto_id"] for f in filas})
        filas = [f for f in filas if f["producto_id"] in pedidos]

    por_proveedor: dict[int, list[dict]] = {}
    sin_proveedor: list[dict] = []
    for f in filas:
        if f["proveedor_id"] is None:
            sin_proveedor.append({"producto_id": f["producto_id"], "nombre": f["nombre"], "sugerido": f["sugerido"]})
        else:
            por_proveedor.setdefault(f["proveedor_id"], []).append(f)

    nombres = _nombres_y_costos(conn, [f["producto_id"] for lista in por_proveedor.values() for f in lista])
    sucursal_id = parametros.get("sucursal_id")
    creadas: list[dict] = []
    fecha = (hoy or datetime.date.today()).isoformat()
    for party_id, lista in por_proveedor.items():
        items = tuple(
            PurchaseOrderItem(item_id=f["producto_id"], quantity_ordered=min(Decimal(str(f["sugerido"])), limites.get(f["producto_id"], Decimal(str(f["sugerido"])))),
                              unit_cost=nombres[f["producto_id"]][1])
            for f in lista
        )
        orden = PurchaseOrder(
            id=None, number=numerador(conn), supplier_party_id=party_id, items=items, status=PurchaseOrderStatus.DRAFT,
            branch_id=sucursal_id, notes=f"Generada desde la reposición sugerida el {fecha}. {_marca(clave)}", created_by=usuario_id)
        creadas.append(_vista(conn, compras.obtener_orden(conn, _insertar(conn, orden)), nombres))
    return {"ordenes": creadas, "sin_proveedor": sin_proveedor, "omitidos": omitidos, "repetida": False}
