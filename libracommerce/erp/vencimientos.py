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
from .stock import add_movimiento_stock, normalizar_lote, normalizar_vencimiento

DIAS_AVISO = 15
#: Tope de la anticipación del aviso: un año.
MAX_DIAS_AVISO = 365

ESTADOS = ("vencido", "por_vencer")

#: Decimales con que se limpia el ruido de `float` de una suma (mismo criterio que `erp.reposicion`).
_ESCALA_RUIDO = 10
_CERO = Decimal("0")


class VencimientosError(Exception):
    """Base de los errores de negocio de este módulo (los de entrada inválida son `ValueError`)."""


class ProductoNoEncontrado(VencimientosError, LookupError):
    """El producto no existe: el router lo traduce a 404."""


class ReglaDeNegocio(VencimientosError):
    """La operación es válida pero el estado de los datos no la permite: el router la traduce a 409."""


class SaldoInsuficiente(ReglaDeNegocio):
    """No hay saldo suficiente en el bucket del que se quiere sacar."""


class SinRevision(VencimientosError):
    """La base no tiene la revisión `0002_vencimientos_lotes`: el router lo traduce a 503."""


def hoy_argentina(ahora: datetime.datetime | None = None) -> datetime.date:
    """La fecha de hoy en Argentina. `ahora` (para las pruebas) es un instante con zona; sin él, el reloj real."""
    ahora = ahora or datetime.datetime.now(datetime.UTC)
    return ahora.astimezone(_ZONA_LOCAL).date()


# ── Utilidades ───────────────────────────────────────────────────────────


def _dec(valor) -> Decimal:
    return Decimal(str(valor)) if valor is not None else _CERO


def _saldo(valor) -> Decimal:
    """Un saldo como `Decimal` sin el ruido de la suma en `float` (`0.1 + 0.2`): a 10 decimales."""
    return round(_dec(valor), _ESCALA_RUIDO)


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


def _lote_de_fila(valor) -> str | None:
    """El lote guardado, normalizado: un texto vacío o de espacios es «sin código»."""
    texto = str(valor).strip() if valor is not None else ""
    return texto or None


def _vence_de_fila(valor) -> str | None:
    """El vencimiento guardado como `'AAAA-MM-DD'`, sea cual sea la forma en que se escribió. Uno que no se puede
    leer cuenta como sin vencimiento (el ledger lo escriben varios caminos; un reporte no debe caerse por uno)."""
    if valor is None:
        return None
    try:
        return normalizar_vencimiento(str(valor))
    except ValueError:
        return None


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


