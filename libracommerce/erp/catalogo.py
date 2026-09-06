"""Catálogo, categorías y depósitos: la orquestación que Contalibra y Restolibra
tenían duplicada en `app/db_productos.py` (P9-M1, 2026-09-06).

Es el mismo código que corría en los dos productos desde P7/P8 —idéntico
salvo docstrings, medido—, con una sola diferencia de forma: **cada función
recibe la conexión** en vez de abrirla. La regla de la capa ERP es que el
motor no abre conexiones ni decide el commit: eso es del llamador, que en un
producto es `with get_connection() as conn:` y en `crear_venta_directa` es la
transacción de la venta entera.

Las firmas y las formas de los dicts devueltos son las históricas de los
productos (`nombre`, `codigo`, `precio_venta`, `stock_minimo`…) y no las del
dominio del motor (`name`, `default_sale_price`, `min_stock`): los routers, los
tests y el frontend de los dos productos dependen de esos nombres. El mapeo
vive acá y en ningún otro lado.

Sobre las tablas: `catalog_items`, `item_codes`, `categories`, `locations` y
`stock_movements`, todas de este motor. `estacion` viaja en
`CatalogItem.metadata` (lo usa Restolibra; Contalibra no lo manda y no lo ve).
"""

from __future__ import annotations

import json
import re
from datetime import date as _date
from datetime import datetime as _datetime
from decimal import Decimal
from typing import Any

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.domain.catalog import CatalogItem, CatalogItemType, ItemCode, ItemCodeType, Unit
from libracommerce.domain.inventory import Location
from libracommerce.usecases.inventory import StockInsuficienteError, transfer_stock

#: Las unidades que ofrece el alta de producto en los dos productos.
UNIDADES = ("u", "kg", "g", "lt", "ml", "m", "cm", "m²", "caja", "par", "docena", "pack")

_TIPO_A_ITEM_TYPE = {"producto": CatalogItemType.PRODUCT, "servicio": CatalogItemType.SERVICE}
_ITEM_TYPE_A_TIPO = {v: k for k, v in _TIPO_A_ITEM_TYPE.items()}


def _validar_tipo(tipo: str) -> CatalogItemType:
    if tipo not in _TIPO_A_ITEM_TYPE:
        raise ValueError(f"tipo inválido: {tipo!r} (debe ser 'producto' o 'servicio')")
    return _TIPO_A_ITEM_TYPE[tipo]


# ── Depósitos ────────────────────────────────────────────────────────────


def _deposito_dict(row) -> dict:
    return {
        "id": row["id"], "nombre": row["name"], "descripcion": row["description"],
        "activo": row["active"], "es_default": row["is_default"],
        "created_at": row["created_at"],
    }


def get_all_depositos(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, description, active, is_default, created_at FROM locations "
        "ORDER BY is_default DESC, name"
    ).fetchall()
    return [_deposito_dict(r) for r in rows]


def get_deposito(conn, did: int) -> dict | None:
    row = conn.execute(
        "SELECT id, name, description, active, is_default, created_at FROM locations WHERE id=?", (did,)
    ).fetchone()
    return _deposito_dict(row) if row else None


