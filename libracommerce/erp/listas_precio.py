"""Listas de precio sobre `price_lists`/`item_prices`: la orquestación que Contalibra
y Restolibra tenían duplicada en `app/db_listas_precio.py` (P9-M2, 2026-09-06).

El modelo de los dos productos es "flat" —un precio por producto por lista, sin
vigencia ni sucursal— así que cada fila base que este módulo toca tiene
`branch_id IS NULL AND min_quantity IS NULL`, y `valid_from` lleva el sentinel
`_SIN_VIGENCIA` (los productos nunca tuvieron ese dato). Encima del flat, los
**quiebres por cantidad** (filas con `min_quantity`) que hoy sólo usa el add-on
mayorista de Contalibra: el motor los guarda, los lee y los resuelve para los
dos; el gateo por add-on es de la capa de API del producto.

Cada función recibe la conexión; las formas de los dicts son las históricas.
"""

from __future__ import annotations

from decimal import Decimal

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.domain.catalog import PriceList

#: "Sin restricción de fecha de inicio", para todo lo que este módulo escribe.
_SIN_VIGENCIA = "2000-01-01T00:00:00"


def _lista_dict(row) -> dict:
    return {
        "id": row["id"], "nombre": row["name"], "descripcion": row["description"],
        "activa": row["active"], "es_default": row["is_default"],
        "created_at": row["created_at"],
    }


def get_all_listas_precio(conn, solo_activas: bool = False) -> list[dict]:
    where = "WHERE active=1" if solo_activas else ""
    rows = conn.execute(
        f"SELECT id, name, description, active, is_default, created_at FROM price_lists {where} "
        "ORDER BY is_default DESC, name"
    ).fetchall()
    return [_lista_dict(r) for r in rows]


def get_lista_precio(conn, lista_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, name, description, active, is_default, created_at FROM price_lists WHERE id=?",
        (lista_id,),
    ).fetchone()
    return _lista_dict(row) if row else None


def create_lista_precio(conn, nombre: str, descripcion: str = "") -> int:
    saved = SqliteCommerceRepository(conn).save_price_list(PriceList(None, nombre, description=descripcion))
    return saved.id


def update_lista_precio(conn, lista_id: int, nombre: str, descripcion: str, activa: int):
    repo = SqliteCommerceRepository(conn)
    lista = repo.get_price_list(lista_id)
    if lista is None:
        return
    repo.save_price_list(
        PriceList(id=lista_id, name=nombre, description=descripcion, active=bool(activa), is_default=lista.is_default)
    )


def delete_lista_precio(conn, lista_id: int):
    # `item_prices` no tiene ON DELETE CASCADE hacia `price_lists`: se borra a
    # mano, que es lo que hacía la tabla vieja (eliminar la lista eliminaba sus precios).
    conn.execute("DELETE FROM item_prices WHERE price_list_id=?", (lista_id,))
    conn.execute("DELETE FROM price_lists WHERE id=?", (lista_id,))


def get_lista_precio_items(conn, lista_id: int, categoria: str = "") -> list[dict]:
    where = "AND cat.name=?" if categoria else ""
    params: list = [lista_id]
    if categoria:
        params.append(categoria)
    rows = conn.execute(f"""
        SELECT ci.id, ic.code AS codigo, ci.name AS nombre, ci.unit_code AS unidad,
               COALESCE(cat.name, '') AS categoria,
               ci.default_sale_price AS precio_venta, ci.default_cost AS precio_costo,
               COALESCE(ip.amount, 0) AS precio_lista,
               CASE WHEN ip.item_id IS NOT NULL THEN 1 ELSE 0 END AS en_lista
        FROM catalog_items ci
        LEFT JOIN categories cat ON cat.id = ci.category_id
        LEFT JOIN item_codes ic ON ic.item_id = ci.id AND ic.is_primary = 1
        LEFT JOIN item_prices ip
               ON ip.price_list_id=? AND ip.item_id=ci.id
               AND ip.branch_id IS NULL AND ip.min_quantity IS NULL
        WHERE ci.active=1 {where}
        ORDER BY categoria, ci.name
    """, params).fetchall()
    return [dict(r) for r in rows]