def _saldos(conn, sql_items: str, params: list, depositos: dict[int, dict]) -> dict[tuple, Decimal]:
    """`{(item, depósito, variante, lote, vence): saldo}` de los movimientos de `sql_items` (un `WHERE` sobre
    `sm`), en los depósitos dados, con el lote y el vencimiento normalizados. Los saldos en cero se descartan."""
    filas = conn.execute(
        f"""SELECT sm.item_id, sm.location_id, sm.variant_id, sm.lot_code, sm.expires_at,
                   SUM(sm.quantity_delta) AS saldo
            FROM stock_movements sm
            WHERE {sql_items}
            GROUP BY sm.item_id, sm.location_id, sm.variant_id, sm.lot_code, sm.expires_at""",
        params,
    ).fetchall()
    saldos: dict[tuple, Decimal] = {}
    for f in filas:
        if f["location_id"] not in depositos:
            continue
        clave = (f["item_id"], f["location_id"], f["variant_id"], _lote_de_fila(f["lot_code"]),
                 _vence_de_fila(f["expires_at"]))
        saldos[clave] = saldos.get(clave, _CERO) + _saldo(f["saldo"])
    return {k: _saldo(v) for k, v in saldos.items() if _saldo(v) != 0}


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
             hoy: datetime.date | None = None) -> list[dict]:
    """Las existencias de un producto por lote: una fila por `(depósito, variante, lote, vencimiento)` con saldo ≠ 0,
    incluido el bucket **sin lote** (`lote` y `vence` en `None`) y los saldos negativos. Ordenadas por depósito,
    variante, vencimiento (los sin fecha al final) y lote.

    Cada fila: `producto_id`, `deposito_id`, `deposito`, `deposito_activo`, `sucursal_id`, `sucursal`, `variante_id`,
    `variante`, `lote`, `vence` (`'AAAA-MM-DD'` o `None`), `dias_para_vencer` (negativo = vencido, `None` sin fecha),
    `saldo`, `sin_lote` y `estado` (`vencido`, `vigente` o `sin_fecha`). La suma de los saldos de todos los depósitos
    es `erp.stock.get_stock_actual`. No requiere que el producto esté marcado. `ProductoNoEncontrado` si no existe;
    `ValueError` con una sucursal o un depósito que no existen."""
    if not conn.execute("SELECT 1 FROM catalog_items WHERE id = ?", (item_id,)).fetchall():
        raise ProductoNoEncontrado(f"el producto {item_id} no existe")
    hoy = hoy or hoy_argentina()
    depositos = _sucursal_y_depositos(conn, sucursal_id, deposito_id)
    saldos = _saldos(conn, "sm.item_id = ?", [item_id], depositos)
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

    `sin_lote`: los productos marcados cuyo saldo **sin lote** (sin código ni fecha) es ≠ 0 en lo que se mira, porque
    ese stock no tiene fecha y el reporte no lo puede avisar. `situacion='sin_fecha'` si el saldo es positivo (hay que
    asignarle vencimiento); `situacion='salidas_sin_lote'` si es negativo: 🔴 son salidas (ventas, ajustes...) que hasta
    A-4 no bajan ningún lote, así que **los saldos de sus lotes están sobreestimados** (ver el docstring del módulo).

    `resumen`: `lotes_por_vencer`, `lotes_vencidos`, `unidades_por_vencer`, `unidades_vencidas` (suma de cantidades,
    cada una en la unidad de su producto), `productos` (los distintos con lotes en la lista), `productos_sin_lote` y
    `productos_con_salidas_sin_lote`. No incluye lotes sin vencimiento (no hay qué avisar).

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
    sin_lote: dict[int, Decimal] = {}
    for (item, dep, variante, lote, vence), saldo in saldos.items():
        if item not in productos:
            continue
        if lote is None and vence is None:
            sin_lote[item] = sin_lote.get(item, _CERO) + saldo
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

    filas_sin_lote = [
        {"producto_id": item, "codigo": productos[item]["codigo"], "nombre": productos[item]["nombre"],
         "unidad": productos[item]["unidad"], "categoria": productos[item]["categoria"], "saldo": _num(saldo),
         "situacion": "sin_fecha" if saldo > 0 else "salidas_sin_lote"}
        for item, saldo in sin_lote.items() if item in productos and saldo != 0
    ]
    filas_sin_lote.sort(key=lambda r: (r["situacion"] != "salidas_sin_lote", r["nombre"].casefold(), r["producto_id"]))

    vencidos = [r for r in lotes if r["estado"] == "vencido"]
    por_vencer = [r for r in lotes if r["estado"] == "por_vencer"]
    resumen = {
        "lotes_por_vencer": len(por_vencer), "lotes_vencidos": len(vencidos),
        "unidades_por_vencer": _num(sum((_dec(r["saldo"]) for r in por_vencer), _CERO)),
        "unidades_vencidas": _num(sum((_dec(r["saldo"]) for r in vencidos), _CERO)),
        "productos": len({r["producto_id"] for r in lotes}),
        "productos_sin_lote": sum(1 for r in filas_sin_lote if r["situacion"] == "sin_fecha"),
        "productos_con_salidas_sin_lote": sum(1 for r in filas_sin_lote if r["situacion"] == "salidas_sin_lote"),
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
                      vence: str | None) -> Decimal:
    depositos = {deposito_id: {}}
    saldos = _saldos(conn, "sm.item_id = ? AND sm.location_id = ?", [item_id, deposito_id], depositos)
    return sum((s for (_, _, v, lo, ve), s in saldos.items() if v == variante_id and lo == lote and ve == vence),
               _CERO)


def _texto_del_saldo(valor: Decimal) -> str:
    """Una cantidad para un mensaje: sin ceros de más ni notación científica (`50`, `0.5`)."""
    return format(valor.normalize(), "f")