def get_default_deposito_id(conn) -> int | None:
    row = conn.execute("SELECT id FROM locations WHERE is_default=1 LIMIT 1").fetchone()
    if not row:
        row = conn.execute("SELECT id FROM locations ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def create_deposito(conn, nombre: str, descripcion: str = "") -> int:
    saved = SqliteCommerceRepository(conn).save_location(Location(None, nombre, description=descripcion))
    return saved.id


def update_deposito(conn, did: int, nombre: str, descripcion: str, activo: int):
    repo = SqliteCommerceRepository(conn)
    location = repo.get_location(did)
    if location is None:
        return
    repo.save_location(
        Location(
            id=did, name=nombre, branch_id=location.branch_id,
            location_type=location.location_type, active=bool(activo),
            description=descripcion, is_default=location.is_default,
        )
    )


def set_default_deposito(conn, did: int):
    # El índice único parcial de `locations` no admite dos defaults a la vez,
    # así que primero se limpia el anterior y recién después se marca el nuevo.
    conn.execute("UPDATE locations SET is_default=0")
    conn.execute("UPDATE locations SET is_default=1 WHERE id=?", (did,))


def delete_deposito(conn, did: int):
    tiene = conn.execute("SELECT COUNT(*) FROM stock_movements WHERE location_id=?", (did,)).fetchone()[0]
    if tiene:
        raise ValueError("No se puede eliminar un depósito con movimientos de stock.")
    es_default = conn.execute("SELECT is_default FROM locations WHERE id=?", (did,)).fetchone()
    if es_default and es_default[0]:
        raise ValueError("No se puede eliminar el depósito por defecto.")
    conn.execute("DELETE FROM locations WHERE id=?", (did,))


def get_stock_por_deposito(conn, deposito_id: int) -> list[dict]:
    rows = conn.execute("""
        SELECT ci.id, ci.name, ci.unit_code, ci.min_stock, ci.active,
               COALESCE(cat.name, '') AS categoria,
               ic.code AS codigo,
               COALESCE(SUM(sm.quantity_delta), 0) AS stock_actual
        FROM catalog_items ci
        LEFT JOIN categories cat ON cat.id = ci.category_id
        LEFT JOIN item_codes ic ON ic.item_id = ci.id AND ic.is_primary = 1
        LEFT JOIN stock_movements sm ON sm.item_id = ci.id AND sm.location_id = ?
        WHERE ci.active = 1 AND ci.item_type = 'product'
        -- Las columnas de tablas unidas tienen que estar en el GROUP BY
        -- (PostgreSQL); y la expresión se repite en el HAVING porque
        -- PostgreSQL no acepta alias de la SELECT ahí (SQLite sí).
        GROUP BY ci.id, cat.name, ic.code
        HAVING COALESCE(SUM(sm.quantity_delta), 0) != 0 OR ci.min_stock > 0
        ORDER BY ci.name
    """, (deposito_id,)).fetchall()
    return [
        {
            "id": r["id"], "codigo": r["codigo"], "nombre": r["name"],
            "unidad": r["unit_code"], "categoria": r["categoria"],
            "stock_minimo": float(r["min_stock"]), "activo": r["active"],
            "stock_actual": float(r["stock_actual"]),
        }
        for r in rows
    ]


def get_stock_producto_todos_depositos(conn, producto_id: int) -> list[dict]:
    rows = conn.execute("""
        SELECT l.id, l.name, l.is_default,
               COALESCE(SUM(sm.quantity_delta), 0) AS stock_actual
        FROM locations l
        LEFT JOIN stock_movements sm ON sm.location_id = l.id AND sm.item_id = ?
        WHERE l.active = 1
        GROUP BY l.id
        ORDER BY l.is_default DESC, l.name
    """, (producto_id,)).fetchall()
    return [
        {"id": r["id"], "nombre": r["name"], "es_default": r["is_default"],
         "stock_actual": float(r["stock_actual"])}
        for r in rows
    ]


def transferir_stock(conn, producto_id: int, origen_id: int, destino_id: int,
                     cantidad: float, usuario_id: int | None = None,
                     fecha: str = "", observaciones: str = ""):
    """Mueve stock entre depósitos, atómico y con la guarda de disponibilidad
    adentro de la misma transacción (`transfer_stock`, motor `v0.7.1`).

    El `reason_code` de cada pata (`transferencia_salida`/`transferencia_entrada`)
    es el vocabulario que el log de actividad muestra sin mapa, y **el texto del
    error** es el que el endpoint devuelve en un 422 y ve el usuario: el del
    motor nombra ids, que no le dicen nada a quien mira nombres.
    """
    _fecha = _datetime.fromisoformat(fecha or _date.today().isoformat())
    ref = observaciones or "Transferencia entre depósitos"
    try:
        transfer_stock(
            SqliteCommerceRepository(conn),
            item_id=producto_id,
            from_location_id=origen_id,
            to_location_id=destino_id,
            # `cantidad` es float en toda esta capa; vía `str` para no
            # arrastrar el binario del float al Decimal.
            quantity=Decimal(str(cantidad)),
            occurred_at=_fecha,
            note=ref,
            created_by=usuario_id,
            reason_code_salida="transferencia_salida",
            reason_code_entrada="transferencia_entrada",
        )
    except StockInsuficienteError as e:
        raise ValueError(
            f"Stock insuficiente en depósito origen (disponible: {float(e.disponible)})."
        ) from e


# ── Categorías de producto ───────────────────────────────────────────────


def get_categorias_producto(conn) -> list[dict]:
    rows = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
    return [{"id": r["id"], "nombre": r["name"]} for r in rows]


def create_categoria_producto(conn, nombre: str) -> int:
    cur = conn.execute("INSERT INTO categories (name) VALUES (?)", (nombre,))
    return cur.lastrowid


def delete_categoria_producto(conn, cid: int):
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))


