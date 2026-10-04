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
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date as _date
from datetime import datetime as _datetime
from decimal import Decimal
from typing import Any

from libracommerce.db.repository import repositorio_de
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
from libracommerce.usecases.inventory import StockInsuficienteError, TramoDeTransferencia, transfer_stock

#: Las unidades que ofrece el alta de producto en los dos productos.
UNIDADES = ("u", "kg", "g", "lt", "ml", "m", "cm", "m²", "caja", "par", "docena", "pack")

_TIPO_A_ITEM_TYPE = {"producto": CatalogItemType.PRODUCT, "servicio": CatalogItemType.SERVICE}
_ITEM_TYPE_A_TIPO = {v: k for k, v in _TIPO_A_ITEM_TYPE.items()}


def _validar_tipo(tipo: str) -> CatalogItemType:
    if tipo not in _TIPO_A_ITEM_TYPE:
        raise ValueError(f"tipo inválido: {tipo!r} (debe ser 'producto' o 'servicio')")
    return _TIPO_A_ITEM_TYPE[tipo]


# ── El código repetido: un error de dominio, no el texto de la base ──────


class CodigoRepetido(ValueError):
    """Ya hay un `item_code` con ese `(code_type, code)`: el índice único de `item_codes`. El mensaje es para la persona
    (castellano, con el código); la excepción de la base queda de `__cause__`. Es un `ValueError` como el resto de los
    rechazos del motor, así que lo que ya atrapa `ValueError` (o `Exception`, como el router) sigue andando."""

    def __init__(self, codigo: str):
        self.codigo = codigo
        super().__init__(f"Ya existe un producto con el código «{codigo}».")


#: El `UNIQUE(code_type, code)` de `item_codes`, en cada motor. PostgreSQL le pone ese nombre (se lee de
#: `diag.constraint_name`); SQLite no nombra la restricción y la identifica por sus columnas.
_UNICO_DE_CODIGOS_PG = "item_codes_code_type_code_key"
_UNICO_DE_CODIGOS_SQLITE = frozenset({"item_codes.code_type", "item_codes.code"})
_PREFIJO_UNICO_SQLITE = "UNIQUE constraint failed:"