def get_precio_en_lista(conn, lista_id: int, producto_id: int) -> float | None:
    """El precio base del producto en la lista, o None si no está definido."""
    row = conn.execute(
        "SELECT amount FROM item_prices WHERE price_list_id=? AND item_id=? "
        "AND branch_id IS NULL AND min_quantity IS NULL",
        (lista_id, producto_id),
    ).fetchone()
    return float(row["amount"]) if row else None


def get_precios_lista_dict(conn, lista_id: int) -> dict[int, float]:
    """{producto_id: precio} de toda la lista (lo consume el autocompletado)."""
    rows = conn.execute(
        "SELECT item_id, amount FROM item_prices WHERE price_list_id=? "
        "AND branch_id IS NULL AND min_quantity IS NULL",
        (lista_id,),
    ).fetchall()
    return {r["item_id"]: r["amount"] for r in rows}


# ── Quiebres por cantidad ────────────────────────────────────────────────


def get_quiebres(conn, lista_id: int, producto_id: int) -> list[dict]:
    """`[{min_quantity, amount}]` ordenado por cantidad, sin la fila base."""
    rows = conn.execute(
        "SELECT min_quantity, amount FROM item_prices "
        "WHERE price_list_id=? AND item_id=? "
        "AND branch_id IS NULL AND min_quantity IS NOT NULL "
        "ORDER BY min_quantity",
        (lista_id, producto_id),
    ).fetchall()
    return [{"min_quantity": float(r["min_quantity"]), "amount": float(r["amount"])} for r in rows]


def set_quiebres(conn, lista_id: int, producto_id: int, quiebres: list[dict]) -> None:
    """Reemplaza los quiebres del producto en la lista; la fila base no se toca."""
    conn.execute(
        "DELETE FROM item_prices WHERE price_list_id=? AND item_id=? "
        "AND branch_id IS NULL AND min_quantity IS NOT NULL",
        (lista_id, producto_id),
    )
    for q in quiebres:
        conn.execute(
            "INSERT INTO item_prices (item_id, price_list_id, amount, valid_from, min_quantity) "
            "VALUES (?,?,?,?,?)",
            (producto_id, lista_id, float(q["amount"]), _SIN_VIGENCIA, float(q["min_quantity"])),
        )


def resolver_precio_por_cantidad(conn, lista_id: int, producto_id: int, cantidad: float) -> float | None:
    """El precio efectivo para esa cantidad: entre base y quiebres aplicables, el
    quiebre más alto (`resolve_price` del motor). None si no hay precio."""
    precio = SqliteCommerceRepository(conn).resolve_price(
        producto_id, price_list_id=lista_id, quantity=Decimal(str(cantidad)),
    )
    return float(precio) if precio is not None else None


# ── Escritura del flat ───────────────────────────────────────────────────


def _upsert_precio(conn, lista_id: int, producto_id: int, precio: float) -> None:
    """`item_prices` admite varias filas por (lista, producto) —vigencias,
    quiebres, sucursal—; el flat escribe sólo la "sin restricciones"."""
    cur = conn.execute(
        "UPDATE item_prices SET amount=? WHERE price_list_id=? AND item_id=? "
        "AND branch_id IS NULL AND min_quantity IS NULL",
        (precio, lista_id, producto_id),
    )
    if cur.rowcount == 0:
        conn.execute(
            "INSERT INTO item_prices (item_id, price_list_id, amount, valid_from) VALUES (?,?,?,?)",
            (producto_id, lista_id, precio, _SIN_VIGENCIA),
        )


def save_lista_precio_items(conn, lista_id: int, precios: dict):
    """Guarda o actualiza `{producto_id: precio}`. Precio <= 0 saca el ítem de la lista."""
    for pid_s, precio_s in precios.items():
        pid = int(pid_s)
        precio = float(precio_s)
        if precio <= 0:
            conn.execute(
                "DELETE FROM item_prices WHERE price_list_id=? AND item_id=? "
                "AND branch_id IS NULL AND min_quantity IS NULL",
                (lista_id, pid),
            )
        else:
            _upsert_precio(conn, lista_id, pid, precio)