def _resolver_categoria_id(conn, categoria: str) -> int | None:
    """Los productos guardan la categoría como texto libre; el motor la
    normaliza en `categories`. Se resuelve por nombre y, si no existe, se crea:
    el alta con una categoría nueva sigue funcionando como cuando era un string."""
    if not categoria:
        return None
    row = conn.execute("SELECT id FROM categories WHERE name=?", (categoria,)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO categories (name) VALUES (?)", (categoria,)).lastrowid


# ── Productos ────────────────────────────────────────────────────────────

_PRODUCTO_SELECT = """
    SELECT ci.id, ci.item_type, ci.name, ci.description, ci.active, ci.sellable,
           ci.default_sale_price, ci.default_cost, ci.unit_code, ci.min_stock,
           ci.metadata_json, ci.created_at,
           COALESCE(cat.name, '') AS categoria,
           ic.code AS codigo
    FROM catalog_items ci
    LEFT JOIN categories cat ON cat.id = ci.category_id
    LEFT JOIN item_codes ic ON ic.item_id = ci.id AND ic.is_primary = 1
"""


def _producto_dict(row) -> dict:
    metadata = json.loads(row["metadata_json"] or "{}")
    return {
        "id": row["id"],
        "codigo": row["codigo"],
        "nombre": row["name"],
        "descripcion": row["description"],
        "precio_venta": float(row["default_sale_price"]),
        "precio_costo": float(row["default_cost"]),
        "unidad": row["unit_code"],
        "categoria": row["categoria"],
        "created_at": row["created_at"],
        "stock_minimo": float(row["min_stock"]),
        "estacion": metadata.get("estacion", ""),
        "vendible": row["sellable"],
        "activo": row["active"],
        "tipo": _ITEM_TYPE_A_TIPO[CatalogItemType(row["item_type"])],
    }


def _set_codigo(repo: SqliteCommerceRepository, conn, item_id: int, codigo: str):
    """`productos.codigo` era una columna UNIQUE; acá es el `item_code` interno
    primario. Se reemplaza el anterior en vez de acumular códigos, para
    preservar la semántica de "un código por producto"."""
    conn.execute("DELETE FROM item_codes WHERE item_id=? AND is_primary=1", (item_id,))
    if codigo:
        repo.save_item_code(ItemCode(None, item_id, ItemCodeType.INTERNAL, codigo, is_primary=True))


def _catalog_item(pid: int | None, *, nombre, unidad, categoria_id, descripcion, activo, vendible,
                  estacion, precio_venta, precio_costo, stock_minimo, item_type) -> CatalogItem:
    return CatalogItem(
        id=pid, item_type=item_type, name=nombre,
        unit=Unit(code=unidad, name=unidad),
        category_id=categoria_id,
        description=descripcion, active=bool(activo), sellable=bool(vendible),
        metadata={"estacion": estacion} if estacion else {},
        default_sale_price=Decimal(str(precio_venta)),
        default_cost=Decimal(str(precio_costo)),
        min_stock=Decimal(str(stock_minimo)),
    )


def create_producto(conn, nombre: str, codigo: str = "", descripcion: str = "",
                    precio_venta: float = 0, precio_costo: float = 0,
                    unidad: str = "u", categoria: str = "",
                    stock_minimo: float = 0, estacion: str = "",
                    vendible: int = 1, tipo: str = "producto") -> int:
    item_type = _validar_tipo(tipo)
    repo = SqliteCommerceRepository(conn)
    saved = repo.save_catalog_item(_catalog_item(
        None, nombre=nombre, unidad=unidad, categoria_id=_resolver_categoria_id(conn, categoria),
        descripcion=descripcion, activo=True, vendible=vendible, estacion=estacion,
        precio_venta=precio_venta, precio_costo=precio_costo, stock_minimo=stock_minimo,
        item_type=item_type,
    ))
    _set_codigo(repo, conn, saved.id, codigo)
    return saved.id


def generar_codigo_producto(conn, categoria: str = "") -> str:
    """Un código único: prefijo según la categoría (3 primeras letras/dígitos en
    mayúscula, o 'PRD') + secuencia correlativa dentro de ese prefijo.
    Ej.: categoría 'Bebidas' -> 'BEB-0001'."""
    base = re.sub(r"[^A-Za-z0-9]", "", (categoria or ""))[:3].upper() or "PRD"
    pat = re.compile(r"^" + re.escape(base) + r"-(\d+)$")
    rows = conn.execute(
        "SELECT code FROM item_codes WHERE code_type='internal' AND code LIKE ?",
        (base + "-%",),
    ).fetchall()
    maxn = 0
    for r in rows:
        m = pat.match(r["code"] or "")
        if m:
            maxn = max(maxn, int(m.group(1)))
    return f"{base}-{maxn + 1:04d}"


def get_all_productos(conn, solo_activos: bool = False, q: str = "",
                      solo_vendibles: bool = False, tipo: str = "") -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if solo_activos:
        where.append("ci.active=1")
    if solo_vendibles:
        where.append("ci.sellable=1")
    if tipo:
        where.append("ci.item_type=?")
        params.append(_validar_tipo(tipo))
    if q:
        # Sin distinguir mayúsculas en ningún motor: con `LIKE` a secas PostgreSQL
        # sí las distingue y `yerba` no encontraba `Yerba` (hallazgo de M2).
        where.append("(LOWER(ci.name) LIKE ? OR LOWER(ic.code) LIKE ? OR LOWER(cat.name) LIKE ?)")
        params += [f"%{q.lower()}%"] * 3
    sql = _PRODUCTO_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ci.name"
    return [_producto_dict(r) for r in conn.execute(sql, params).fetchall()]


def get_producto(conn, pid: int) -> dict | None:
    row = conn.execute(_PRODUCTO_SELECT + " WHERE ci.id=?", (pid,)).fetchone()
    return _producto_dict(row) if row else None


def get_producto_by_codigo(conn, codigo: str) -> dict | None:
    row = conn.execute(_PRODUCTO_SELECT + " WHERE ic.code=? AND ci.active=1", (codigo,)).fetchone()
    return _producto_dict(row) if row else None


def update_producto(conn, pid: int, nombre: str, codigo: str, descripcion: str,
                    precio_venta: float, precio_costo: float,
                    unidad: str, categoria: str, activo: int,
                    stock_minimo: float = 0, estacion: str = "",
                    vendible: int = 1, tipo: str = "producto"):
    item_type = _validar_tipo(tipo)
    repo = SqliteCommerceRepository(conn)
    repo.save_catalog_item(_catalog_item(
        pid, nombre=nombre, unidad=unidad, categoria_id=_resolver_categoria_id(conn, categoria),
        descripcion=descripcion, activo=activo, vendible=vendible, estacion=estacion,
        precio_venta=precio_venta, precio_costo=precio_costo, stock_minimo=stock_minimo,
        item_type=item_type,
    ))
    _set_codigo(repo, conn, pid, codigo)


def delete_producto(conn, pid: int):
    conn.execute("DELETE FROM item_codes WHERE item_id=?", (pid,))
    conn.execute("DELETE FROM catalog_items WHERE id=?", (pid,))
