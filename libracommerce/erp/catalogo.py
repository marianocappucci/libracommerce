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
from dataclasses import asdict, replace
from datetime import date as _date
from datetime import datetime as _datetime
from decimal import Decimal
from typing import Any

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.domain.catalog import (
    CatalogItem,
    CatalogItemType,
    ItemCode,
    ItemCodeType,
    ItemVariant,
    Unit,
)
from libracommerce.domain.inventory import Location
from libracommerce.domain.scale import ScaleFormat, ScaleValueKind, parse_scale_barcode
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
           ic.code AS codigo,
           u.allows_fraction
    FROM catalog_items ci
    LEFT JOIN categories cat ON cat.id = ci.category_id
    LEFT JOIN item_codes ic ON ic.item_id = ci.id AND ic.is_primary = 1
    LEFT JOIN units u ON u.code = ci.unit_code
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
        # F1 de VentaLibra: si la unidad admite fracciones (kg, lt...), para la
        # balanza (`escanear`). Es del CÓDIGO de unidad, no del producto -- lo
        # comparten todos los productos con la misma `unidad`, como el
        # registro de `units` que ya usa VentaLibra por su cuenta
        # (`app/services/catalog.py::CatalogService.create_unit`).
        "permite_fraccion": bool(row["allows_fraction"]) if row["allows_fraction"] is not None else False,
    }


def agregar_codigo_balanza(conn, pid: int, codigo: str) -> None:
    """Código con el que el producto está cargado en la balanza de mostrador
    (`item_codes.code_type='scale'`) -- no es el EAN que imprime la etiqueta
    (ese trae el peso adentro, ver `domain/scale.py`), es el código corto que
    el comercio eligió al cargarlo en el equipo. Lo consume `escanear`."""
    SqliteCommerceRepository(conn).save_item_code(
        ItemCode(id=None, item_id=pid, code_type=ItemCodeType.SCALE, code=codigo)
    )


def _set_codigo(repo: SqliteCommerceRepository, conn, item_id: int, codigo: str):
    """`productos.codigo` era una columna UNIQUE; acá es el `item_code` interno
    primario. Se reemplaza el anterior en vez de acumular códigos, para
    preservar la semántica de "un código por producto"."""
    conn.execute("DELETE FROM item_codes WHERE item_id=? AND is_primary=1", (item_id,))
    if codigo:
        repo.save_item_code(ItemCode(None, item_id, ItemCodeType.INTERNAL, codigo, is_primary=True))


def _permite_fraccion_actual(conn, unidad: str) -> bool:
    """El `allows_fraction` que YA tiene `units` para ese código, o `False` si
    el código todavía no existe (lo de siempre: una unidad nueva nace sin
    fracción)."""
    row = conn.execute("SELECT allows_fraction FROM units WHERE code=?", (unidad,)).fetchone()
    return bool(row["allows_fraction"]) if row is not None else False


def _resolver_permite_fraccion(conn, unidad: str, permite_fraccion: bool | None) -> bool:
    """`None` (el default, y lo que manda cualquier caller que no lo declare)
    CONSERVA el valor que ya tiene la unidad -- no lo resetea. `_upsert_unit`
    (repository.py) reescribe `units.allows_fraction` en cada
    `save_catalog_item`, así que sin esto un PUT de un producto en "kg" sin
    este campo apagaría la balanza por peso de TODOS los productos en "kg"."""
    if permite_fraccion is not None:
        return permite_fraccion
    return _permite_fraccion_actual(conn, unidad)