def apply_porcentaje_lista(conn, lista_id: int, porcentaje: float,
                           base: str = "lista", categoria: str = "") -> int:
    """Ajuste porcentual. base: 'lista' (sobre el precio actual), 'venta' o
    'costo' (sobre el precio del producto). Devuelve cuántos se actualizaron."""
    factor = 1 + porcentaje / 100
    cat_where = "AND cat.name=?" if categoria else ""
    cat_param = [categoria] if categoria else []
    if base == "lista":
        rows = conn.execute(f"""
            SELECT ip.item_id, ip.amount
            FROM item_prices ip
            JOIN catalog_items ci ON ci.id = ip.item_id
            LEFT JOIN categories cat ON cat.id = ci.category_id
            WHERE ip.price_list_id=? AND ci.active=1
              AND ip.branch_id IS NULL AND ip.min_quantity IS NULL {cat_where}
        """, [lista_id] + cat_param).fetchall()
        for r in rows:
            nuevo = round(r["amount"] * factor, 2)
            conn.execute(
                "UPDATE item_prices SET amount=? WHERE price_list_id=? AND item_id=? "
                "AND branch_id IS NULL AND min_quantity IS NULL",
                (nuevo, lista_id, r["item_id"]),
            )
        return len(rows)
    col = "default_sale_price" if base == "venta" else "default_cost"
    rows = conn.execute(f"""
        SELECT ci.id, ci.{col} AS base_precio
        FROM catalog_items ci
        LEFT JOIN categories cat ON cat.id = ci.category_id
        WHERE ci.active=1 {cat_where}
    """, cat_param).fetchall()
    for r in rows:
        nuevo = round(r["base_precio"] * factor, 2)
        _upsert_precio(conn, lista_id, r["id"], nuevo)
    return len(rows)


def importar_precios_lista(conn, lista_id: int, fuente: str, fuente_lista_id: int | None = None):
    """Importa precios desde 'venta', 'costo' o 'lista' (con `fuente_lista_id`)."""
    if fuente == "lista" and fuente_lista_id:
        rows = conn.execute(
            "SELECT item_id, amount FROM item_prices WHERE price_list_id=? "
            "AND branch_id IS NULL AND min_quantity IS NULL",
            (fuente_lista_id,),
        ).fetchall()
        for r in rows:
            _upsert_precio(conn, lista_id, r["item_id"], r["amount"])
        return
    col = "default_sale_price" if fuente == "venta" else "default_cost"
    rows = conn.execute(f"SELECT id, {col} AS precio FROM catalog_items WHERE active=1").fetchall()
    for r in rows:
        _upsert_precio(conn, lista_id, r["id"], r["precio"])


# ── Autocompletado del punto de venta ────────────────────────────────────


def buscar_productos(conn, q: str = "", lista_id: int = 0, tipo: str = "",
                     solo_vendibles: bool = False, tope: int = 20) -> list[dict]:
    """Lo que devuelve `/productos/buscar` a Ventas, Facturas, Presupuestos y
    Remitos: hasta `tope` productos activos, con `precio_venta` tomado de la
    lista si se pidió una y el producto está en ella, y `precio_base` siempre.
    `tipo` ('producto'|'servicio') restringe; `solo_vendibles` saca los insumos."""
    from .catalogo import get_all_productos

    resultados = get_all_productos(conn, solo_activos=True, q=q, solo_vendibles=solo_vendibles, tipo=tipo)[:tope]
    precios_lista = get_precios_lista_dict(conn, lista_id) if lista_id else {}
    return [{
        "id": p["id"],
        "codigo": p["codigo"] or "",
        "nombre": p["nombre"],
        "precio_venta": precios_lista.get(p["id"], p["precio_venta"]),
        "precio_base": p["precio_venta"],
        "unidad": p["unidad"],
    } for p in resultados]