def _es_codigo_repetido(exc: BaseException) -> bool:
    """¿`exc` es la violación del índice único `(code_type, code)` de `item_codes`, y no otro error de integridad?

    El único lugar donde se decide. Recorre la cadena de `__cause__` (no la de `__context__`: un error ajeno que se lanzó mientras se atendía éste no es éste) porque contra PostgreSQL
    `libracore` convierte el error de psycopg en un `sqlite3.IntegrityError` (para que los `except` de los productos
    anden en los dos motores) y deja el original de `__cause__`.

    - **PostgreSQL**: `psycopg.errors.UniqueViolation` cuyo `diag.constraint_name` es el único de los códigos. Otra
      unicidad de la misma tabla (`idx_item_codes_one_primary_per_item`, un segundo principal), una FK o un check no.
    - **SQLite**: `sqlite3.IntegrityError` con `sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"`. SQLite no da el nombre
      de la restricción, sólo las columnas en el texto del error (`UNIQUE constraint failed: item_codes.code_type,
      item_codes.code`): lo único que se lee del mensaje es ese conjunto de columnas, y sólo cuando la clase del error ya
      es la de una unicidad de SQLite. Un segundo principal falla con `item_codes.item_id`, y no coincide.
    """
    try:
        from psycopg.errors import UniqueViolation
    except ImportError:   # sin psycopg (sólo SQLite) no hay nada de PostgreSQL que reconocer
        UniqueViolation = None  # noqa: N806
    visto: set[int] = set()
    actual: BaseException | None = exc
    while actual is not None and id(actual) not in visto:
        visto.add(id(actual))
        if UniqueViolation is not None and isinstance(actual, UniqueViolation):
            return getattr(getattr(actual, "diag", None), "constraint_name", None) == _UNICO_DE_CODIGOS_PG
        if isinstance(actual, sqlite3.IntegrityError) and getattr(actual, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_UNIQUE":
            mensaje = str(actual)
            if mensaje.startswith(_PREFIJO_UNICO_SQLITE):
                columnas = {c.strip() for c in mensaje[len(_PREFIJO_UNICO_SQLITE):].split(",")}
                return columnas == _UNICO_DE_CODIGOS_SQLITE
            return False
        actual = actual.__cause__
    return False


@contextmanager
def _codigo_repetido_como_error_de_dominio(codigo: str):
    """Todo camino que escribe un `item_code` pasa por acá: la violación del único de códigos sale como `CodigoRepetido`
    (con el código pedido) y **cualquier otra excepción sigue su camino sin tocarse** (una FK, un check, un segundo
    principal, una base caída). Se usa por FUERA del `repo.transaction()`: el rollback ya se hizo cuando se traduce."""
    try:
        yield
    except Exception as e:
        if _es_codigo_repetido(e):
            raise CodigoRepetido(codigo) from e
        raise


# ── Depósitos ────────────────────────────────────────────────────────────


class DepositoInexistente(ValueError):
    """Un `deposito_id` (venta, devolución o transferencia) no existe o no
    está activo en `locations`.

    Se valida con `validar_deposito` —una sola función, ANTES de escribir
    nada, en `erp.ventas.registrar_venta`/`devolver_items` y en
    `transferir_stock` (origen Y destino)—: un depósito inventado no es un
    conflicto con otra operación simultánea, es un dato del pedido que nunca
    iba a dejar de fallar. Hereda de `ValueError` para que el 422 de
    `web/ventas_router.py` y `web/catalogo_router.py` sea el mismo mecanismo
    con el que ya rebotan `delete_deposito`/`update_deposito`.

    🔴 **`add_movimiento_stock` (y por lo tanto `descontar_stock_venta`,
    `transfer_stock`) NO valida esto por su cuenta** —confiar en la FK de
    `stock_movements.location_id` dejaría pasar escrituras previas (la pata de
    salida de una transferencia, por ejemplo) antes de reventar, y ese error
    de FK no lo atrapa ningún `except ValueError` (sale como 500). Por eso la
    validación va temprano, en cada caller que recibe un `deposito_id` de
    afuera."""


def _deposito_dict(row) -> dict:
    return {
        "id": row["id"], "nombre": row["name"], "descripcion": row["description"],
        "activo": row["active"], "es_default": row["is_default"],
        "created_at": row["created_at"],
        # `locations.location_type`: el motor no lo interpreta; lo usa un producto
        # con sucursales y depósitos (VentaLibra: `store`/`warehouse`).
        "tipo": row["location_type"],
        # `locations.branch_id`: la sucursal del depósito (`None` en un producto sin sucursales).
        "branch_id": row["branch_id"],
    }


def get_all_depositos(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, description, active, is_default, created_at, location_type, branch_id FROM locations "
        "ORDER BY is_default DESC, name"
    ).fetchall()
    return [_deposito_dict(r) for r in rows]


def get_deposito(conn, did: int) -> dict | None:
    row = conn.execute(
        "SELECT id, name, description, active, is_default, created_at, location_type, branch_id FROM locations "
        "WHERE id=?",
        (did,),
    ).fetchone()
    return _deposito_dict(row) if row else None


def validar_deposito(conn, deposito_id: int | None) -> None:
    """Levanta `DepositoInexistente` si `deposito_id` no es `None` y no
    resuelve a un depósito existente y activo. `None` no se valida — es "no
    se especificó", el comportamiento de siempre.

    Compartida por `erp.ventas.registrar_venta`/`devolver_items` y por
    `transferir_stock` de este módulo: un solo criterio, un solo lugar donde
    cambiarlo."""
    if deposito_id is None:
        return
    deposito = get_deposito(conn, deposito_id)
    if deposito is None or not deposito["activo"]:
        raise DepositoInexistente(
            f"El depósito {deposito_id} no existe o no está activo."
        )


def get_default_deposito_id(conn) -> int | None:
    row = conn.execute("SELECT id FROM locations WHERE is_default=1 LIMIT 1").fetchone()
    if not row:
        row = conn.execute("SELECT id FROM locations ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def create_deposito(
    conn, nombre: str, descripcion: str = "", tipo: str | None = None, branch_id: int | None = None
) -> int:
    """`tipo` es el `location_type` (sin él, el default del dominio, como siempre). El motor no valida el
    vocabulario: es del producto (`OpcionesDepositos.validar_alta`).

    `branch_id`, si se pasa, tiene que resolver a una sucursal existente y activa (`validar_sucursal`) —
    un producto sin sucursales (Contalibra) nunca lo manda y sigue en `None`, como siempre. Si esa sucursal
    todavía no tenía depósito predeterminado, este pasa a serlo."""
    validar_sucursal(conn, branch_id)
    location = Location(None, nombre, branch_id=branch_id, description=descripcion)
    if tipo:
        location = replace(location, location_type=tipo)
    saved = repositorio_de(conn).save_location(location)
    if branch_id is not None:
        _asegurar_deposito_predeterminado(conn, branch_id)
    return saved.id


def update_deposito(conn, did: int, nombre: str, descripcion: str, activo: int):
    """🔴 **No se puede desactivar el depósito por defecto** — levanta
    `ValueError` (mismo mecanismo que `delete_deposito`, que ya rechaza
    borrarlo). Sin esta guarda, `stock.py::add_movimiento_stock` seguía
    resolviendo el default vía `get_default_deposito_id` —que no mira
    `active`— y toda venta sin `deposito_id` explícito le seguía cargando
    stock a un depósito que la pantalla mostraba como dado de baja.

    🔴 **Tampoco el último depósito activo de una sucursal activa** (`_verificar_no_es_el_ultimo_deposito`):
    toda sucursal declara como mínimo un depósito donde vive su stock. Si el que se desactiva era el
    predeterminado de su sucursal, el predeterminado pasa a otro activo."""
    repo = repositorio_de(conn)
    location = repo.get_location(did)
    if location is None:
        return
    if location.is_default and not activo:
        raise ValueError(
            "No se puede desactivar el depósito por defecto: primero hay que "
            "marcar otro depósito como default."
        )
    se_desactiva = location.active and not activo
    if se_desactiva:
        _verificar_no_es_el_ultimo_deposito(conn, location)
    repo.save_location(
        Location(
            id=did, name=nombre, branch_id=location.branch_id,
            location_type=location.location_type, active=bool(activo),
            description=descripcion, is_default=location.is_default,
        )
    )
    if location.branch_id is not None and (se_desactiva or (activo and not location.active)):
        _asegurar_deposito_predeterminado(conn, location.branch_id)


def set_default_deposito(conn, did: int):
    """🔴 **No se puede marcar como default un depósito inactivo** — mismo
    motivo que la guarda de `update_deposito`: `get_default_deposito_id` no
    mira `active`, así que un default inactivo le seguiría cargando stock en
    silencio a un depósito dado de baja."""
    row = conn.execute("SELECT active FROM locations WHERE id=?", (did,)).fetchone()
    if row is not None and not row[0]:
        raise ValueError(
            "No se puede marcar como default un depósito inactivo: activalo primero."
        )
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
    location = repositorio_de(conn).get_location(did)
    if location is not None and location.active:
        _verificar_no_es_el_ultimo_deposito(conn, location)
    conn.execute("DELETE FROM locations WHERE id=?", (did,))
    if location is not None and location.branch_id is not None:
        _asegurar_deposito_predeterminado(conn, location.branch_id)


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


def _tramos_de_transferencia(conn, producto_id, origen_id, destino_id, cantidad, variant_id):
    """Los tramos FEFO de la transferencia de un producto MARCADO con lotes en el origen, o `None` (el camino de
    siempre: un par sin lote) si no está marcado, no hay lotes de los que sacar, o la operación es inválida (cantidad no
    positiva, mismo origen y destino: que la rechace `transfer_stock` como hoy, sin tomar nada). Toma el producto antes
    de planificar."""
    if cantidad <= 0 or origen_id == destino_id:
        return None
    # Import diferido: `erp.lotes` importa este módulo (`get_default_deposito_id`).
    from . import lotes

    if not lotes.ids_marcados(conn, [producto_id]):
        return None
    lotes.tomar_productos(conn, [producto_id])
    plan = lotes.plan_fefo(conn, producto_id, origen_id, variant_id, Decimal(str(cantidad)))
    if all(tr.sin_lote for tr in plan):
        return None     # un marcado sin lotes en el origen: el par de siempre, idéntico al de un producto sin marcar
    return [
        TramoDeTransferencia(tr.cantidad, tr.lote, _date.fromisoformat(tr.vence) if tr.vence else None)
        for tr in plan
    ]


def transferir_stock(conn, producto_id: int, origen_id: int, destino_id: int,
                     cantidad: float, usuario_id: int | None = None,
                     fecha: str = "", observaciones: str = "", variant_id: int | None = None):
    """Mueve stock entre depósitos, atómico y con la guarda de disponibilidad
    adentro de la misma transacción (`transfer_stock`, motor `v0.7.1`).

    El `reason_code` de cada pata (`transferencia_salida`/`transferencia_entrada`)
    es el vocabulario que el log de actividad muestra sin mapa, y **el texto del
    error** es el que el endpoint devuelve en un 422 y ve el usuario: el del
    motor nombra ids, que no le dicen nada a quien mira nombres.

    🔴 **`origen_id` y `destino_id` se validan con `validar_deposito` ANTES de
    llamar a `transfer_stock`** —los dos, no sólo uno—. Sin esto: un
    `destino_id` inexistente escribía la pata de SALIDA (dentro de la misma
    transacción de `transfer_stock`, pero antes del `INSERT` que revienta) y
    recién ahí reventaba con la `IntegrityError` de la FK, sin manejar (500);
    un `destino_id` inactivo se aceptaba sin aviso, dejando el movimiento en
    un depósito dado de baja; y un `origen_id` inexistente terminaba en el
    422 de "Stock insuficiente" —engañoso: el problema no era la cantidad, el
    depósito no existía—.

    🔑 **Productos marcados (`tracks_expiry = 1`, ADR-018, A-4 PR-3): un par por lote.** La transferencia de un marcado
    con lotes en el origen sale por FEFO (`lotes.plan_fefo` sobre el depósito de ORIGEN y la variante: vencidos
    incluidos, con aviso en otra capa; «sin lote» último; el faltante no aplica porque la guarda de disponibilidad total
    ya exige stock) y escribe **un par salida/entrada por tramo**, en la misma transacción: la salida lleva el lote y el
    vencimiento, la entrada los copia y cada entrada sigue apuntando a SU salida con `source_id`. Una transferencia de N
    lotes son N pares y `get_transferencias` muestra N filas (una por tramo, cada una con su cantidad). Antes de leer los
    saldos el producto se toma (`lotes.tomar_productos`): dos transferencias (o una venta) del mismo producto se
    serializan en PostgreSQL. Un producto sin marcar, y un marcado sin lotes, escriben el par de siempre (sin lote); la
    guarda de disponibilidad total no cambia.
    """
    validar_deposito(conn, origen_id)
    validar_deposito(conn, destino_id)
    _fecha = _datetime.fromisoformat(fecha or _date.today().isoformat())
    ref = observaciones or "Transferencia entre depósitos"
    tramos = _tramos_de_transferencia(conn, producto_id, origen_id, destino_id, cantidad, variant_id)
    try:
        transfer_stock(
            repositorio_de(conn),
            item_id=producto_id,
            variant_id=variant_id,
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
            tramos=tramos,
        )
    except StockInsuficienteError as e:
        raise ValueError(
            f"Stock insuficiente en depósito origen (disponible: {float(e.disponible)})."
        ) from e


def get_transferencias(conn, deposito_id: int | None = None, limite: int = 200) -> list[dict]:
    """El historial de transferencias, reconstruido desde el ledger: no hay tabla propia.

    Cada transferencia son DOS filas de `stock_movements` que `transfer_stock` aparea: la entrada lleva
    `source_type='transfer'` y `source_id` = id de la salida (la salida se escribe primero y los movimientos son
    inmutables, así que el ancla es la salida). Un ajuste manual no aparece acá por dos guardas independientes: el
    `movement_type = 'transfer_out'` y el `JOIN` estricto con la contraparte; conviene saberlo antes de sacar una
    por "redundante".

    `deposito_id` trae las que tocan ese depósito **de los dos lados** (lo que salió y lo que entró).
    """
    where = ""
    params: list = ["transfer_out"]
    if deposito_id is not None:
        where = "AND (salida.location_id = ? OR entrada.location_id = ?)"
        params += [deposito_id, deposito_id]
    params.append(limite)
    rows = conn.execute(
        f"""
        SELECT salida.id, salida.item_id, salida.variant_id, -salida.quantity_delta,
               salida.location_id, entrada.location_id, salida.occurred_at, salida.note,
               salida.created_by, ci.name
        FROM stock_movements AS salida
        JOIN stock_movements AS entrada
          ON entrada.source_type = 'transfer' AND entrada.source_id = salida.id
        LEFT JOIN catalog_items AS ci ON ci.id = salida.item_id
        WHERE salida.movement_type = ? {where}
        ORDER BY salida.occurred_at DESC, salida.id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    nombres = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM locations").fetchall()}
    return [
        {
            "id": r[0], "producto_id": r[1], "producto": r[9] or f"#{r[1]}", "variant_id": r[2],
            "cantidad": float(r[3]),
            "origen_id": r[4], "origen": nombres.get(r[4], f"#{r[4]}"),
            "destino_id": r[5], "destino": nombres.get(r[5], f"#{r[5]}"),
            "fecha": str(r[6]), "observaciones": r[7] or "", "usuario_id": r[8],
        }
        for r in rows
    ]


# ── Sucursales ───────────────────────────────────────────────────────────
#
# Portado desde LibraDesk (`app/services/comercial.py`), el primer y hasta
# ahora único consumidor con sucursales reales (decidido el 2026-08-14: "eje
# transversal, no instancia aparte"). `locations.branch_id` es la columna del
# motor a la que esto apunta — existía sin tabla propia desde Fase 4
# (2026-07-26); Contalibra la deja en NULL siempre.
#
# Igual que `sucursales` en LibraDesk, esta tabla **no tiene FK** contra
# `locations.branch_id`/`sales.branch_id`/`purchase_orders.branch_id`/
# `item_prices.branch_id`: son columnas sueltas desde antes de que existiera
# esta tabla, y agregar la FK ahora es una migración de datos sobre las bases
# ya desplegadas de Contalibra/VentaLibra/LibraDesk (fuera de este alcance,
# ver `wiki/analyses/jerarquia-sucursal-deposito-libracommerce.md`). Por eso
# la baja es **lógica, nunca DELETE**: sin FK no hay cascada, y un DELETE
# dejaría esas cuatro tablas apuntando a un id inexistente.


class SucursalInexistente(ValueError):
    """Un `branch_id` (depósito, venta, orden de compra o precio) no existe o
    no está activo en `branches`. Mismo mecanismo que `DepositoInexistente`:
    hereda de `ValueError` para que el 422 del router sea el mismo camino."""


def _sucursal_dict(row) -> dict:
    return {
        "id": row["id"], "nombre": row["name"], "codigo": row["code"],
        "direccion": row["address"], "activa": bool(row["active"]),
        "es_default": bool(row["is_default"]),
        "deposito_predeterminado_id": row["default_location_id"],
    }


def get_all_sucursales(conn, solo_activas: bool = False) -> list[dict]:
    """Cada fila trae `depositos`: cuántos depósitos activos tiene, igual que
    `listar_sucursales` de LibraDesk — es lo que la pantalla necesita para no
    mostrar una sucursal sin ningún lugar donde cargar stock."""
    sql = "SELECT id, name, code, address, active, is_default, default_location_id FROM branches"
    if solo_activas:
        sql += " WHERE active = 1"
    sql += " ORDER BY is_default DESC, name"
    rows = conn.execute(sql).fetchall()
    resultado = []
    for r in rows:
        d = _sucursal_dict(r)
        d["depositos"] = conn.execute(
            "SELECT COUNT(*) FROM locations WHERE branch_id = ? AND active = 1", (r["id"],)
        ).fetchone()[0]
        resultado.append(d)
    return resultado


def get_sucursal(conn, sid: int) -> dict | None:
    row = conn.execute(
        "SELECT id, name, code, address, active, is_default, default_location_id FROM branches WHERE id=?", (sid,)
    ).fetchone()
    return _sucursal_dict(row) if row else None


def validar_sucursal(conn, sucursal_id: int | None) -> None:
    """Levanta `SucursalInexistente` si `sucursal_id` no es `None` y no
    resuelve a una sucursal existente y activa. `None` es válido: "sin
    sucursal", el caso de una empresa de un solo local.

    Sin esto la falta de FK se paga en el alta: un `branch_id` inventado
    entra sin chistar y el depósito desaparece de toda pantalla filtrada por
    sucursal (mismo defecto que `verificar_sucursal` cierra en LibraDesk)."""
    if sucursal_id is None:
        return
    sucursal = get_sucursal(conn, sucursal_id)
    if sucursal is None or not sucursal["activa"]:
        raise SucursalInexistente(
            f"La sucursal {sucursal_id} no existe o no está activa."
        )


def get_default_sucursal_id(conn) -> int | None:
    row = conn.execute("SELECT id FROM branches WHERE is_default=1 LIMIT 1").fetchone()
    return row[0] if row else None


def create_sucursal(
    conn, nombre: str, codigo: str = "", direccion: str = "",
    deposito: str | None = None, deposito_tipo: str | None = None,
) -> int:
    """Crea la sucursal **y su primer depósito**, que queda como predeterminado: toda sucursal declara como
    mínimo un depósito (decisión del humano, 2026-09-28) y el stock vive sólo en depósitos. Sin `deposito`, se
    llama «Depósito <nombre de la sucursal>»; `deposito_tipo` es el `location_type` (el motor no valida el
    vocabulario, es del producto)."""
    nombre = nombre.strip()
    cur = conn.execute(
        "INSERT INTO branches (name, code, address) VALUES (?, ?, ?)",
        (nombre, codigo, direccion),
    )
    sid = cur.lastrowid
    create_deposito(
        conn, (deposito or "").strip() or f"Depósito {nombre}", tipo=deposito_tipo, branch_id=sid
    )
    return sid


def update_sucursal(conn, sid: int, nombre: str, codigo: str, direccion: str, activo: int):
    """🔴 **No se puede desactivar la sucursal por defecto** (mismo criterio
    que `update_deposito`) **ni una sucursal con existencias en sus depósitos** —ver
    `_verificar_baja_de_sucursal`, portada de LibraDesk—. Al darla de baja se dan de baja también sus
    depósitos; al reactivarla se reactiva su predeterminado, porque una sucursal activa siempre tiene al
    menos un depósito activo."""
    actual = get_sucursal(conn, sid)
    if actual is None:
        raise ValueError("La sucursal no existe.")
    if actual["es_default"] and not activo:
        raise ValueError(
            "No se puede desactivar la sucursal por defecto: primero hay que "
            "marcar otra sucursal como default."
        )
    se_desactiva = actual["activa"] and not activo
    if se_desactiva:
        _verificar_baja_de_sucursal(conn, sid)
    conn.execute(
        "UPDATE branches SET name=?, code=?, address=?, active=? WHERE id=?",
        (nombre.strip(), codigo, direccion, int(activo), sid),
    )
    if se_desactiva:
        # La baja arrastra a sus depósitos (sin existencias, ya verificado): una sucursal inactiva con depósitos
        # activos dejaría stock ofrecido en pantallas que ya no muestran la sucursal.
        conn.execute("UPDATE locations SET active=0 WHERE branch_id=?", (sid,))
    elif not actual["activa"] and activo:
        _reactivar_deposito_de(conn, sid)


def _verificar_baja_de_sucursal(conn, sucursal_id: int) -> None:
    """Se planta si la sucursal todavía tiene algo vivo colgando.

    Portado de `_verificar_baja_de_sucursal` de LibraDesk
    (`app/services/comercial.py`), incluida la corrección del 2026-08-16: se
    miran las EXISTENCIAS (ternas ítem/variante/depósito con saldo `<> 0`, no `> 0` —
    un stock negativo tampoco es "nada que mover"), y un depósito desactivado
    con stock adentro también cuenta.

    🔵 **Diferencia con LibraDesk (ADR-013):** acá un depósito activo *vacío* ya no bloquea la baja. Con la
    invariante «toda sucursal activa tiene un depósito activo» (`_verificar_no_es_el_ultimo_deposito`) esa
    condición era imposible de cumplir —no se puede desactivar el último depósito antes que la sucursal ni
    la sucursal antes que sus depósitos—; lo que protegía (que el stock no quede invisible) lo cubre el
    chequeo de existencias, y `update_sucursal` da de baja los depósitos junto con la sucursal. La
    historia (`sales`/`purchase_orders`/`item_prices` con este `branch_id`)
    **no bloquea**: bloquear por eso haría imposible cerrar una sucursal que
    alguna vez vendió algo, nunca."""
    problemas = []

    es_default = conn.execute(
        "SELECT COUNT(*) AS n FROM locations WHERE branch_id=? AND is_default=1", (sucursal_id,)
    ).fetchone()["n"]
    if es_default:
        problemas.append("el depósito por defecto de la instancia")

    con_saldo = conn.execute(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT sm.item_id, sm.variant_id, sm.location_id
            FROM stock_movements sm
            JOIN locations l ON l.id = sm.location_id
            WHERE l.branch_id = ?
            GROUP BY sm.item_id, sm.variant_id, sm.location_id
            HAVING SUM(sm.quantity_delta) <> 0
        ) x
        """,
        (sucursal_id,),
    ).fetchone()["n"]
    if con_saldo:
        problemas.append(f"{con_saldo} producto(s) con existencias en sus depósitos")

    if problemas:
        raise ValueError(
            "La sucursal todavía tiene " + " y ".join(problemas) + ". "
            "Transferí el stock a otra sucursal (y marcá otro depósito como default de la instancia, si "
            "corresponde) antes de dar de baja la sucursal."
        )


def _asegurar_deposito_predeterminado(conn, sucursal_id: int) -> None:
    """Deja `branches.default_location_id` apuntando a un depósito ACTIVO de la sucursal: el que ya tenía, si
    sigue siéndolo, y si no el activo de menor id (`NULL` si no queda ninguno)."""
    fila = conn.execute(
        "SELECT default_location_id FROM branches WHERE id=?", (sucursal_id,)
    ).fetchone()
    if fila is None:
        return
    actual = fila[0]
    if actual is not None:
        vigente = conn.execute(
            "SELECT 1 FROM locations WHERE id=? AND branch_id=? AND active=1", (actual, sucursal_id)
        ).fetchone()
        if vigente:
            return
    nuevo = conn.execute(
        "SELECT id FROM locations WHERE branch_id=? AND active=1 ORDER BY id LIMIT 1", (sucursal_id,)
    ).fetchone()
    conn.execute(
        "UPDATE branches SET default_location_id=? WHERE id=?", (nuevo[0] if nuevo else None, sucursal_id)
    )


def _verificar_no_es_el_ultimo_deposito(conn, location) -> None:
    """Levanta `ValueError` si `location` es el único depósito activo de una sucursal activa. Un depósito
    sin sucursal, o de una sucursal ya dada de baja, no tiene esta guarda."""
    if location.branch_id is None:
        return
    sucursal = get_sucursal(conn, location.branch_id)
    if sucursal is None or not sucursal["activa"]:
        return
    otros = conn.execute(
        "SELECT COUNT(*) FROM locations WHERE branch_id=? AND active=1 AND id<>?",
        (location.branch_id, location.id),
    ).fetchone()[0]
    if not otros:
        raise ValueError(
            f"«{location.name}» es el único depósito activo de la sucursal «{sucursal['nombre']}»: "
            "creá otro antes de quitarlo, o dá de baja la sucursal."
        )


def _reactivar_deposito_de(conn, sucursal_id: int) -> None:
    """Al reactivar una sucursal: si quedó sin ningún depósito activo, reactiva el predeterminado (o el de
    menor id) y, si no tuviera ninguno, crea uno."""
    activos = conn.execute(
        "SELECT COUNT(*) FROM locations WHERE branch_id=? AND active=1", (sucursal_id,)
    ).fetchone()[0]
    if not activos:
        candidato = conn.execute(
            "SELECT l.id FROM locations l JOIN branches b ON b.id = l.branch_id "
            "WHERE l.branch_id=? ORDER BY (l.id = b.default_location_id) DESC, l.id LIMIT 1",
            (sucursal_id,),
        ).fetchone()
        if candidato:
            conn.execute("UPDATE locations SET active=1 WHERE id=?", (candidato[0],))
        else:
            nombre = get_sucursal(conn, sucursal_id)["nombre"]
            create_deposito(conn, f"Depósito {nombre}", branch_id=sucursal_id)
    _asegurar_deposito_predeterminado(conn, sucursal_id)


def get_deposito_de_venta(conn, sucursal_id: int) -> int | None:
    """El depósito del que descuenta una venta hecha en esta sucursal: su predeterminado si sigue activo, y
    si no (datos de antes de esta invariante) el activo de menor id. `None` si la sucursal no existe o no
    tiene ninguno activo — quien llama decide si eso es un error."""
    if get_sucursal(conn, sucursal_id) is None:
        return None
    _asegurar_deposito_predeterminado(conn, sucursal_id)
    return conn.execute("SELECT default_location_id FROM branches WHERE id=?", (sucursal_id,)).fetchone()[0]


def set_deposito_predeterminado(conn, sucursal_id: int, deposito_id: int):
    """Marca cuál de los depósitos de la sucursal es el de venta. Tiene que ser de esa sucursal y estar activo."""
    if get_sucursal(conn, sucursal_id) is None:
        raise ValueError("La sucursal no existe.")
    fila = conn.execute(
        "SELECT active FROM locations WHERE id=? AND branch_id=?", (deposito_id, sucursal_id)
    ).fetchone()
    if fila is None:
        raise ValueError("El depósito no pertenece a esa sucursal.")
    if not fila[0]:
        raise ValueError("No se puede marcar como predeterminado un depósito inactivo: activalo primero.")
    conn.execute("UPDATE branches SET default_location_id=? WHERE id=?", (deposito_id, sucursal_id))


def set_default_sucursal(conn, sid: int):
    row = conn.execute("SELECT active FROM branches WHERE id=?", (sid,)).fetchone()
    if row is None:
        raise ValueError("La sucursal no existe.")
    if not row[0]:
        raise ValueError("No se puede marcar como default una sucursal inactiva: activala primero.")
    conn.execute("UPDATE branches SET is_default=0")
    conn.execute("UPDATE branches SET is_default=1 WHERE id=?", (sid,))


# ── Categorías de producto ───────────────────────────────────────────────


def get_categorias_producto(conn) -> list[dict]:
    """Las categorías que se ofrecen al cargar un producto: las activas (`categories.active` es de un producto que las
    da de baja sin borrarlas, como VentaLibra; para el resto todas lo están)."""
    rows = conn.execute("SELECT id, name FROM categories WHERE active = 1 ORDER BY name").fetchall()
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
           ci.category_id, COALESCE(cat.name, '') AS categoria,
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
        "categoria_id": row["category_id"],
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
    with _codigo_repetido_como_error_de_dominio(codigo):
        repositorio_de(conn).save_item_code(
            ItemCode(id=None, item_id=pid, code_type=ItemCodeType.SCALE, code=codigo)
        )


def _set_codigo(repo, conn, item_id: int, codigo: str):
    """`productos.codigo` era una columna UNIQUE; acá es el `item_code` interno
    primario. Se reemplaza el anterior en vez de acumular códigos, para
    preservar la semántica de "un código por producto".

    **Si el código no cambió, no se toca nada**: el primario puede ser de otro tipo (un código de barras, el que un
    producto con varios códigos —VentaLibra— marcó como principal) y reescribirlo como `internal` en cada edición le
    cambiaría el tipo sin que nadie lo pidiera."""
    actual = conn.execute(
        "SELECT code FROM item_codes WHERE item_id=? AND is_primary=1", (item_id,)
    ).fetchone()
    if actual is not None and actual[0] == codigo:
        return
    if actual is None and not codigo:
        return
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


def _unidad(conn, codigo: str, permite_fraccion: bool) -> Unit:
    """La unidad con lo que YA tiene `units` para ese código (nombre, escala decimal), y `allows_fraction` como se
    pidió. `_upsert_unit` reescribe la fila entera en cada `save_catalog_item`: con `Unit(code, name=code)` un producto
    de una instancia que administra sus unidades (VentaLibra: «Kilogramo», escala 3) las pisaba con el código y escala 0
    cada vez que se guardaba."""
    row = conn.execute("SELECT name, decimal_scale FROM units WHERE code=?", (codigo,)).fetchone()
    if row is None:
        return Unit(code=codigo, name=codigo, allows_fraction=permite_fraccion)
    return Unit(code=codigo, name=row["name"], allows_fraction=permite_fraccion, decimal_scale=row["decimal_scale"])


def _catalog_item(pid: int | None, *, nombre, unidad, categoria_id, descripcion, activo, vendible,
                  estacion, precio_venta, precio_costo, stock_minimo, item_type,
                  permite_fraccion: bool = False, unit: Unit | None = None) -> CatalogItem:
    return CatalogItem(
        id=pid, item_type=item_type, name=nombre,
        unit=unit or Unit(code=unidad, name=unidad, allows_fraction=permite_fraccion),
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
    repo = repositorio_de(conn)
    # El producto y su código en una sola transacción, como `update_producto` (ADR-028; ADR-029): `save_catalog_item` confirmaba solo, así que un código repetido
    # (que falla recién en `_set_codigo`) dejaba el producto guardado y sin código.
    with _codigo_repetido_como_error_de_dominio(codigo), repo.transaction():
        saved = repo.save_catalog_item(_catalog_item(
            None, nombre=nombre, unidad=unidad, categoria_id=_resolver_categoria_id(conn, categoria),
            descripcion=descripcion, activo=True, vendible=vendible, estacion=estacion,
            precio_venta=precio_venta, precio_costo=precio_costo, stock_minimo=stock_minimo,
            item_type=item_type, permite_fraccion=_resolver_permite_fraccion(conn, unidad, permite_fraccion),
            unit=_unidad(conn, unidad, _resolver_permite_fraccion(conn, unidad, permite_fraccion)),
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


#: Las dos formas de cada letra mapean a la forma SIN acento y en minúscula: así ni `_sin_acentos` (Python) ni
#: `_sin_acentos_sql` dependen de que `LOWER()` sepa bajar una vocal acentuada.
_QUITAR_ACENTOS = (
    ("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u"), ("ñ", "n"),
    ("Á", "a"), ("É", "e"), ("Í", "i"), ("Ó", "o"), ("Ú", "u"), ("Ü", "u"), ("Ñ", "n"),
)


def _sin_acentos(texto: str) -> str:
    for con_acento, sin_acento in _QUITAR_ACENTOS:
        texto = texto.replace(con_acento, sin_acento)
    return texto.lower()


def _sin_acentos_sql(columna: str) -> str:
    """La misma normalización como expresión SQL. `columna` es siempre un nombre fijo, nunca un valor de usuario."""
    expr = columna
    for con_acento, sin_acento in _QUITAR_ACENTOS:
        expr = f"REPLACE({expr}, '{con_acento}', '{sin_acento}')"
    return f"LOWER({expr})"


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
    for termino in q.split():
        # Sin distinguir mayúsculas ni acentos, en ningún motor: con `LIKE` a secas PostgreSQL sí distingue mayúsculas
        # (`yerba` no encontraba `Yerba`, hallazgo de M2) y ni SQLite ni PostgreSQL saben bajar una vocal acentuada sin
        # ICU/locale, así que se normaliza con `REPLACE` (ver `_QUITAR_ACENTOS`). Y **todos** los términos, en cualquier
        # orden, cada uno en el nombre, el código o la categoría: «simple cono» encuentra «Cono Simple».
        where.append(
            f"({_sin_acentos_sql('ci.name')} LIKE ? OR {_sin_acentos_sql('ic.code')} LIKE ?"
            f" OR {_sin_acentos_sql('cat.name')} LIKE ?)"
        )
        params += [f"%{_sin_acentos(termino)}%"] * 3
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


def _exigir_minimo_bajo_el_techo(conn, pid: int, stock_minimo) -> None:
    """ADR-020: si el producto tiene un techo de reposición (`max_stock`), el stock mínimo no puede pasarlo. `fijar_parametros` ya lo
    exige al cargar el techo; acá se exige al subir el mínimo, para que editar el producto no deje la invariante rota. `ValueError`
    (el router lo contesta 422), pero sólo si el mínimo SUBE: dejar el que ya tenía no se rechaza. Toma el candado del producto (`_bloquear_producto`) antes de leer el
    techo: queda tomado hasta el commit del final de `update_producto` (guardado y código en una sola transacción, ADR-028) o hasta que la transacción termina. Una base sin la revisión `0003` no tiene techos: no hace
    nada ni bloquea."""
    from .reposicion import _bloquear_producto, tiene_parametros

    if not tiene_parametros(conn):
        return
    # El candado del producto ANTES de leer el techo (ADR-026), el mismo que toman `fijar_parametros` y `fijar_minimo_sucursal`: sin él, esta lectura ve el techo
    # de antes del que otro pedido todavía no confirmó, y el mínimo nuevo y el techo nuevo se cruzan. Siempre producto primero (el orden de `delete_producto`).
    _bloquear_producto(conn, pid)
    fila = conn.execute("SELECT max_stock, min_stock FROM catalog_items WHERE id = ?", (pid,)).fetchall()
    if not fila or fila[0]["max_stock"] is None:
        return
    techo, minimo = Decimal(str(fila[0]["max_stock"])), Decimal(str(stock_minimo))
    # Sólo se rechaza SUBIR el mínimo por encima del techo: un producto que ya los tenía cruzados (de antes de esta guarda) puede seguir
    # editándose —precio, nombre, la actualización masiva de precios que reenvía el mínimo tal cual— y bajar su mínimo hacia el techo.
    if minimo > techo and minimo > Decimal(str(fila[0]["min_stock"] or 0)):
        raise ValueError(f"el stock mínimo ({minimo}) no puede ser mayor que el stock máximo de reposición del producto ({techo}); "
                         "bajá o quitá el máximo primero")


def update_producto(conn, pid: int, nombre: str, codigo: str, descripcion: str,
                    precio_venta: float, precio_costo: float,
                    unidad: str, categoria: str, activo: int,
                    stock_minimo: float = 0, estacion: str = "",
                    vendible: int = 1, tipo: str = "producto",
                    permite_fraccion: bool | None = None):
    item_type = _validar_tipo(tipo)
    _exigir_minimo_bajo_el_techo(conn, pid, stock_minimo)
    repo = repositorio_de(conn)
    anterior = repo.get_catalog_item(pid)
    nuevo = _catalog_item(
        pid, nombre=nombre, unidad=unidad, categoria_id=_resolver_categoria_id(conn, categoria),
        descripcion=descripcion, activo=activo, vendible=vendible, estacion=estacion,
        precio_venta=precio_venta, precio_costo=precio_costo, stock_minimo=stock_minimo,
        item_type=item_type, permite_fraccion=_resolver_permite_fraccion(conn, unidad, permite_fraccion),
        unit=_unidad(conn, unidad, _resolver_permite_fraccion(conn, unidad, permite_fraccion)),
    )
    if anterior is not None:
        # Lo que este payload no maneja se conserva: `purchasable`, `tax_profile` y las claves de `metadata` que no
        # son la estación. Un `CatalogItem` nuevo los reseteaba a su default en cada edición.
        metadata = {k: v for k, v in anterior.metadata.items() if k != "estacion"} | nuevo.metadata
        nuevo = replace(nuevo, purchasable=anterior.purchasable, tax_profile=anterior.tax_profile, metadata=metadata)
    # El guardado y el reemplazo del código principal, en una sola transacción (ADR-028): `repo.transaction()` hace que ninguno confirme solo, así que el candado del
    # producto que tomó `_exigir_minimo_bajo_el_techo` (o el que tomó quien llama, como la actualización masiva) sigue tomado hasta el único commit del final, y un
    # código repetido (que falla en `_set_codigo`) deshace también lo que ya se había guardado.
    with _codigo_repetido_como_error_de_dominio(codigo), repo.transaction():
        repo.save_catalog_item(nuevo)
        _set_codigo(repo, conn, pid, codigo)


def delete_producto(conn, pid: int):
    from .reposicion import _bloquear_producto, tiene_minimos_sucursal

    if tiene_minimos_sucursal(conn):   # ADR-024: los mínimos por sucursal cuelgan del producto (FK) y se van con él
        # Primero el candado del producto y después sus mínimos, el mismo orden que `fijar_minimo_sucursal`: al revés (mínimos y luego producto), un borrado
        # a la vez con la carga de un mínimo existente se esperan uno al otro (deadlock en PostgreSQL).
        _bloquear_producto(conn, pid)
        conn.execute("DELETE FROM item_branch_min_stock WHERE item_id=?", (pid,))
    conn.execute("DELETE FROM item_codes WHERE item_id=?", (pid,))
    conn.execute("DELETE FROM catalog_items WHERE id=?", (pid,))


# ── Códigos de un producto (varios por producto, de distintos tipos) ─────

TIPOS_DE_CODIGO = tuple(t.value for t in ItemCodeType)


def get_codigos(conn, pid: int) -> list[dict]:
    """Todos los códigos del producto, el principal primero. `codigo` de `get_producto` es sólo el principal."""
    rows = conn.execute(
        "SELECT id, item_id, code_type, code, is_primary FROM item_codes WHERE item_id=? "
        "ORDER BY is_primary DESC, id",
        (pid,),
    ).fetchall()
    return [
        {"id": r["id"], "producto_id": r["item_id"], "tipo": r["code_type"], "codigo": r["code"],
         "es_principal": bool(r["is_primary"])}
        for r in rows
    ]


def add_codigo(conn, pid: int, tipo: str, codigo: str, es_principal: bool = False) -> dict:
    """Agrega un código. `ValueError` si el tipo no es de `TIPOS_DE_CODIGO`; `CodigoRepetido` (también un `ValueError`, con el
    mensaje para la persona) si ya existe ese `(tipo, código)`. El `IntegrityError` de un segundo principal (u otro error de
    integridad) sube tal cual y lo traduce el router (409)."""
    if tipo not in TIPOS_DE_CODIGO:
        raise ValueError(f"tipo de código inválido: {tipo!r} (los válidos: {', '.join(TIPOS_DE_CODIGO)})")
    with _codigo_repetido_como_error_de_dominio(codigo):
        repositorio_de(conn).save_item_code(
            ItemCode(id=None, item_id=pid, code_type=ItemCodeType(tipo), code=codigo, is_primary=es_principal)
        )
    return next(c for c in get_codigos(conn, pid) if c["codigo"] == codigo and c["tipo"] == tipo)


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
    saved = repositorio_de(conn).save_item_variant(
        ItemVariant(id=None, item_id=pid, sku=sku, name=nombre, attributes=atributos or {})
    )
    return _variante_dict(saved)


def update_variante(conn, vid: int, sku: str, nombre: str,
                    atributos: dict[str, str] | None, activa: bool) -> dict | None:
    repo = repositorio_de(conn)
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
    crudo = repositorio_de(conn).get_setting(_SETTING_FORMATO_BALANZA)
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
    repositorio_de(conn).set_setting(_SETTING_FORMATO_BALANZA, json.dumps(datos))


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
    repo = repositorio_de(conn)

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