def asignar_vencimiento_a_saldo(conn, item_id: int, deposito_id: int, lot_code: str, expires_at, cantidad, *,
                                variante_id: int | None = None, usuario_id: int | None = None, nota: str = "",
                                fecha: str = "") -> dict:
    """Le pone lote y vencimiento a `cantidad` unidades del saldo **sin lote** de un producto marcado en un depósito
    (el stock que había antes de usar lotes, o el que entró por una vía que no los pide).

    Escribe un **par aditivo** de movimientos de ajuste con la misma referencia (`nota` más un identificador
    `[asignación xxxxxxxx]`, que las une) y la misma fecha: uno **negativo sin lote** y uno **positivo con el lote y
    el vencimiento**. Nunca modifica una fila ya escrita y el stock total del producto en ese depósito no cambia.
    Si `fecha` no viene es hoy en Argentina.

    `ValueError` con datos inválidos (lote vacío, fecha ilegible, cantidad ≤ 0, depósito inexistente);
    `ProductoNoEncontrado`; `ReglaDeNegocio` si el producto no está marcado con `marcar_vence`; `SaldoInsuficiente` si
    el saldo sin lote de ese depósito y variante es menor a `cantidad`. Todo se valida antes de escribir la primera
    fila. Devuelve `{producto_id, deposito_id, variante_id, lote, vence, cantidad, referencia, saldo_sin_lote}` (el
    saldo que queda sin lote)."""
    lote = normalizar_lote(lot_code)
    vence = normalizar_vencimiento(expires_at)
    cant = _cantidad_positiva(cantidad)
    _preparar_escritura(conn, item_id, deposito_id)
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
    destino = {"usuario_id": usuario_id, "fecha": fecha, "deposito_id": deposito_id, "variant_id": variante_id}
    add_movimiento_stock(conn, item_id, "ajuste", -float(cant), f"{detalle} (sale de sin lote)", **destino)
    add_movimiento_stock(conn, item_id, "ajuste", float(cant), f"{detalle} (entra al lote)",
                         lot_code=lote, expires_at=vence, **destino)
    return {"producto_id": item_id, "deposito_id": deposito_id, "variante_id": variante_id, "lote": lote,
            "vence": vence, "cantidad": _num(cant), "referencia": referencia,
            "saldo_sin_lote": _num(sin_lote - cant)}


def dar_de_baja_lote(conn, item_id: int, deposito_id: int, lot_code: str | None, expires_at, cantidad, *,
                     variante_id: int | None = None, motivo: str = "Vencimiento", usuario_id: int | None = None,
                     nota: str = "", fecha: str = "") -> dict:
    """La merma de `cantidad` unidades de **un lote concreto** de un depósito: un movimiento `merma` negativo con el
    lote y el vencimiento del bucket, sin dejar su saldo negativo. La referencia sigue la convención de las mermas
    del motor: `Merma: <motivo>`, más el lote y la `nota`. **No hay un `reason_code` propio** `vencimiento`: el
    ledger guarda el tipo (`merma`, `movement_type='waste'`) y el motivo va en la referencia, como en el resto de
    las mermas (agregar un tipo cambiaría `erp.stock.TIPOS`, que listan las pantallas).

    `lot_code` y `expires_at` son los del bucket tal como los devuelve `lotes_de` (al menos uno tiene que venir: la
    merma del stock sin lote es un ajuste común). `ValueError` con datos inválidos o un depósito inexistente;
    `ProductoNoEncontrado`; `SaldoInsuficiente` si el lote no tiene `cantidad` en ese depósito y variante (un lote
    que no existe tiene saldo 0). No exige que el producto esté marcado. Devuelve `{producto_id, deposito_id,
    variante_id, lote, vence, cantidad, saldo_restante}`."""
    lote = normalizar_lote(lot_code) if lot_code is not None else None
    vence = normalizar_vencimiento(expires_at) if expires_at is not None else None
    if lote is None and vence is None:
        raise ValueError("hace falta el lote o el vencimiento: la merma del stock sin lote es un ajuste común")
    cant = _cantidad_positiva(cantidad)
    motivo = (motivo or "").strip() or "Vencimiento"
    _preparar_escritura(conn, item_id, deposito_id)
    _fila_de_producto(conn, item_id)
    saldo = _saldo_del_bucket(conn, item_id, deposito_id, variante_id, lote, vence)
    if saldo < cant:
        raise SaldoInsuficiente(
            f"el lote {lote or '(sin código)'} (vence {vence or 'sin fecha'}) tiene {_texto_del_saldo(max(saldo, _CERO))} "
            f"en el depósito {deposito_id} y se quieren dar de baja {_texto_del_saldo(cant)}"
        )
    referencia = f"Merma: {motivo} — lote {lote or '(sin código)'}, vence {vence or 'sin fecha'}"
    if nota.strip():
        referencia += f" — {nota.strip()}"
    add_movimiento_stock(conn, item_id, "merma", -float(cant), referencia, usuario_id=usuario_id,
                         fecha=fecha or hoy_argentina().isoformat(), deposito_id=deposito_id,
                         variant_id=variante_id, lot_code=lote, expires_at=vence)
    return {"producto_id": item_id, "deposito_id": deposito_id, "variante_id": variante_id, "lote": lote,
            "vence": vence, "cantidad": _num(cant), "saldo_restante": _num(saldo - cant)}
