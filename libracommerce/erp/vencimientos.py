"""Vencimientos y lotes, la parte informativa (A-1, ADR-018, 2026-09-30).

Roadmap de producto de VentaLibra, A-1. Contalibra, Restolibra y VentaLibra comparten este motor, así que todo es
**aditivo y opt-in por producto**: un producto que nadie marca con `marcar_vence` no cambia en nada (su ledger queda
byte a byte igual) y no aparece en ningún reporte de acá. **No toca el camino de ventas**: ni la venta, ni la
anulación, ni la devolución, ni la transferencia eligen lote todavía (eso es A-4, con FEFO).

**El lote es una dimensión del ledger, no una tabla.** `stock_movements` ya tiene `lot_code` y `expires_at`; las
existencias por lote son `SUM(quantity_delta)` agrupado por producto, depósito, variante, lote y vencimiento
(`lotes_de`). El bucket con `lot_code` y `expires_at` en NULL es el stock **«sin lote»**. Por partir el ledger en
más grupos el stock total de un producto/depósito no cambia (`get_stock_actual` sigue sumando todo).

**Lo que hay** (todo lo escribe `erp.stock.add_movimiento_stock`, aditivo, nunca un `UPDATE`):

- `marcar_vence`: la marca `catalog_items.tracks_expiry` (revisión Alembic `0002`).
- `lotes_de`: las existencias por lote de un producto.
- `proximos_a_vencer`: el reporte por lote, de lo que vence dentro de `dias` (15 por defecto, tope 365) y lo ya vencido.
- `asignar_vencimiento_a_saldo`: le pone lote y vencimiento a saldo que hoy no lo tiene, con un **par** de filas de
  ajuste (una negativa sin lote y una positiva con lote, misma referencia).
- `dar_de_baja_lote`: la merma de un lote concreto, sin dejar su saldo negativo.

**«Hoy» es la fecha de Argentina** (UTC-3 fijo, como `erp.listas_precio`), no la del servidor: una consulta a las 22:00
de Buenos Aires no salta de día porque el contenedor esté en UTC. Es lo que hace que «vence hoy» sea estable.

**Vencido** es `vence < hoy`: un producto que vence hoy todavía es `por_vencer` (con `dias_para_vencer = 0`).
La ventana es cerrada: entra lo que vence hasta `hoy + dias` inclusive.

🔴 **Hasta A-4 el saldo por lote sobreestima lo que hay** en un producto marcado: la venta, la anulación, la devolución,
la transferencia y `ajustar_stock` siguen escribiendo movimientos **sin lote**, así que restan del bucket «sin lote»
(que puede quedar negativo) y no del lote del que salió la mercadería. `proximos_a_vencer` lo hace visible: la lista
`sin_lote` incluye los productos con saldo sin lote **negativo** (`situacion='salidas_sin_lote'`), que son exactamente
los que tienen salidas todavía no descontadas de ningún lote. Los saldos ≠ 0 de `lotes_de` también lo muestran.

**Actualización (A-4 PR-2, 2026-09-30):** para la venta y la anulación lo de arriba ya no aplica: un producto marcado vende por
FEFO (`erp.lotes`) y la anulación repone al lote de origen. Sigue valiendo para la devolución, la transferencia y
`ajustar_stock` (hasta el PR-3) y para las ventas anteriores.

Las consultas son las mismas en SQLite y PostgreSQL: se agrupa en SQL y se limpia y fusiona en Python con `Decimal`
(un mismo lote puede estar guardado como `'2026-10-05'` o como `'2026-10-05T00:00:00'` según quién lo escribió: la
recepción de compras guarda el `isoformat()` del `datetime`; se normaliza al leer). Depósitos: se miran **todos**
(activos o no), porque la mercadería existe aunque el depósito ya no se use; cada fila trae `deposito_activo`.
Productos: `proximos_a_vencer` sólo mira los **activos**. Ninguna función commitea: quien la llama (el router, con su
`with abrir()`) decide; las que escriben validan **antes** de escribir la primera fila.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal, InvalidOperation

from .listas_precio import _ZONA_LOCAL
from .lotes import CERO as _CERO
from .lotes import dec as _dec
from .lotes import lote_de_fila as _lote_de_fila
from .lotes import saldo_limpio as _saldo
from .lotes import saldos_por_bucket as _saldos
from .lotes import vence_de_fila as _vence_de_fila
from .stock import add_movimiento_stock, normalizar_lote, normalizar_vencimiento

DIAS_AVISO = 15
#: Tope de la anticipación del aviso: un año.
MAX_DIAS_AVISO = 365

ESTADOS = ("vencido", "por_vencer")

#: Largo máximo de la `clave_operacion` (un UUID en texto son 36).
MAX_LARGO_CLAVE = 64


class VencimientosError(Exception):
    """Base de los errores de negocio de este módulo (los de entrada inválida son `ValueError`)."""


class ProductoNoEncontrado(VencimientosError, LookupError):
    """El producto no existe: el router lo traduce a 404."""


class ReglaDeNegocio(VencimientosError):
    """La operación es válida pero el estado de los datos no la permite: el router la traduce a 409."""


class SaldoInsuficiente(ReglaDeNegocio):
    """No hay saldo suficiente en el bucket del que se quiere sacar."""


class ClaveDeOperacionReusada(ReglaDeNegocio):
    """La `clave_operacion` ya se usó, sobre ese producto, con otros parámetros u otra operación: 409 en el router."""


class SinRevision(VencimientosError):
    """La base no tiene la revisión `0002_vencimientos_lotes`: el router lo traduce a 503."""


def hoy_argentina(ahora: datetime.datetime | None = None) -> datetime.date:
    """La fecha de hoy en Argentina. `ahora` (para las pruebas) es un instante con zona; sin él, el reloj real."""
    ahora = ahora or datetime.datetime.now(datetime.UTC)
    return ahora.astimezone(_ZONA_LOCAL).date()


# ── Utilidades ───────────────────────────────────────────────────────────


def _num(valor: Decimal):
    """Un `int` si es entero, un `float` si no: `5` y no `5.0` para lo que se cuenta de a unidades."""
    return int(valor) if valor == valor.to_integral_value() else float(valor)


def _cantidad_positiva(valor, nombre: str = "cantidad") -> Decimal:
    if isinstance(valor, bool):
        raise ValueError(f"{nombre} tiene que ser un número mayor a 0: {valor!r}")
    try:
        cantidad = Decimal(str(valor))
    except InvalidOperation:
        raise ValueError(f"{nombre} tiene que ser un número mayor a 0: {valor!r}") from None
    if not cantidad.is_finite() or cantidad <= 0:
        raise ValueError(f"{nombre} tiene que ser un número mayor a 0: {valor!r}")
    return cantidad


def _entero_en_rango(nombre: str, valor, maximo: int) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or not 1 <= valor <= maximo:
        raise ValueError(f"{nombre} tiene que ser un entero entre 1 y {maximo}: {valor!r}")
    return valor


def _columnas(conn, tabla: str) -> set[str]:
    return {f[1] for f in conn.execute(f"PRAGMA table_info({tabla})").fetchall()}


def _exigir_revision(conn) -> None:
    if "tracks_expiry" not in _columnas(conn, "catalog_items"):
        raise SinRevision(
            "Falta la revisión 0002_vencimientos_lotes del motor: corré `libracommerce-migrar upgrade` "
            "(--prefijo del producto) antes de usar vencimientos y lotes."
        )


def _sucursal_y_depositos(conn, sucursal_id: int | None, deposito_id: int | None) -> dict[int, dict]:
    """Los depósitos que se miran, `{id: {nombre, activo, sucursal_id, sucursal}}`. `ValueError` si la sucursal o
    el depósito pedidos no existen. Sin filtros, todos."""
    if sucursal_id is not None and not conn.execute("SELECT 1 FROM branches WHERE id = ?", (sucursal_id,)).fetchall():
        raise ValueError(f"la sucursal {sucursal_id} no existe")
    filas = conn.execute(
        """SELECT l.id, l.name, l.active, l.branch_id, b.name AS sucursal
           FROM locations l LEFT JOIN branches b ON b.id = l.branch_id"""
    ).fetchall()
    todos = {
        f["id"]: {"nombre": f["name"], "activo": bool(f["active"]), "sucursal_id": f["branch_id"],
                  "sucursal": f["sucursal"]}
        for f in filas
    }
    if deposito_id is not None and deposito_id not in todos:
        raise ValueError(f"el depósito {deposito_id} no existe")
    return {
        i: d for i, d in todos.items()
        if (deposito_id is None or i == deposito_id) and (sucursal_id is None or d["sucursal_id"] == sucursal_id)
    }


def _nombres_de_variantes(conn) -> dict[int, str]:
    return {f["id"]: (f["name"] or f["sku"] or "") for f in conn.execute("SELECT id, sku, name FROM item_variants")}


def _fila_de_producto(conn, item_id: int):
    fila = conn.execute(
        """SELECT ci.id, ci.name, ci.unit_code, ci.item_type, ci.active, ci.tracks_expiry, ic.code AS codigo
           FROM catalog_items ci
           LEFT JOIN item_codes ic ON ic.item_id = ci.id AND ic.is_primary = 1
           WHERE ci.id = ?""",
        (item_id,),
    ).fetchone()
    if fila is None:
        raise ProductoNoEncontrado(f"el producto {item_id} no existe")
    return fila


def _validar_variante(conn, item_id: int, variante_id: int | None) -> None:
    """`variante_id` (que llega del cliente) tiene que ser una variante **de este producto**: sin esto se podría mover
    saldo del producto A a una variante del B. No exige que esté activa: el motor no lo exige para mover stock, y la
    merma de una variante dada de baja es legítima. `ProductoNoEncontrado` si el producto no existe; `ValueError` si la
    variante no existe o es de otro producto. `None` (el producto sin variantes) siempre vale."""
    if variante_id is None:
        return
    if isinstance(variante_id, bool) or not isinstance(variante_id, int):
        raise ValueError(f"variante_id tiene que ser un entero: {variante_id!r}")
    if not conn.execute("SELECT 1 FROM catalog_items WHERE id = ?", (item_id,)).fetchall():
        raise ProductoNoEncontrado(f"el producto {item_id} no existe")
    fila = conn.execute("SELECT item_id FROM item_variants WHERE id = ?", (variante_id,)).fetchone()
    if fila is None or fila["item_id"] != item_id:
        raise ValueError(f"la variante {variante_id} no existe o no es del producto {item_id}")


def _dias(vence: str | None, hoy: datetime.date) -> int | None:
    return None if vence is None else (datetime.date.fromisoformat(vence) - hoy).days


# ── La marca «vence» ─────────────────────────────────────────────────────


def marcar_vence(conn, item_id: int, vence: bool) -> dict:
    """Marca (o desmarca) a un producto como perecedero. Sólo eso: no escribe movimientos ni cambia stock. Un
    producto sin marcar no aparece en `proximos_a_vencer` y es lo que son todos hasta que alguien lo pide.

    `ValueError` si `vence` no es un `bool`; `ProductoNoEncontrado` si no existe; `ReglaDeNegocio` si es un servicio
    (no tiene inventario). `SinRevision` si la base no tiene la revisión `0002`. Devuelve `{producto_id, vence}`.
    Desmarcar no toca los lotes ya escritos en el ledger."""
    if not isinstance(vence, bool):
        raise ValueError(f"vence tiene que ser verdadero o falso: {vence!r}")
    _exigir_revision(conn)
    producto = _fila_de_producto(conn, item_id)
    if producto["item_type"] == "service":
        raise ReglaDeNegocio("un servicio no tiene inventario: no se le puede marcar vencimiento")
    conn.execute("UPDATE catalog_items SET tracks_expiry = ? WHERE id = ?", (1 if vence else 0, item_id))
    return {"producto_id": item_id, "vence": vence}


def ficha(conn, item_id: int) -> dict:
    """`{producto_id, codigo, nombre, unidad, vence}` de un producto. `ProductoNoEncontrado` si no existe."""
    _exigir_revision(conn)
    p = _fila_de_producto(conn, item_id)
    return {"producto_id": p["id"], "codigo": p["codigo"], "nombre": p["name"], "unidad": p["unit_code"],
            "vence": bool(p["tracks_expiry"])}


# ── Existencias por lote ─────────────────────────────────────────────────


def lotes_de(conn, item_id: int, *, deposito_id: int | None = None, sucursal_id: int | None = None,
             variante_id: int | None = None, hoy: datetime.date | None = None) -> list[dict]:
    """Las existencias de un producto por lote: una fila por `(depósito, variante, lote, vencimiento)` con saldo ≠ 0,
    incluido el bucket **sin lote** (`lote` y `vence` en `None`) y los saldos negativos. Ordenadas por depósito,
    variante, vencimiento (los sin fecha al final) y lote.

    Cada fila: `producto_id`, `deposito_id`, `deposito`, `deposito_activo`, `sucursal_id`, `sucursal`, `variante_id`,
    `variante`, `lote`, `vence` (`'AAAA-MM-DD'` o `None`), `dias_para_vencer` (negativo = vencido, `None` sin fecha),
    `saldo`, `sin_lote` y `estado` (`vencido`, `vigente` o `sin_fecha`). La suma de los saldos de todos los depósitos
    es `erp.stock.get_stock_actual`. Con `variante_id`, sólo esa variante (tiene que ser del producto: si no,
    `ValueError`, antes de leer saldos). No requiere que el producto esté marcado. `ProductoNoEncontrado` si no existe;
    `ValueError` con una sucursal o un depósito que no existen."""
    if not conn.execute("SELECT 1 FROM catalog_items WHERE id = ?", (item_id,)).fetchall():
        raise ProductoNoEncontrado(f"el producto {item_id} no existe")
    _validar_variante(conn, item_id, variante_id)
    hoy = hoy or hoy_argentina()
    depositos = _sucursal_y_depositos(conn, sucursal_id, deposito_id)
    saldos = _saldos(conn, "sm.item_id = ?", [item_id], depositos)
    if variante_id is not None:
        saldos = {k: v for k, v in saldos.items() if k[2] == variante_id}
    variantes = _nombres_de_variantes(conn)
    filas = []
    for (_, dep, variante, lote, vence), saldo in saldos.items():
        d = depositos[dep]
        dias = _dias(vence, hoy)
        filas.append({
            "producto_id": item_id, "deposito_id": dep, "deposito": d["nombre"], "deposito_activo": d["activo"],
            "sucursal_id": d["sucursal_id"], "sucursal": d["sucursal"], "variante_id": variante,
            "variante": variantes.get(variante) if variante is not None else None,
            "lote": lote, "vence": vence, "dias_para_vencer": dias, "saldo": _num(saldo),
            "sin_lote": lote is None and vence is None,
            "estado": "sin_fecha" if dias is None else ("vencido" if dias < 0 else "vigente"),
        })
    return sorted(filas, key=lambda r: (r["deposito_id"], r["variante_id"] or 0, r["vence"] is None,
                                        r["vence"] or "", r["lote"] or ""))


# ── El reporte de próximos a vencer ──────────────────────────────────────


def _productos_que_vencen(conn, categoria: str | None, producto_id: int | None) -> dict[int, dict]:
    donde = ["ci.active = 1", "ci.tracks_expiry = 1"]
    params: list = []
    if producto_id is not None:
        donde.append("ci.id = ?")
        params.append(producto_id)
    filas = conn.execute(
        f"""SELECT ci.id, ci.name, ci.unit_code, COALESCE(cat.name, '') AS categoria, ic.code AS codigo
            FROM catalog_items ci
            LEFT JOIN categories cat ON cat.id = ci.category_id
            LEFT JOIN item_codes ic ON ic.item_id = ci.id AND ic.is_primary = 1
            WHERE {' AND '.join(donde)}""",
        params,
    ).fetchall()
    if categoria:
        # Por nombre, sin distinguir mayúsculas; la categoría directa del producto (como la reposición).
        filas = [f for f in filas if f["categoria"].casefold() == categoria.strip().casefold()]
    return {f["id"]: {"codigo": f["codigo"], "nombre": f["name"], "unidad": f["unit_code"],
                      "categoria": f["categoria"]} for f in filas}


def proximos_a_vencer(conn, *, dias: int = DIAS_AVISO, sucursal_id: int | None = None,
                      deposito_id: int | None = None, categoria: str | None = None,
                      producto_id: int | None = None, incluir_vencidos: bool = True,
                      hoy: datetime.date | None = None) -> dict:
    """Los lotes con saldo > 0 de los productos **marcados** (`tracks_expiry = 1` y activos) que vencen dentro de
    `dias` (la ventana es cerrada: hasta `hoy + dias` inclusive) y, con `incluir_vencidos` (el default), los que ya
    vencieron. Devuelve `{hoy, dias, hasta, resumen, lotes, sin_lote}`.

    `lotes`, por vencimiento (el más próximo primero; después nombre, depósito y lote): `producto_id`, `codigo`,
    `nombre`, `unidad`, `categoria`, `deposito_id`, `deposito`, `deposito_activo`, `sucursal_id`, `sucursal`,
    `variante_id`, `variante`, `lote`, `vence`, `dias_para_vencer` (negativo = vencido), `saldo` y `estado`
    (`vencido` si `vence < hoy`, si no `por_vencer`; un producto que vence hoy es `por_vencer`, con 0 días).

    `sin_lote`: los saldos **sin lote** (sin código ni fecha) ≠ 0 de los productos marcados, **uno por producto,
    depósito y variante** (nunca sumados entre depósitos: un −5 en uno y un +5 en otro no se cancelan), porque ese
    stock no tiene fecha y el reporte no lo puede avisar. `situacion='sin_fecha'` si el saldo es positivo (hay que
    asignarle vencimiento); `situacion='salidas_sin_lote'` si es negativo: 🔴 son salidas (ventas, ajustes...) que hasta
    A-4 no bajan ningún lote, así que **los saldos de sus lotes están sobreestimados** (ver el docstring del módulo).

    `resumen`: `lotes_por_vencer`, `lotes_vencidos`, `unidades_por_vencer`, `unidades_vencidas` (suma de cantidades,
    cada una en la unidad de su producto), `productos` (los distintos con lotes en la lista), `productos_sin_lote` y
    `productos_con_salidas_sin_lote` (productos distintos) y `saldos_sin_fecha` y `saldos_con_salidas_sin_lote` (saldos por
    depósito y variante). No incluye lotes sin vencimiento (no hay qué avisar).

    `hoy` es para las pruebas: el default es la fecha de Argentina. `ValueError` con `dias` fuera de 1..365 o con una
    sucursal o un depósito que no existen. `SinRevision` sin la revisión `0002`. No escribe."""
    _entero_en_rango("dias", dias, MAX_DIAS_AVISO)
    _exigir_revision(conn)
    hoy = hoy or hoy_argentina()
    hasta = hoy + datetime.timedelta(days=dias)
    depositos = _sucursal_y_depositos(conn, sucursal_id, deposito_id)
    productos = _productos_que_vencen(conn, categoria, producto_id)
    saldos = _saldos(conn, "sm.item_id IN (SELECT id FROM catalog_items WHERE tracks_expiry = 1)", [], depositos)
    variantes = _nombres_de_variantes(conn)

    lotes: list[dict] = []
    filas_sin_lote: list[dict] = []
    for (item, dep, variante, lote, vence), saldo in saldos.items():
        if item not in productos:
            continue
        if lote is None and vence is None:
            # Cada bucket sin lote se clasifica POR DEPÓSITO Y VARIANTE, sin sumar antes: −5 en un depósito y +5 en
            # otro no se cancelan (el negativo son salidas que no bajaron ningún lote de ese depósito).
            d, p = depositos[dep], productos[item]
            filas_sin_lote.append({
                "producto_id": item, "codigo": p["codigo"], "nombre": p["nombre"], "unidad": p["unidad"],
                "categoria": p["categoria"], "deposito_id": dep, "deposito": d["nombre"],
                "deposito_activo": d["activo"], "sucursal_id": d["sucursal_id"], "sucursal": d["sucursal"],
                "variante_id": variante, "variante": variantes.get(variante) if variante is not None else None,
                "saldo": _num(saldo), "situacion": "sin_fecha" if saldo > 0 else "salidas_sin_lote",
            })
            continue
        if vence is None or saldo <= 0:
            continue
        dias_para = _dias(vence, hoy)
        if dias_para > dias or (dias_para < 0 and not incluir_vencidos):
            continue
        d, p = depositos[dep], productos[item]
        lotes.append({
            "producto_id": item, "codigo": p["codigo"], "nombre": p["nombre"], "unidad": p["unidad"],
            "categoria": p["categoria"], "deposito_id": dep, "deposito": d["nombre"],
            "deposito_activo": d["activo"], "sucursal_id": d["sucursal_id"], "sucursal": d["sucursal"],
            "variante_id": variante, "variante": variantes.get(variante) if variante is not None else None,
            "lote": lote, "vence": vence, "dias_para_vencer": dias_para, "saldo": _num(saldo),
            "estado": "vencido" if dias_para < 0 else "por_vencer",
        })
    lotes.sort(key=lambda r: (r["vence"], r["nombre"].casefold(), r["deposito_id"], r["lote"] or "",
                              r["producto_id"], r["variante_id"] or 0))

    filas_sin_lote.sort(key=lambda r: (r["situacion"] != "salidas_sin_lote", r["nombre"].casefold(), r["producto_id"],
                                       r["deposito_id"], r["variante_id"] or 0))

    vencidos = [r for r in lotes if r["estado"] == "vencido"]
    por_vencer = [r for r in lotes if r["estado"] == "por_vencer"]
    resumen = {
        "lotes_por_vencer": len(por_vencer), "lotes_vencidos": len(vencidos),
        "unidades_por_vencer": _num(sum((_dec(r["saldo"]) for r in por_vencer), _CERO)),
        "unidades_vencidas": _num(sum((_dec(r["saldo"]) for r in vencidos), _CERO)),
        "productos": len({r["producto_id"] for r in lotes}),
        # Productos distintos con al menos un saldo sin lote de cada clase (un producto puede estar en las dos), y
        # cuántos saldos (depósito × variante) hay de cada clase.
        "productos_sin_lote": len({r["producto_id"] for r in filas_sin_lote if r["situacion"] == "sin_fecha"}),
        "productos_con_salidas_sin_lote": len({r["producto_id"] for r in filas_sin_lote
                                               if r["situacion"] == "salidas_sin_lote"}),
        "saldos_sin_fecha": sum(1 for r in filas_sin_lote if r["situacion"] == "sin_fecha"),
        "saldos_con_salidas_sin_lote": sum(1 for r in filas_sin_lote if r["situacion"] == "salidas_sin_lote"),
    }
    return {"hoy": hoy.isoformat(), "dias": dias, "hasta": hasta.isoformat(), "resumen": resumen,
            "lotes": lotes, "sin_lote": filas_sin_lote}


# ── Escrituras: siempre filas nuevas, nunca un UPDATE del ledger ──────────


def _preparar_escritura(conn, item_id: int, deposito_id: int) -> None:
    """Valida el depósito, exige la revisión, y **toma el producto**: un `UPDATE` de sí mismo lo bloquea hasta el
    commit (fila en PostgreSQL, base en SQLite), así dos asignaciones o bajas simultáneas del mismo producto no leen
    el mismo saldo y lo gastan dos veces. Después de tomarlo se relee el saldo."""
    _exigir_revision(conn)
    if not conn.execute("SELECT 1 FROM locations WHERE id = ?", (deposito_id,)).fetchall():
        raise ValueError(f"el depósito {deposito_id} no existe")
    conn.execute("UPDATE catalog_items SET tracks_expiry = tracks_expiry WHERE id = ?", (item_id,))


def _saldo_del_bucket(conn, item_id: int, deposito_id: int, variante_id: int | None, lote: str | None,
                      vence: str | None, hasta_id: int | None = None) -> Decimal:
    """El saldo de un bucket; con `hasta_id`, el que tenía justo después de escribir el movimiento con ese `id`."""
    sql, params = "sm.item_id = ? AND sm.location_id = ?", [item_id, deposito_id]
    if hasta_id is not None:
        sql, params = sql + " AND sm.id <= ?", params + [hasta_id]
    saldos = _saldos(conn, sql, params, {deposito_id: {}})
    return sum((s for (_, _, v, lo, ve), s in saldos.items() if v == variante_id and lo == lote and ve == vence),
               _CERO)


# ── Idempotencia de las escrituras ────────────────────────────────────────
#
# Un reintento tras un commit con la respuesta perdida no puede duplicar el par ni descontar dos veces. El ledger no
# tiene columna para la clave, así que viaja **al final de la nota** de cada movimiento como `[op:<clave>]` (el mismo
# recurso que `[asignación xxxxxxxx]`) y se escribe en la MISMA transacción que los movimientos.
#
# 🔑 **La unicidad es (producto, clave), no global.** El bloqueo es por producto (`_preparar_escritura`), así que sólo
# serializa a quienes compiten por el MISMO producto: buscar la clave entre los movimientos de ese `item_id`, con el
# producto ya tomado, es lo único que garantiza que el segundo de dos reintentos simultáneos ve la marca del primero.
# Una búsqueda global no lo garantizaría (dos productos distintos con la misma clave tomarían bloqueos distintos y
# ninguno vería al otro). La misma clave sobre otro producto es, simplemente, otra operación.
# Sólo cuenta si está al final de la nota, así que un texto libre (`nota`, `motivo`) no la puede imitar. La búsqueda usa
# el índice del producto y recorre las notas de sus movimientos.


def _normalizar_clave(clave) -> str:
    """La `clave_operacion` recortada: un texto imprimible, no vacío, de hasta `MAX_LARGO_CLAVE` caracteres y sin
    corchetes (delimitan la marca). `ValueError` si no."""
    if not isinstance(clave, str):
        raise ValueError(f"clave_operacion tiene que ser un texto (p. ej. un UUID): {clave!r}")
    clave = clave.strip()
    if not clave:
        raise ValueError("clave_operacion no puede estar vacía: es obligatoria para poder reintentar sin duplicar")
    if len(clave) > MAX_LARGO_CLAVE:
        raise ValueError(f"clave_operacion no puede pasar de {MAX_LARGO_CLAVE} caracteres")
    if "[" in clave or "]" in clave or not clave.isprintable():
        raise ValueError("clave_operacion no puede tener corchetes ni caracteres no imprimibles")
    return clave


def _marca_de_operacion(clave: str) -> str:
    return f"[op:{clave}]"


def _movimientos_de_la_operacion(conn, item_id: int, clave: str) -> list:
    """Los movimientos de `item_id` que ya escribió una operación con esta clave, en orden de escritura (`[]` si es
    nueva). Sólo de ese producto: la clave es única por producto."""
    marca = _marca_de_operacion(clave)
    patron = "%" + marca.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    filas = conn.execute(
        "SELECT id, item_id, location_id, variant_id, movement_type, quantity_delta, lot_code, expires_at, note "
        "FROM stock_movements WHERE item_id = ? AND note LIKE ? ESCAPE '\\' ORDER BY id",
        (item_id, patron),
    ).fetchall()
    return [f for f in filas if str(f["note"]).endswith(marca)]  # LIKE no distingue mayúsculas en SQLite


def _misma_operacion(previa: dict, pedida: dict) -> None:
    if previa != pedida:
        raise ClaveDeOperacionReusada(
            "la clave_operacion ya se usó en este producto con otros parámetros u otra operación: para una operación "
            "distinta hace falta otra clave"
        )


def _texto_del_saldo(valor: Decimal) -> str:
    """Una cantidad para un mensaje: sin ceros de más ni notación científica (`50`, `0.5`)."""
    return format(valor.normalize(), "f")


def asignar_vencimiento_a_saldo(conn, item_id: int, deposito_id: int, lot_code: str, expires_at, cantidad, *,
                                clave_operacion: str, variante_id: int | None = None,
                                usuario_id: int | None = None, nota: str = "", fecha: str = "") -> dict:
    """Le pone lote y vencimiento a `cantidad` unidades del saldo **sin lote** de un producto marcado en un depósito
    (el stock que había antes de usar lotes, o el que entró por una vía que no los pide).

    Escribe un **par aditivo** de movimientos de ajuste con la misma referencia (`nota` más un identificador
    `[asignación xxxxxxxx]`, que las une) y la misma fecha: uno **negativo sin lote** y uno **positivo con el lote y
    el vencimiento**. Nunca modifica una fila ya escrita y el stock total del producto en ese depósito no cambia.
    Si `fecha` no viene es hoy en Argentina.

    **Idempotente por `clave_operacion`** (obligatoria: un texto no vacío de hasta 64 caracteres, sin corchetes; p. ej.
    un UUID que el cliente genera una vez por intento del usuario). Viaja al final de la nota de los dos movimientos
    (`[op:<clave>]`), en la misma transacción, y se busca con el producto ya tomado. **La clave es única por producto**
    (`(item_id, clave)`): la misma clave sobre otro producto es otra operación. Un **reintento con la misma clave y
    los mismos parámetros** no escribe nada y devuelve el resultado de la primera vez con `repetida: True`; con la misma
    clave y **otros** parámetros sobre ese producto (u otra operación), `ClaveDeOperacionReusada` (409).

    `ValueError` con datos inválidos (lote vacío, fecha ilegible, cantidad ≤ 0, clave inválida, depósito inexistente, una
    `variante_id` que no existe o es de otro producto: se valida antes de leer saldos y de escribir);
    `ProductoNoEncontrado`; `ReglaDeNegocio` si el producto no está marcado con `marcar_vence`; `SaldoInsuficiente` si
    el saldo sin lote de ese depósito y variante es menor a `cantidad`. Todo se valida antes de escribir la primera
    fila. Devuelve `{producto_id, deposito_id, variante_id, lote, vence, cantidad, referencia, saldo_sin_lote,
    repetida}` (`saldo_sin_lote` es el que quedó sin lote al terminar la operación)."""
    clave = _normalizar_clave(clave_operacion)
    lote = normalizar_lote(lot_code)
    vence = normalizar_vencimiento(expires_at)
    cant = _cantidad_positiva(cantidad)
    _validar_variante(conn, item_id, variante_id)
    _preparar_escritura(conn, item_id, deposito_id)
    previas = _movimientos_de_la_operacion(conn, item_id, clave)
    if previas:
        return _asignacion_repetida(conn, clave, previas, {
            "tipo": "asignar", "producto_id": item_id, "deposito_id": deposito_id, "variante_id": variante_id,
            "lote": lote, "vence": vence, "cantidad": _saldo(cant)})
    producto = _fila_de_producto(conn, item_id)
    if not producto["tracks_expiry"]:
        raise ReglaDeNegocio(f"el producto {item_id} no está marcado como perecedero: marcalo antes de asignar un "
                             "vencimiento")
    sin_lote = _saldo_del_bucket(conn, item_id, deposito_id, variante_id, None, None)
    if sin_lote < cant:
        raise SaldoInsuficiente(
            f"el saldo sin lote es {_texto_del_saldo(sin_lote)} y se quieren asignar {_texto_del_saldo(cant)}"
        )
    fecha = fecha or hoy_argentina().isoformat()
    marca = f"[asignación {uuid.uuid4().hex[:8]}]"
    referencia = f"{nota.strip()} {marca}" if nota.strip() else f"Asignación de vencimiento {marca}"
    detalle = f"{referencia}: lote {lote}, vence {vence}"
    op = _marca_de_operacion(clave)
    destino = {"usuario_id": usuario_id, "fecha": fecha, "deposito_id": deposito_id, "variant_id": variante_id}
    add_movimiento_stock(conn, item_id, "ajuste", -float(cant), f"{detalle} {_SALE_DE_SIN_LOTE} {op}", **destino)
    add_movimiento_stock(conn, item_id, "ajuste", float(cant), f"{detalle} (entra al lote) {op}",
                         lot_code=lote, expires_at=vence, **destino)
    return {"producto_id": item_id, "deposito_id": deposito_id, "variante_id": variante_id, "lote": lote,
            "vence": vence, "cantidad": _num(cant), "referencia": referencia,
            "saldo_sin_lote": _num(sin_lote - cant), "repetida": False}


_SALE_DE_SIN_LOTE = "(sale de sin lote)"


def _asignacion_repetida(conn, clave: str, filas: list, pedida: dict) -> dict:
    """El resultado de una asignación que ya se escribió con esta clave, o `ClaveDeOperacionReusada` si lo pedido ahora
    no es lo mismo. Sin escribir nada."""
    previa = {"tipo": "otra"}
    if len(filas) == 2 and all(f["movement_type"] == "adjustment" for f in filas):
        neg, pos = filas  # en orden de escritura: primero la que sale de sin lote
        cantidad = _saldo(_dec(pos["quantity_delta"]))
        es_par = (
            cantidad > 0 and _saldo(_dec(neg["quantity_delta"])) == -cantidad
            and neg["item_id"] == pos["item_id"] and neg["location_id"] == pos["location_id"]
            and neg["variant_id"] == pos["variant_id"]
            and _lote_de_fila(neg["lot_code"]) is None and _vence_de_fila(neg["expires_at"]) is None
        )
        if es_par:
            previa = {"tipo": "asignar", "producto_id": pos["item_id"], "deposito_id": pos["location_id"],
                      "variante_id": pos["variant_id"], "lote": _lote_de_fila(pos["lot_code"]),
                      "vence": _vence_de_fila(pos["expires_at"]), "cantidad": cantidad}
    _misma_operacion(previa, pedida)
    nota = filas[0]["note"]
    sufijo = f": lote {previa['lote']}, vence {previa['vence']} {_SALE_DE_SIN_LOTE} {_marca_de_operacion(clave)}"
    saldo = _saldo_del_bucket(conn, previa["producto_id"], previa["deposito_id"], previa["variante_id"], None, None,
                              hasta_id=filas[0]["id"])
    return {"producto_id": previa["producto_id"], "deposito_id": previa["deposito_id"],
            "variante_id": previa["variante_id"], "lote": previa["lote"], "vence": previa["vence"],
            "cantidad": _num(previa["cantidad"]), "referencia": nota[:-len(sufijo)] if nota.endswith(sufijo) else nota,
            "saldo_sin_lote": _num(saldo), "repetida": True}


def dar_de_baja_lote(conn, item_id: int, deposito_id: int, lot_code: str | None, expires_at, cantidad, *,
                     clave_operacion: str, variante_id: int | None = None, motivo: str = "Vencimiento",
                     usuario_id: int | None = None, nota: str = "", fecha: str = "") -> dict:
    """La merma de `cantidad` unidades de **un lote concreto** de un depósito: un movimiento `merma` negativo con el
    lote y el vencimiento del bucket, sin dejar su saldo negativo. La referencia sigue la convención de las mermas
    del motor: `Merma: <motivo>`, más el lote y la `nota`. **No hay un `reason_code` propio** `vencimiento`: el
    ledger guarda el tipo (`merma`, `movement_type='waste'`) y el motivo va en la referencia, como en el resto de
    las mermas (agregar un tipo cambiaría `erp.stock.TIPOS`, que listan las pantallas).

    **Idempotente por `clave_operacion`**, igual que `asignar_vencimiento_a_saldo` (obligatoria; `[op:<clave>]` al
    final de la nota del movimiento): un reintento con la misma clave y los mismos parámetros no descuenta otra vez y
    devuelve el resultado de la primera con `repetida: True`; con otros parámetros, `ClaveDeOperacionReusada` (409).

    `lot_code` y `expires_at` son los del bucket tal como los devuelve `lotes_de` (al menos uno tiene que venir: la
    merma del stock sin lote es un ajuste común). `ValueError` con datos inválidos o un depósito inexistente;
    `ProductoNoEncontrado`; `SaldoInsuficiente` si el lote no tiene `cantidad` en ese depósito y variante (un lote
    que no existe tiene saldo 0) **o si el stock total del producto ahí (lotes y sin lote) no la alcanza**;
    `ReglaDeNegocio` si hay **saldo «sin lote» negativo** en ese depósito y variante (salidas sin conciliar: hasta A-4 las
    ventas no bajan el lote, su saldo puede estar sobreestimado y mermarlo descontaría dos veces lo vendido). Esas
    comprobaciones van después de buscar la clave: un reintento (`repetida`) no se ve afectado por ellas. No exige que el producto esté marcado. Devuelve `{producto_id, deposito_id,
    variante_id, lote, vence, cantidad, saldo_restante, repetida}`."""
    clave = _normalizar_clave(clave_operacion)
    lote = normalizar_lote(lot_code) if lot_code is not None else None
    vence = normalizar_vencimiento(expires_at) if expires_at is not None else None
    if lote is None and vence is None:
        raise ValueError("hace falta el lote o el vencimiento: la merma del stock sin lote es un ajuste común")
    cant = _cantidad_positiva(cantidad)
    motivo = (motivo or "").strip() or "Vencimiento"
    _validar_variante(conn, item_id, variante_id)
    _preparar_escritura(conn, item_id, deposito_id)
    previas = _movimientos_de_la_operacion(conn, item_id, clave)
    if previas:
        return _baja_repetida(conn, previas, {
            "tipo": "merma", "producto_id": item_id, "deposito_id": deposito_id, "variante_id": variante_id,
            "lote": lote, "vence": vence, "cantidad": _saldo(cant)})
    _fila_de_producto(conn, item_id)
    # 🔴 Parche hasta A-4 (ver ADR-018, nota del 2026-09-30): las ventas todavía descuentan del bucket «sin lote», no del
    # lote, así que el saldo de un lote puede estar sobreestimado. Por eso, además del saldo del lote, se mira el del
    # producto en ese depósito y variante. Las tres comprobaciones corren DESPUÉS de buscar la clave (un reintento de una
    # baja ya hecha devuelve lo anterior aunque el estado haya cambiado) y con el producto tomado.
    saldos = {k[3:]: v for k, v in _saldos(conn, "sm.item_id = ? AND sm.location_id = ?", [item_id, deposito_id],
                                          {deposito_id: {}}).items() if k[2] == variante_id}
    saldo = saldos.get((lote, vence), _CERO)
    if saldo < cant:
        raise SaldoInsuficiente(
            f"el lote {lote or '(sin código)'} (vence {vence or 'sin fecha'}) tiene {_texto_del_saldo(max(saldo, _CERO))} "
            f"en el depósito {deposito_id} y se quieren dar de baja {_texto_del_saldo(cant)}"
        )
    if saldos.get((None, None), _CERO) < 0:
        raise ReglaDeNegocio(
            "hay salidas sin lote sin conciliar en este depósito: el saldo del lote puede estar sobreestimado; "
            "conciliá con el conteo físico antes de dar de baja"
        )
    total = sum(saldos.values(), _CERO)
    if total < cant:
        raise SaldoInsuficiente(
            f"el stock total del producto en el depósito {deposito_id} es {_texto_del_saldo(max(total, _CERO))} "
            f"y se quieren dar de baja {_texto_del_saldo(cant)}"
        )
    referencia = f"Merma: {motivo} — lote {lote or '(sin código)'}, vence {vence or 'sin fecha'}"
    if nota.strip():
        referencia += f" — {nota.strip()}"
    referencia = f"{referencia} {_marca_de_operacion(clave)}"
    add_movimiento_stock(conn, item_id, "merma", -float(cant), referencia, usuario_id=usuario_id,
                         fecha=fecha or hoy_argentina().isoformat(), deposito_id=deposito_id,
                         variant_id=variante_id, lot_code=lote, expires_at=vence)
    return {"producto_id": item_id, "deposito_id": deposito_id, "variante_id": variante_id, "lote": lote,
            "vence": vence, "cantidad": _num(cant), "saldo_restante": _num(saldo - cant),
            "repetida": False}


def _baja_repetida(conn, filas: list, pedida: dict) -> dict:
    """El resultado de una baja que ya se escribió con esta clave, o `ClaveDeOperacionReusada` si lo pedido ahora no
    es lo mismo. Sin escribir nada."""
    previa = {"tipo": "otra"}
    if len(filas) == 1 and filas[0]["movement_type"] == "waste" and _dec(filas[0]["quantity_delta"]) < 0:
        f = filas[0]
        previa = {"tipo": "merma", "producto_id": f["item_id"], "deposito_id": f["location_id"],
                  "variante_id": f["variant_id"], "lote": _lote_de_fila(f["lot_code"]),
                  "vence": _vence_de_fila(f["expires_at"]), "cantidad": _saldo(-_dec(f["quantity_delta"]))}
    _misma_operacion(previa, pedida)
    saldo = _saldo_del_bucket(conn, previa["producto_id"], previa["deposito_id"], previa["variante_id"],
                              previa["lote"], previa["vence"], hasta_id=filas[0]["id"])
    return {"producto_id": previa["producto_id"], "deposito_id": previa["deposito_id"],
            "variante_id": previa["variante_id"], "lote": previa["lote"], "vence": previa["vence"],
            "cantidad": _num(previa["cantidad"]), "saldo_restante": _num(saldo), "repetida": True}
