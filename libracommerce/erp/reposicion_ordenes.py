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

**Sin órdenes no hay nada que recordar.** La clave se estampa en las órdenes creadas: si una petición no crea ninguna (nada que pedir, o todo sin proveedor), no deja registro y
repetirla vuelve a calcular; es inofensivo porque no escribió nada. Cuando sí crea, el reintento exacto devuelve también lo que no se pidió (`sin_proveedor`, `omitidos`).

**No se duplica.** Las órdenes en borrador cuentan como «en camino» en la reposición (ADR-017), así que generar dos veces seguidas la segunda no
encuentra nada que pedir. Además `clave_operacion` (obligatoria) hace idempotente un reintento exacto: se estampa `[op:<clave>]` en las
`notes` de cada orden creada, y una clave ya usada devuelve esas mismas órdenes (`repetida: true`) sin crear otras.

**Atómica.** Todas las órdenes se crean en la transacción de quien llama (no commitea): si algo falla, no queda ninguna. La numeración la da el
`numerador` que se pase (el default del motor es `MAX(id)+1`, ver `erp.compras.numero_por_defecto`; un producto con concurrencia real pasa el suyo).
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import re
from decimal import Decimal

from ..domain.purchasing import PurchaseOrder, PurchaseOrderItem, PurchaseOrderStatus
from . import compras, lotes, reposicion

MAX_LARGO_CLAVE = 64
_CERO = Decimal("0")


class ClaveReusada(ValueError):
    """La `clave_operacion` ya generó órdenes con OTROS datos: es un pedido distinto y la clave identifica uno solo. El router la contesta 409."""


_CLAVE_VALIDA = re.compile(rf"[A-Za-z0-9._:-]{{1,{MAX_LARGO_CLAVE}}}")


def _clave(clave) -> str:
    """La clave: letras, números y `. _ : -` (un UUID entra), de 1 a 64. Sin corchetes ni espacios: el marcador `[op:<clave>]` de las `notes` tiene que
    poder encontrarse sin que una clave sea un pedazo de otra."""
    if not isinstance(clave, str) or not _CLAVE_VALIDA.fullmatch(clave.strip()):
        raise ValueError(f"clave_operacion tiene que ser un texto de 1 a {MAX_LARGO_CLAVE} caracteres (letras, números y . _ : -; un UUID por intento): {clave!r}")
    return clave.strip()


def _marca(clave: str) -> str:
    return f"[op:{clave}]"


def _huella(datos: dict) -> str:
    """Una firma corta y estable de lo que se pidió, estampada en las `notes`: la misma clave con otros datos no es el mismo pedido."""
    return hashlib.sha256(json.dumps(datos, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _marca_de_huella(huella: str) -> str:
    return f"[h:{huella}]"


def _marca_de_resultado(sin_proveedor: list[dict], omitidos: list[int]) -> str:
    """Lo que NO se pidió (productos sin proveedor y omitidos), codificado para las `notes` de la primera orden: un reintento exacto devuelve lo mismo que
    la primera respuesta, y no «nada» porque lo ya pedido hoy cuenta como «en camino»."""
    crudo = json.dumps({"s": sin_proveedor, "o": omitidos}, separators=(",", ":"), ensure_ascii=True).encode()
    return f"[res:{base64.urlsafe_b64encode(crudo).decode().rstrip('=')}]"


def _resultado_guardado(notas: str) -> tuple[list[dict], list[int]]:
    m = re.search(r"\[res:([A-Za-z0-9_-]*)\]", notas or "")
    if not m:
        return [], []
    try:
        d = json.loads(base64.urlsafe_b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)))
        return list(d.get("s", [])), [int(i) for i in d.get("o", [])]
    except (ValueError, TypeError):
        return [], []


def _ordenes_de_la_clave(conn, clave: str) -> list[tuple[dict, str]]:
    """Las órdenes que ya estamparon esta clave, con las `notes` de cada una. El marcador se compara **literal** y sensible a mayúsculas: un `LIKE`
    trata `%` y `_` como comodines (y SQLite ignora mayúsculas), y una clave distinta podría confundirse con otra; el `LIKE` sólo acota y la
    igualdad exacta la decide acá."""
    marca = _marca(clave)
    escapada = marca.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    filas = conn.execute("SELECT id, notes FROM purchase_orders WHERE notes LIKE ? ESCAPE '\\' ORDER BY id", (f"%{escapada}%",)).fetchall()
    return [(compras.obtener_orden(conn, f["id"]), f["notes"]) for f in filas if marca in (f["notes"] or "")]


def _nombres_y_costos(conn, ids: list[int]) -> dict[int, tuple[str, Decimal]]:
    if not ids:
        return {}
    filas = conn.execute(
        f"SELECT id, name, default_cost FROM catalog_items WHERE id IN ({','.join('?' for _ in ids)})", ids).fetchall()
    return {f["id"]: (f["name"], Decimal(str(f["default_cost"] if f["default_cost"] is not None else 0))) for f in filas}