def _catalog_item(pid: int | None, *, nombre, unidad, categoria_id, descripcion, activo, vendible,
                  estacion, precio_venta, precio_costo, stock_minimo, item_type,
                  permite_fraccion: bool = False) -> CatalogItem:
    return CatalogItem(
        id=pid, item_type=item_type, name=nombre,
        unit=Unit(code=unidad, name=unidad, allows_fraction=permite_fraccion),
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
                    vendible: int = 1, tipo: str = "producto",
                    permite_fraccion: bool | None = None) -> int:
    item_type = _validar_tipo(tipo)
    repo = SqliteCommerceRepository(conn)
    saved = repo.save_catalog_item(_catalog_item(
        None, nombre=nombre, unidad=unidad, categoria_id=_resolver_categoria_id(conn, categoria),
        descripcion=descripcion, activo=True, vendible=vendible, estacion=estacion,
        precio_venta=precio_venta, precio_costo=precio_costo, stock_minimo=stock_minimo,
        item_type=item_type, permite_fraccion=_resolver_permite_fraccion(conn, unidad, permite_fraccion),
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
                    vendible: int = 1, tipo: str = "producto",
                    permite_fraccion: bool | None = None):
    item_type = _validar_tipo(tipo)
    repo = SqliteCommerceRepository(conn)
    repo.save_catalog_item(_catalog_item(
        pid, nombre=nombre, unidad=unidad, categoria_id=_resolver_categoria_id(conn, categoria),
        descripcion=descripcion, activo=activo, vendible=vendible, estacion=estacion,
        precio_venta=precio_venta, precio_costo=precio_costo, stock_minimo=stock_minimo,
        item_type=item_type, permite_fraccion=_resolver_permite_fraccion(conn, unidad, permite_fraccion),
    ))
    _set_codigo(repo, conn, pid, codigo)


def delete_producto(conn, pid: int):
    conn.execute("DELETE FROM item_codes WHERE item_id=?", (pid,))
    conn.execute("DELETE FROM catalog_items WHERE id=?", (pid,))


# ── Variantes (F1 de VentaLibra a LibraCommerce, 2026-09-14) ─────────────
#
# Talle/color, presentaciones -- lo que ya usa VentaLibra por su cuenta
# (`ventalibra/app/services/catalog.py::CatalogService.add_variant/list_variants/
# get_variant`) sobre `item_variants`, que el dominio y el schema del motor ya
# tienen. Aditivo: Contalibra y Restolibra no llaman nada de esta sección y
# `get_all_productos`/`_producto_dict` no cambian de forma.


def _variante_from_row(row) -> ItemVariant:
    return ItemVariant(
        id=row["id"], item_id=row["item_id"], sku=row["sku"], name=row["name"],
        attributes=json.loads(row["attributes_json"] or "{}"), active=bool(row["active"]),
    )


def _variante_dict(v: ItemVariant) -> dict:
    return {
        "id": v.id, "producto_id": v.item_id, "sku": v.sku, "nombre": v.name,
        "atributos": v.attributes, "activa": v.active,
    }


def _variante_por_sku(conn, sku: str) -> ItemVariant | None:
    """Sólo variantes activas: es lo que puede venderse, y lo que usa `escanear`."""
    row = conn.execute(
        "SELECT id, item_id, sku, name, attributes_json, active FROM item_variants "
        "WHERE sku=? AND active=1",
        (sku,),
    ).fetchone()
    return _variante_from_row(row) if row else None


def get_variantes_producto(conn, pid: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, item_id, sku, name, attributes_json, active FROM item_variants "
        "WHERE item_id=? ORDER BY id",
        (pid,),
    ).fetchall()
    return [_variante_dict(_variante_from_row(r)) for r in rows]


def get_variante(conn, vid: int) -> dict | None:
    row = conn.execute(
        "SELECT id, item_id, sku, name, attributes_json, active FROM item_variants WHERE id=?",
        (vid,),
    ).fetchone()
    return _variante_dict(_variante_from_row(row)) if row else None


def create_variante(conn, pid: int, sku: str, nombre: str, atributos: dict[str, str] | None = None) -> dict:
    saved = SqliteCommerceRepository(conn).save_item_variant(
        ItemVariant(id=None, item_id=pid, sku=sku, name=nombre, attributes=atributos or {})
    )
    return _variante_dict(saved)


def update_variante(conn, vid: int, sku: str, nombre: str,
                    atributos: dict[str, str] | None, activa: bool) -> dict | None:
    repo = SqliteCommerceRepository(conn)
    existente = repo.get_item_variant(vid)
    if existente is None:
        return None
    saved = repo.save_item_variant(
        replace(existente, sku=sku, name=nombre, attributes=atributos or {}, active=activa)
    )
    return _variante_dict(saved)


# ── Balanza de mostrador y escaneo (F1 de VentaLibra a LibraCommerce) ────
#
# Replica `ventalibra/app/services/scale.py::ScaleService`: el parser vive en
# `domain/scale.py` (lógica comercial, no de un producto puntual); acá está lo
# que lo rodea, sobre las tablas de este motor (`commerce_settings`,
# `item_codes` con `code_type='scale'`). Aditivo: nada de esto lo llaman
# Contalibra ni Restolibra.

#: Clave en `commerce_settings`. Sin ella, todo se lee como código común.
_SETTING_FORMATO_BALANZA = "scale.format"


class EtiquetaBalanzaError(ValueError):
    """La etiqueta de balanza se leyó bien, pero apunta a algo que no se puede
    vender así (no está cargada en el catálogo, o el producto no admite
    fracciones). El router la traduce a 422: el código está bien leído, lo que
    no se puede es cobrar lo que dice."""


def get_formato_balanza(conn) -> ScaleFormat | None:
    crudo = SqliteCommerceRepository(conn).get_setting(_SETTING_FORMATO_BALANZA)
    if not crudo:
        return None
    datos = json.loads(crudo)
    datos["value_kind"] = ScaleValueKind(datos["value_kind"])
    return ScaleFormat(**datos)


def set_formato_balanza(conn, fmt: ScaleFormat | None) -> None:
    """`fmt=None` apaga la balanza: los códigos vuelven a leerse todos como
    comunes."""
    if fmt is None:
        conn.execute("DELETE FROM commerce_settings WHERE key=?", (_SETTING_FORMATO_BALANZA,))
        return
    datos = asdict(fmt)
    datos["value_kind"] = fmt.value_kind.value
    SqliteCommerceRepository(conn).set_setting(_SETTING_FORMATO_BALANZA, json.dumps(datos))


def escanear(conn, code: str) -> dict | None:
    """Resuelve un código escaneado: de balanza (trae la cantidad pesada o el
    importe ya calculado) o común (código de barras del producto, o el SKU de
    una variante). `None` si ningún producto ni variante corresponde al
    código -- el router lo traduce a 404. `EtiquetaBalanzaError` si la
    etiqueta SÍ es de balanza pero apunta a algo que no se puede vender así
    (404 sería engañoso: el código se leyó bien).

    La forma del resultado: `{"producto", "cantidad", "precio_unitario",
    "de_balanza"}`, más `"variante"` cuando el código matcheó el SKU de una
    variante en vez del código de barras del producto. `precio_unitario` sólo
    viene con valor cuando la balanza ya trae el importe calculado: en ese
    caso se cobra ÉSE y no el de la lista de precios, porque es el que está
    impreso en la etiqueta pegada al producto.
    """
    fmt = get_formato_balanza(conn)
    leido = parse_scale_barcode(code, fmt) if fmt is not None else None
    repo = SqliteCommerceRepository(conn)

    if leido is None:
        item = repo.find_item_by_code(code)
        variante = None
        if item is None:
            variante = _variante_por_sku(conn, code)
            if variante is None:
                return None
            item_id = variante.item_id
        else:
            item_id = item.id
        producto = get_producto(conn, item_id)
        if producto is None:
            return None
        resultado = {"producto": producto, "cantidad": 1.0, "precio_unitario": None, "de_balanza": False}
        if variante is not None:
            resultado["variante"] = _variante_dict(variante)
        return resultado

    item = repo.find_item_by_code(leido.item_code, code_type=ItemCodeType.SCALE)
    if item is None:
        raise EtiquetaBalanzaError(
            f"la etiqueta es del producto {leido.item_code} de la balanza, "
            "que no está cargado en el catálogo"
        )
    producto = get_producto(conn, item.id)
    if leido.kind is ScaleValueKind.WEIGHT:
        if not item.unit.allows_fraction:
            # Vender "0,750" de algo que se cuenta por unidad es un error de
            # carga (el código de balanza quedó en el producto equivocado);
            # cobrarlo igual sería peor que frenar acá.
            raise EtiquetaBalanzaError(
                f"{item.name} se vende por {item.unit.name.lower()} y no admite "
                "fracciones, así que no puede venir de la balanza por peso"
            )
        return {
            "producto": producto, "cantidad": float(leido.value),
            "precio_unitario": None, "de_balanza": True,
        }
    return {
        "producto": producto, "cantidad": 1.0,
        "precio_unitario": float(leido.value), "de_balanza": True,
    }