def _escalas(conn, ids: list[int]) -> dict[int, int]:
    """Los decimales con que se pide cada producto: 0 si su unidad no admite fracciones, y la `decimal_scale` de la unidad (3 si no la trae) si sí.
    Es la misma regla con que la reposición redondea el `sugerido`."""
    if not ids:
        return {}
    filas = conn.execute(
        f"SELECT ci.id, u.allows_fraction, u.decimal_scale FROM catalog_items ci LEFT JOIN units u ON u.code = ci.unit_code "
        f"WHERE ci.id IN ({','.join('?' for _ in ids)})", ids).fetchall()
    return {f["id"]: ((int(f["decimal_scale"] or 0) or reposicion._ESCALA_FRACCION) if f["allows_fraction"] else 0) for f in filas}


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
    una `clave_operacion` mal formada (`ClaveReusada`, que es un `ValueError`, si la clave ya se usó con otros datos); `reposicion.SinRevision` sin la `0004`. No commitea."""
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

    huella = _huella({
        "parametros": {k: parametros[k] for k in sorted(parametros)},
        "producto_ids": sorted(producto_ids) if producto_ids is not None else None,
        "topes": {str(k): str(limites[k]) for k in sorted(limites)},
    })
    # 🔒 **Se serializa antes de mirar la clave y de calcular lo que hay que pedir.** Dos pedidos a la vez (dos pestañas, un doble clic que salió dos veces)
    # leerían lo mismo y crearían las mismas órdenes: la clave vive en las `notes` y no hay una restricción única que lo impida. Se toman las filas de los
    # productos candidatos (un `UPDATE` de sí mismas las bloquea hasta el commit, en orden ascendente; es el mismo bloqueo de la venta con FEFO y de las bajas
    # de lote) y recién después se lee todo: el segundo pedido espera al primero y encuentra sus órdenes —por la clave, o ya contadas como «en camino».
    candidatos = {f["producto_id"] for f in reposicion.sugerencia_reposicion(conn, solo_a_pedir=True, hoy=hoy, **parametros)}
    lotes.tomar_productos(conn, candidatos | set(producto_ids or []))

    previas = _ordenes_de_la_clave(conn, clave)
    if previas:
        if any(_marca_de_huella(huella) not in notas for _, notas in previas):
            raise ClaveReusada("la clave_operacion ya se usó con otros datos: es otro pedido y necesita otra clave")
        sin_prov, omit = [], []
        for _, notas in previas:                                   # lo que no se pidió viaja en las notas de la primera orden
            guardado = _resultado_guardado(notas)
            if guardado != ([], []):
                sin_prov, omit = guardado
                break
        return {"ordenes": [_vista(conn, o) for o, _ in previas], "sin_proveedor": sin_prov, "omitidos": omit, "repetida": True}

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

    ids_a_pedir = [f["producto_id"] for lista in por_proveedor.values() for f in lista]
    nombres = _nombres_y_costos(conn, ids_a_pedir)
    escalas = _escalas(conn, ids_a_pedir)
    sucursal_id = parametros.get("sucursal_id")
    creadas: list[dict] = []
    fecha = (hoy or datetime.date.today()).isoformat()
    for party_id, lista in por_proveedor.items():
        items = []
        for f in lista:
            sugerido = Decimal(str(f["sugerido"]))
            # El tope de lo confirmado, redondeado HACIA ABAJO a la unidad del producto: una cantidad de 0,5 de algo que se pide entero no existe.
            cantidad = reposicion._piso(min(sugerido, limites.get(f["producto_id"], sugerido)), escalas.get(f["producto_id"], 0))
            if cantidad <= 0:
                omitidos.append(f["producto_id"])
                continue
            items.append(PurchaseOrderItem(item_id=f["producto_id"], quantity_ordered=cantidad, unit_cost=nombres[f["producto_id"]][1]))
        if not items:
            continue
        orden = PurchaseOrder(
            id=None, number=numerador(conn), supplier_party_id=party_id, items=tuple(items), status=PurchaseOrderStatus.DRAFT,
            branch_id=sucursal_id, created_by=usuario_id,
            notes=f"Generada desde la reposición sugerida el {fecha}. {_marca(clave)} {_marca_de_huella(huella)}")
        creadas.append(_vista(conn, compras.obtener_orden(conn, _insertar(conn, orden)), nombres))
    omitidos = sorted(set(omitidos))
    if creadas and (sin_proveedor or omitidos):
        conn.execute("UPDATE purchase_orders SET notes = notes || ? WHERE id = ?", (" " + _marca_de_resultado(sin_proveedor, omitidos), creadas[0]["id"]))
    return {"ordenes": creadas, "sin_proveedor": sin_proveedor, "omitidos": omitidos, "repetida": False}
