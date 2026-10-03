"""Reposición sugerida —«qué pedir»—: sólo lectura (2026-09-30).

Roadmap de producto de VentaLibra, B-1 (ADR-017). Cruza lo que ya hay guardado —lo que se vende
(`erp.margen`), lo que hay (`erp.stock`), lo que ya se pidió (`erp.compras`) y el `min_stock` del catálogo— y
devuelve, por producto, cuánto conviene pedir. **Sólo sugiere**: no genera la orden de compra, no escribe nada
ni toca el schema, y no depende de ningún plan.

**La fórmula.** Con `N = dias_rotacion` y `H = dias_cobertura + plazo_entrega_dias`:

    necesidad = unidades_vendidas × H / dias_de_muestra          (la rotación diaria × H)
    sugerido  = max(0, ceil(necesidad − stock − en_camino))
    y, si stock + en_camino < min_stock:  sugerido = max(sugerido, min_stock − stock − en_camino)

`unidades_vendidas` son las **netas** de la ventana de `N` días que termina hoy (`hoy − N + 1 .. hoy`), con el
criterio de `erp.margen` —se reusa `margen.unidades_netas`, no se reimplementa—: `confirmed`, `partially_returned` y
`returned`; la anulada y la pendiente de cobro no cuentan, y lo devuelto se resta. La multiplicación va antes de la
división para que un cociente exacto (`10 × 18 / 30`) no se pase de un entero por el redondeo del `Decimal`.
`min_stock` es el piso: si lo que hay más lo que viene no llega, se pide al menos lo que falta para llegar. Se
redondea **hacia arriba** a la unidad del producto: entero si `units.allows_fraction` es 0, y a `decimal_scale`
decimales (3 si la unidad fraccionable no lo trae) si admite fracciones.

**Stock negativo se toma como 0** para la cuenta (el campo `stock` muestra el real): un stock en negativo es casi
siempre inventario que nunca se cargó, y pedir de más para «tapar» ese hueco es peor que pedir de menos;
`posible_quiebre` lo marca.

**Por sucursal.** `sucursal_id` mira los depósitos **activos** de esa sucursal (`locations.branch_id`); sin él, todos
los depósitos activos de la instancia (con o sin sucursal). Las ventas de la sucursal son las que salieron de sus
depósitos: 🔴 `sales.branch_id` **no sirve solo**, `erp.ventas.crear_venta` (`POST /api/ventas`) no lo escribe y toda
venta de mostrador lo trae en NULL; se usa `sales.branch_id` cuando viene y, si no, la sucursal del depósito del ledger
(`margen._sucursal_de_las_ventas`). Una venta sin ninguna de las dos no entra en ninguna sucursal, sí en el total.

**En camino.** Las líneas de las órdenes de compra **abiertas**: cantidad pedida menos recibida, sin bajar de 0 por
línea. Abierta es todo menos `received` y `cancelled`, **`draft` incluido**: este motor no tiene ninguna operación que
pase una orden a `sent` (nace `draft` y sólo `confirmar_recepcion` la mueve, a `partial`/`received`), así que la orden
que ya se le pidió al proveedor está en `draft`. Costo: una orden abandonada en borrador cuenta como pedido. Con
`sucursal_id` cuentan las de esa sucursal **y las que no tienen sucursal** (`purchase_orders.branch_id` es opcional,
y no saber a dónde va no es motivo para pedirlo dos veces); esa parte se informa aparte (`en_camino_sin_sucursal`).
La mercadería se recibe por depósito, así que lo pedido y lo recibido se descuentan sin mirar a cuál.

**El sesgo por quiebres, y lo que se hace.** Un producto sin stock no vende, y dividir por `N` subestima su
rotación. La variante barata: del ledger se arma el saldo día por día de los depósitos que se miran, y un día en que
el saldo **nunca fue positivo y no hubo ninguna venta** no cuenta para la rotación (`dias_con_stock = N − esos días`).
Un producto nuevo (sin movimientos antes de tal día) queda cubierto por lo mismo. Un día con ventas nunca se
excluye: si vendió, es que había, y así un producto cuyo inventario nunca se cargó (saldo negativo pero vende todos
los días) no se sobrestima. Límites: es por día (dos movimientos del mismo día que suben y bajan el saldo no se
distinguen), depende de que el ledger esté bien cargado, y con menos de `7` días de muestra (o `N`, si es menor) se
divide por 7 para que un solo día con stock no dispare la rotación. `posible_quiebre` es «stock ≤ 0 o algún día sin
stock en la ventana»: la rotación de ese producto es una estimación y quien pide debe mirarla.

**Lo vencido no se cuenta como stock (v2, 2026-10-01).** Para un producto marcado como perecedero
(`catalog_items.tracks_expiry`), la mercadería de un lote con `vence < hoy` no se puede ofrecer: se descuenta del
stock disponible para la cuenta, así que el producto se pide aunque la góndola esté «llena» de lo vencido. El campo
`stock` sigue mostrando el real y `vencido` dice cuánto se descontó. Sólo cuentan los lotes con saldo positivo de los
depósitos que se miran; el saldo «sin lote» (sin fecha) no se descuenta. `descontar_vencido=False` vuelve a la cuenta
de la v1. Un producto sin marcar, o una base sin la revisión `0002`, no cambia en nada.

**Plazo y techo propios del producto (v2, ADR-020).** `catalog_items.lead_time_days` es el plazo de entrega de ese
producto y reemplaza al `plazo_entrega_dias` general en SU horizonte (`dias_cobertura + plazo`); `catalog_items.max_stock`
es su techo: lo sugerido nunca hace pasar `disponible + sugerido` de ese valor (se redondea hacia abajo a la unidad). El
techo manda sobre el piso del mínimo (un mínimo mayor que el techo es una configuración que `fijar_parametros` no deja
guardar, pero si ya existe gana el techo). Los dos son opcionales: sin valor, la cuenta es la de siempre; una base sin
la revisión `0003` también. `plazo_entrega_dias` en cada fila es el que se usó y `plazo_propio` dice si era el del
producto; `limitado_por_maximo` marca la fila a la que el techo le recortó la sugerencia.

**Proveedor habitual (v2, ADR-021).** `catalog_items.supplier_party_id` es el proveedor al que se le suele pedir el producto
(opcional). Cada fila trae `proveedor_id` y `proveedor` (el nombre; `None` sin proveedor) y `proveedor_id` filtra la lista a los
productos de ese proveedor: es lo que prepara la orden de compra en borrador. Una base sin la revisión `0004` no tiene
proveedores y la consulta es la de siempre.

**Estacionalidad (v2, ADR-023, opt-in con `estacionalidad=True`).** La rotación de los últimos `N` días supone que lo que viene se parece a lo reciente; en un producto
estacional no es así (el helado en octubre, el pan dulce en diciembre). Con `estacionalidad` el motor mira **lo que pasó hace un año** (la misma fecha, un año atrás) y
compara dos ventanas de ese año: la **de referencia** (los `N` días que terminaban entonces, equivalente a la ventana de ahora) y la **proyectada** (los `H` días
que venían después, el mismo horizonte que se está cubriendo). El `factor_estacional` es la razón de las rotaciones diarias, proyectada / de referencia, acotado entre
`0.25` y `4`, y multiplica la necesidad: `necesidad = unidades × H / dias_de_muestra × factor`. Un factor de 2 dice «hace un año, después de una ventana como la de ahora,
se vendió el doble por día». **Sin factor (`None`, sin ajuste) si no hay con qué**: la ventana de referencia del año pasado tiene menos de 3 días con venta (una instancia
con menos de un año de historia, o un producto que entonces no se vendía). Es un ajuste de la proyección, no de lo que hay ni del mínimo. Límites: confía en un solo año
(un año atípico se hereda), no corrige quiebres de entonces, y no inventa temporada para un producto sin rotación reciente (necesidad cero sigue en cero).

**Mínimo por sucursal (v2, ADR-024).** `catalog_items.min_stock` es el mínimo global del producto; la tabla `item_branch_min_stock` (revisión `0005`) permite un mínimo propio por
sucursal. Con `sucursal_id`, el piso del producto es el de esa sucursal si tiene fila y, si no, el global (un producto sin fila se comporta como siempre). **Sin `sucursal_id` (toda la
instancia) el piso es siempre el global**: los mínimos por sucursal no se suman ni se promedian, porque hablan del stock de cada sucursal y no del total. `0` sigue siendo «no me avises»
(también como mínimo propio: una sucursal puede apagar el aviso de un producto que el global sí vigila). Cada fila trae `stock_minimo` ya resuelto y `stock_minimo_propio` (`True` si viene de
la sucursal). Una base sin la revisión `0005` no tiene mínimos por sucursal y la consulta es la de siempre.

**Lo que vence dentro del horizonte (v2, ADR-025, opt-in con `descontar_por_vencer=True`).** Descontar sólo lo YA vencido deja pasar el lote que está por vencer y que no se va a vender
a tiempo: el producto figura cubierto y a los pocos días se tira. Con `descontar_por_vencer`, para un producto marcado (`tracks_expiry = 1`) se estima cuánto de lo que **todavía no venció**
pero vence dentro del horizonte `H = dias_cobertura + plazo` no llega a venderse antes de vencer, y eso (`por_vencer`, aparte de `stock` y de `vencido`) también se resta del stock utilizable:
`utilizable = max(stock − vencido − por_vencer, 0)`. La cuenta: la rotación diaria proyectada es `r = proyectado / H` (la proyección ya lleva el factor estacional si está prendido) y los
lotes se venden por orden de vencimiento (FEFO). Para cada lote `j` que vence dentro del horizonte, `d_j = (vence_j − hoy).días + 1` (el día del vencimiento todavía se vende y hoy cuenta) y `C_j` es el
saldo acumulado de los lotes no vencidos hasta `j` inclusive (orden por vencimiento); al vencer `j` ya se vendieron, a lo sumo, `r × d_j` unidades, así que sobran `C_j − r × d_j`. La pérdida es
`max(0, máx_j (C_j − r × d_j))` sobre los `j` con `d_j <= H`: el **máximo** del acumulado y no la suma (lo que se pierde de un lote se vendería, si no, del siguiente), y por construcción nunca pasa de la suma de los
saldos de esos lotes. Sólo cuentan los saldos positivos de los depósitos que se miran; el saldo «sin lote» (sin fecha) no cuenta, y un lote con `vence < hoy` ya va en `vencido` y no se duplica acá (con
`descontar_vencido=False` tampoco entra en `por_vencer`). La aritmética es de racionales exactos (`Fraction`): un resto decimal no pide una unidad de más ni de menos, y `sugerido`, el mínimo y el techo se calculan con
la pérdida exacta; `por_vencer` se informa redondeado hacia arriba a la escala del informe. **Borde:** un producto **sin ventas** (`r = 0`) pierde completo lo que vence dentro del horizonte; si además tiene un mínimo > 0,
se sugiere reponerlo (es lo que dice la cuenta: lo que va a vencer sin venderse no sirve de colchón). Límites: supone que la rotación de la ventana se mantiene todo el horizonte, que el stock se vende por FEFO y
que lo «sin lote» no compite con los lotes. Apagado por default porque cambia números que hoy se ven; sin la opción, `por_vencer` es 0 y la cuenta es la de siempre. Un producto sin marcar, o una base sin la revisión `0002`, no cambia en nada.

Agrupa por producto, no por variante (`variantes` dice cuántas activas tiene). Sólo productos activos, de tipo
`product` y `purchasable`; se agrega en Python con `Decimal` y las consultas son las mismas en SQLite y PostgreSQL.
"""

from __future__ import annotations

import datetime
import sqlite3
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from fractions import Fraction

from . import margen
from .lotes import saldos_por_bucket
from .stock import get_stock_por_deposito
from .vencimientos import tiene_revision

DIAS_ROTACION = 30
DIAS_COBERTURA = 15
PLAZO_ENTREGA_DIAS = 3
#: Topes de los tres parámetros: un año de historia, y no se pide para más de un año ni con un plazo de medio año.
MAX_DIAS_ROTACION = 365
MAX_DIAS_COBERTURA = 365
MAX_PLAZO_ENTREGA_DIAS = 180
#: Tope del stock mínimo por sucursal (ADR-024): mil millones de unidades. No es una regla de negocio sino un seguro contra un `1e400` que SQLite
#: guardaría como infinito; el techo de reposición (`max_stock`) tampoco tiene otro límite que ser finito.
MAX_STOCK_MINIMO = Decimal(10) ** 9

#: Estacionalidad (ADR-023): el factor se acota entre estos dos valores (un producto no se pide a menos de un cuarto ni a más de cuatro veces de lo que
#: dice la rotación reciente por lo que pasó hace un año) y exige este mínimo de días con venta en la ventana de referencia del año pasado.
FACTOR_ESTACIONAL_MIN = Fraction(1, 4)
FACTOR_ESTACIONAL_MAX = Fraction(4)
_MIN_DIAS_CON_VENTA_ESTACIONAL = 3

MOTIVOS = ("bajo_minimo", "por_rotacion", "ambos")

#: Las órdenes que todavía no llegaron: todo menos `received` y `cancelled` (ver "En camino" en el docstring).
_ESTADOS_EN_CAMINO = ("draft", "sent", "partial")
#: Con menos días de muestra que estos, se divide por estos (ver "El sesgo por quiebres").
_MIN_DIAS_DE_MUESTRA = 7
#: Decimales de una unidad que admite fracciones y no declara `decimal_scale`.
_ESCALA_FRACCION = 3
#: Decimales con que se limpia el ruido de `float` de un saldo (ver `_stock`).
_ESCALA_RUIDO = 10
#: Mínimo de decimales con que se informa una cantidad de una unidad entera o de escala baja.
_ESCALA_MINIMA_DE_INFORME = 3

_CERO = Decimal("0")


def _dec(valor) -> Decimal:
    return Decimal(str(valor)) if valor is not None else _CERO


def _stock(valor) -> Decimal:
    """Un saldo o un movimiento como `Decimal`, sin el ruido de la suma en `float` (`0.1 + 0.2`). Se redondea a 10
    decimales, mucho más fino que cualquier `decimal_scale`: sólo saca el ruido, no toca la precisión guardada."""
    return round(_dec(valor), _ESCALA_RUIDO)


def _techo(valor: Decimal | Fraction, escala: int) -> Decimal:
    """`valor` redondeado hacia arriba a `escala` decimales. Un `Fraction` se redondea con enteros, sin pasar por un cociente de `Decimal`."""
    paso = Decimal(10) ** escala
    if isinstance(valor, Fraction):
        return Decimal(-((-valor.numerator * 10 ** escala) // valor.denominator)) / paso
    return (valor * paso).to_integral_value(rounding=ROUND_CEILING) / paso


def _piso(valor: Decimal | Fraction, escala: int) -> Decimal:
    """`valor` redondeado hacia abajo a `escala` decimales (un `Fraction`, como en `_techo`)."""
    paso = Decimal(10) ** escala
    if isinstance(valor, Fraction):
        return Decimal((valor.numerator * 10 ** escala) // valor.denominator) / paso
    return (valor * paso).to_integral_value(rounding=ROUND_FLOOR) / paso


def _decimal(valor: Fraction) -> Decimal:
    """Un `Fraction` como `Decimal` (un cociente: sólo para mostrar, no para redondear una cantidad a pedir)."""
    return Decimal(valor.numerator) / Decimal(valor.denominator)


def tiene_parametros(conn) -> bool:
    """Si la base tiene la revisión `0003_parametros_reposicion` (las columnas `lead_time_days` y `max_stock`). Se sondea
    por metadatos y no con un `SELECT` de la columna: en PostgreSQL un `SELECT` fallido aborta la transacción."""
    return {"lead_time_days", "max_stock"} <= {f[1] for f in conn.execute("PRAGMA table_info(catalog_items)").fetchall()}


def tiene_proveedor(conn) -> bool:
    """Si la base tiene la revisión `0004_proveedor_por_producto` (la columna `supplier_party_id`)."""
    return "supplier_party_id" in {f[1] for f in conn.execute("PRAGMA table_info(catalog_items)").fetchall()}


def tiene_minimos_sucursal(conn) -> bool:
    """Si la base tiene la revisión `0005_min_stock_por_sucursal` (la tabla `item_branch_min_stock`). Por metadatos, por la misma razón que `tiene_parametros`."""
    return bool(conn.execute("PRAGMA table_info(item_branch_min_stock)").fetchall())


def _minimos_de_la_sucursal(conn, sucursal_id: int | None) -> dict[int, Decimal]:
    """`{producto_id: mínimo propio}` de la sucursal. Vacío sin sucursal (toda la instancia usa el global) o sin la revisión `0005`. Ver "Mínimo por sucursal" en el módulo."""
    if sucursal_id is None or not tiene_minimos_sucursal(conn):
        return {}
    return {f["item_id"]: _dec(f["min_stock"])
            for f in conn.execute("SELECT item_id, min_stock FROM item_branch_min_stock WHERE branch_id = ?", (sucursal_id,)).fetchall()}


def _cantidad(valor: Decimal, escala: int):
    """Un `int` para lo que se pide de una unidad entera (`escala` 0), un `float` redondeado para el resto."""
    return int(valor) if escala == 0 else float(round(valor, escala))


def _entero_en_rango(nombre: str, valor, maximo: int) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or not 1 <= valor <= maximo:
        raise ValueError(f"{nombre} tiene que ser un entero entre 1 y {maximo}: {valor!r}")
    return valor


def _productos(conn, categoria: str | None, producto_id: int | None, proveedor_id: int | None = None) -> list:
    donde = ["ci.active = 1", "ci.item_type = 'product'", "ci.purchasable = 1"]
    params: list = []
    if producto_id is not None:
        donde.append("ci.id = ?")
        params.append(producto_id)
    propios = "ci.lead_time_days, ci.max_stock," if tiene_parametros(conn) else "NULL AS lead_time_days, NULL AS max_stock,"
    if tiene_proveedor(conn):
        propios += " ci.supplier_party_id AS proveedor_id, sp.display_name AS proveedor,"
        union = "LEFT JOIN parties sp ON sp.id = ci.supplier_party_id"
        if proveedor_id is not None:
            donde.append("ci.supplier_party_id = ?")
            params.append(proveedor_id)
    else:
        propios += " NULL AS proveedor_id, NULL AS proveedor,"
        union = ""
    filas = conn.execute(
        f"""SELECT ci.id, ci.name, ci.unit_code, ci.min_stock, {propios} COALESCE(cat.name, '') AS categoria,
                   ic.code AS codigo, u.allows_fraction, u.decimal_scale
            FROM catalog_items ci
            LEFT JOIN categories cat ON cat.id = ci.category_id
            LEFT JOIN item_codes ic ON ic.item_id = ci.id AND ic.is_primary = 1
            LEFT JOIN units u ON u.code = ci.unit_code
            {union}
            WHERE {' AND '.join(donde)}
            ORDER BY ci.name, ci.id""",
        params,
    ).fetchall()
    if categoria:
        # Por nombre, sin distinguir mayúsculas; la categoría directa del producto, no la de sus padres.
        filas = [f for f in filas if f["categoria"].casefold() == categoria.strip().casefold()]
    return filas


def _depositos(conn, sucursal_id: int | None) -> set[int]:
    """Los depósitos activos que se miran: los de la sucursal, o todos."""
    sql, params = "SELECT id FROM locations WHERE active = 1", []
    if sucursal_id is not None:
        sql += " AND branch_id = ?"
        params.append(sucursal_id)
    return {f["id"] for f in conn.execute(sql, params).fetchall()}


def _en_camino(conn, sucursal_id: int | None) -> tuple[dict[int, Decimal], dict[int, Decimal]]:
    """`(en_camino, de_ese_en_camino_sin_sucursal)` por producto. Ver "En camino" en el docstring del módulo."""
    filas = conn.execute(
        f"""SELECT poi.item_id, po.branch_id, poi.quantity_ordered, poi.quantity_received
            FROM purchase_order_items poi
            JOIN purchase_orders po ON po.id = poi.purchase_order_id
            WHERE po.status IN ({",".join("?" for _ in _ESTADOS_EN_CAMINO)})""",
        list(_ESTADOS_EN_CAMINO),
    ).fetchall()
    total: dict[int, Decimal] = {}
    sin_sucursal: dict[int, Decimal] = {}
    for f in filas:
        if sucursal_id is not None and f["branch_id"] not in (None, sucursal_id):
            continue
        pendiente = max(_dec(f["quantity_ordered"]) - _dec(f["quantity_received"]), _CERO)
        total[f["item_id"]] = total.get(f["item_id"], _CERO) + pendiente
        if f["branch_id"] is None:
            sin_sucursal[f["item_id"]] = sin_sucursal.get(f["item_id"], _CERO) + pendiente
    return total, sin_sucursal


def _lotes_con_fecha(conn, ids: set[int], depositos: set[int]) -> dict[int, list[tuple[datetime.date, Decimal]]]:
    """Por producto marcado, `[(vence, saldo)]` de los lotes con fecha y saldo positivo en `depositos`, ordenados por vencimiento (el orden de FEFO). Vacío si la base no tiene la
    revisión `0002` o nadie está marcado. Una sola consulta sirve a `_vencido` y a `_por_vencer` (ver "Lo vencido no se cuenta" y "Lo que vence dentro del horizonte" en el módulo)."""
    if not ids or not tiene_revision(conn):
        return {}
    marcados = {f["id"] for f in conn.execute("SELECT id FROM catalog_items WHERE tracks_expiry = 1").fetchall()}
    marcados &= ids
    if not marcados or not depositos:
        return {}
    lotes: dict[int, list[tuple[datetime.date, Decimal]]] = {}
    # Sólo los productos marcados y los depósitos que se miran, ya en el SQL: un reporte de un producto no agrupa el
    # historial de toda la instancia.
    donde = (f"sm.expires_at IS NOT NULL AND sm.item_id IN ({','.join('?' for _ in marcados)}) "
             f"AND sm.location_id IN ({','.join('?' for _ in depositos)})")
    saldos = saldos_por_bucket(conn, donde, [*sorted(marcados), *sorted(depositos)], depositos)
    for (item, _dep, _variante, _lote, vence), saldo in saldos.items():
        if item in marcados and vence is not None and saldo > 0:
            lotes.setdefault(item, []).append((datetime.date.fromisoformat(vence), saldo))
    return {item: sorted(lista) for item, lista in lotes.items()}


def _vencido(lotes: dict[int, list[tuple[datetime.date, Decimal]]], hoy: datetime.date) -> dict[int, Decimal]:
    """Por producto marcado, la suma de los saldos positivos de los lotes ya vencidos (`vence < hoy`). Ver "Lo vencido no se cuenta" en el módulo."""
    vencido = {item: sum((saldo for vence, saldo in lista if vence < hoy), _CERO) for item, lista in lotes.items()}
    return {item: v for item, v in vencido.items() if v}


def _por_vencer(lista: list[tuple[datetime.date, Decimal]], hoy: datetime.date, horizonte: int, rotacion: Fraction) -> Fraction:
    """Lo que, de los lotes `lista` (`[(vence, saldo)]` ordenados por vencimiento), vence dentro del horizonte sin llegar a venderse antes: `max(0, máx_j (C_j − rotacion × d_j))` con
    `d_j = (vence_j − hoy).días + 1` y `C_j` el acumulado hasta `j`, sobre los lotes con `d_j <= horizonte`. Ignora los ya vencidos (`vence < hoy`: van en `_vencido`). Exacto (`Fraction`).
    Ver "Lo que vence dentro del horizonte" en el módulo."""
    perdida = Fraction(0)
    acumulado = Fraction(0)
    for vence, saldo in lista:
        if vence < hoy:
            continue
        dias = (vence - hoy).days + 1
        if dias > horizonte:
            break
        acumulado += Fraction(saldo)
        perdida = max(perdida, acumulado - rotacion * dias)
    return perdida


def _dias_sin_stock(saldo_final: Decimal, movimientos: list[tuple[str, Decimal]], desde: datetime.date,
                    dias: int, dias_con_venta: set[str]) -> int:
    """Los días de la ventana en que el saldo nunca fue positivo y no hubo ninguna venta.

    `movimientos`: `(día 'AAAA-MM-DD', delta)` de la ventana, en orden. El saldo de partida es el actual menos lo
    que se movió en la ventana, así que no hace falta otra consulta por el saldo anterior."""
    por_dia: dict[str, list[Decimal]] = {}
    for dia, delta in movimientos:
        por_dia.setdefault(dia, []).append(delta)
    saldo = saldo_final - sum((d for _, d in movimientos), _CERO)
    sin_stock = 0
    for n in range(dias):
        dia = (desde + datetime.timedelta(days=n)).isoformat()
        mayor = saldo
        for delta in por_dia.get(dia, ()):
            saldo += delta
            mayor = max(mayor, saldo)
        if mayor <= 0 and dia not in dias_con_venta:
            sin_stock += 1
    return sin_stock


def _un_anio_atras(dia: datetime.date) -> datetime.date:
    """La misma fecha un año atrás (el 29 de febrero cae en el 28)."""
    try:
        return dia.replace(year=dia.year - 1)
    except ValueError:
        return dia.replace(year=dia.year - 1, day=28)


def _factores_estacionales(conn, ids: set[int], hoy: datetime.date, dias_rotacion: int, horizontes: dict[int, int],
                           sucursal_id: int | None) -> dict[int, Fraction]:
    """`{producto_id: factor}` sólo de los productos con historia suficiente de hace un año (ver "Estacionalidad" en el docstring del módulo).

    Una sola consulta de ventas cubre las dos ventanas del año pasado; la proyectada de cada producto es `horizontes[id]` días (el suyo: puede tener plazo propio),
    cortada en `hoy` si el horizonte se pasa de un año."""
    ref_fin = _un_anio_atras(hoy)
    ref_ini = ref_fin - datetime.timedelta(days=dias_rotacion - 1)
    hasta = min(ref_fin + datetime.timedelta(days=max(horizontes.values(), default=0)), hoy)
    por_producto: dict[int, list[tuple[datetime.date, Decimal]]] = {}
    for occurred_on, item_id, unidades in margen.unidades_netas(conn, ref_ini.isoformat(), hasta.isoformat(), sucursal_id):
        if item_id in ids and unidades > 0:
            por_producto.setdefault(item_id, []).append((datetime.date.fromisoformat(str(occurred_on)[:10]), unidades))
    factores: dict[int, Fraction] = {}
    for item_id, ventas in por_producto.items():
        referencia = [(d, u) for d, u in ventas if ref_ini <= d <= ref_fin]
        if len({d for d, _ in referencia}) < _MIN_DIAS_CON_VENTA_ESTACIONAL:
            continue
        fin = min(ref_fin + datetime.timedelta(days=horizontes[item_id]), hoy)
        dias_proyectados = (fin - ref_fin).days
        if dias_proyectados < 1:
            continue
        proyectadas = sum((u for d, u in ventas if ref_fin < d <= fin), _CERO)
        # Racionales exactos: un cociente de `Decimal` deja restos (`7.000000000000000000000000001`) que el techo de `_techo` convierte en una unidad de más.
        diaria_referencia = Fraction(sum((u for _, u in referencia), _CERO)) / dias_rotacion
        factor = (Fraction(proyectadas) / dias_proyectados) / diaria_referencia
        factores[item_id] = min(max(factor, FACTOR_ESTACIONAL_MIN), FACTOR_ESTACIONAL_MAX)
    return factores


def sugerencia_reposicion(conn, *, dias_rotacion: int = DIAS_ROTACION, dias_cobertura: int = DIAS_COBERTURA,
                          plazo_entrega_dias: int = PLAZO_ENTREGA_DIAS, sucursal_id: int | None = None,
                          categoria: str | None = None, producto_id: int | None = None,
                          solo_a_pedir: bool = True, descontar_vencido: bool = True, proveedor_id: int | None = None,
                          estacionalidad: bool = False, descontar_por_vencer: bool = False,
                          hoy: datetime.date | None = None) -> list[dict]:
    """Qué pedir, por producto, del más urgente al menos (menor cobertura primero, luego mayor `sugerido`; los
    que no tienen rotación, al final). Cada fila: `producto_id`, `codigo`, `nombre`, `unidad`, `categoria`, `stock`,
    `vencido`, `por_vencer`, `proveedor_id`, `proveedor`, `en_camino`, `en_camino_sin_sucursal`, `stock_minimo`, `stock_minimo_propio`, `unidades_vendidas`, `dias_con_stock`, `rotacion_diaria`,
    `cobertura_dias` (`None` sin rotación), `sugerido`, `motivo` (`bajo_minimo`, `por_rotacion`, `ambos`, o `None`
    si no hay nada que pedir), `sin_ventas`, `posible_quiebre`, `variantes` y `factor_estacional` (`None` si `estacionalidad` está apagada o no hay historia). Ver el docstring del módulo.

    `solo_a_pedir` (el default) deja sólo los de `sugerido > 0`. `descontar_vencido` (el default) resta del stock lo
    que está en lotes vencidos. `stock_minimo` es el piso ya resuelto: con `sucursal_id`, el mínimo propio de esa sucursal si lo tiene (`stock_minimo_propio=True`) y si no el global; sin
    `sucursal_id`, siempre el global (ADR-024). `estacionalidad` (apagada por default) ajusta la proyección por lo que pasó hace un año. `descontar_por_vencer` (apagado por default, ADR-025) resta además del stock utilizable lo que, de los
    lotes que vencen dentro del horizonte, no llega a venderse antes (`por_vencer`; 0 sin la opción; un producto sin ventas pierde completo lo que vence dentro del horizonte, y con un mínimo > 0 se
    sugiere reponerlo). `hoy` es para las pruebas: el default es la fecha
    del servidor. Levanta `ValueError` con un parámetro fuera de rango o una `sucursal_id` que no existe.
    No commitea (no escribe)."""
    _entero_en_rango("dias_rotacion", dias_rotacion, MAX_DIAS_ROTACION)
    _entero_en_rango("dias_cobertura", dias_cobertura, MAX_DIAS_COBERTURA)
    _entero_en_rango("plazo_entrega_dias", plazo_entrega_dias, MAX_PLAZO_ENTREGA_DIAS)
    if sucursal_id is not None and not conn.execute("SELECT 1 FROM branches WHERE id = ?", (sucursal_id,)).fetchall():
        raise ValueError(f"la sucursal {sucursal_id} no existe")
    if proveedor_id is not None and not conn.execute("SELECT 1 FROM parties WHERE id = ?", (proveedor_id,)).fetchall():
        raise ValueError(f"el proveedor {proveedor_id} no existe")

    hoy = hoy or datetime.date.today()
    desde = hoy - datetime.timedelta(days=dias_rotacion - 1)
    muestra_minima = min(dias_rotacion, _MIN_DIAS_DE_MUESTRA)

    productos = _productos(conn, categoria, producto_id, proveedor_id)
    ids = {p["id"] for p in productos}
    depositos = _depositos(conn, sucursal_id)
    minimos_propios = _minimos_de_la_sucursal(conn, sucursal_id)

    saldos: dict[int, Decimal] = {}
    for item_id, por_deposito in get_stock_por_deposito(conn).items():
        if item_id in ids:
            saldos[item_id] = sum((_stock(c) for d, c in por_deposito.items() if d in depositos), _CERO)

    movimientos: dict[int, list[tuple[str, Decimal]]] = {}
    for f in conn.execute(
        "SELECT item_id, location_id, occurred_at, quantity_delta FROM stock_movements "
        "WHERE occurred_at >= ? ORDER BY occurred_at, id", (desde.isoformat(),),
    ).fetchall():
        if f["item_id"] in ids and f["location_id"] in depositos:
            movimientos.setdefault(f["item_id"], []).append((str(f["occurred_at"])[:10], _stock(f["quantity_delta"])))

    vendidas: dict[int, Decimal] = {}
    dias_con_venta: dict[int, set[str]] = {}
    for occurred_on, item_id, unidades in margen.unidades_netas(conn, desde.isoformat(), hoy.isoformat(), sucursal_id):
        if item_id in ids and unidades > 0:
            vendidas[item_id] = vendidas.get(item_id, _CERO) + unidades
            dias_con_venta.setdefault(item_id, set()).add(str(occurred_on)[:10])

    factores: dict[int, Fraction] = {}
    if estacionalidad:
        horizontes = {p["id"]: dias_cobertura + (int(p["lead_time_days"]) if p["lead_time_days"] is not None else plazo_entrega_dias) for p in productos}
        factores = _factores_estacionales(conn, ids, hoy, dias_rotacion, horizontes, sucursal_id)

    lotes = _lotes_con_fecha(conn, ids, depositos) if descontar_vencido or descontar_por_vencer else {}
    vencidos = _vencido(lotes, hoy) if descontar_vencido else {}
    en_camino, en_camino_sin_sucursal = _en_camino(conn, sucursal_id)
    variantes = {
        f["item_id"]: f["n"]
        for f in conn.execute(
            "SELECT item_id, COUNT(*) AS n FROM item_variants WHERE active = 1 GROUP BY item_id"
        ).fetchall()
    }

    filas = []
    for p in productos:
        pid = p["id"]
        escala = (int(p["decimal_scale"] or 0) or _ESCALA_FRACCION) if p["allows_fraction"] else 0
        # Todas las cantidades se informan con la escala de la unidad (una de 6 decimales no puede mostrar 0,0004 como
        # 0,0); una entera, o de menos de 3, conserva los 3 de siempre por si el saldo trae fracción. `sugerido` va con `escala`.
        informe = max(escala, _ESCALA_MINIMA_DE_INFORME)
        stock = saldos.get(pid, _CERO)
        pedido = en_camino.get(pid, _CERO)
        minimo_propio = pid in minimos_propios
        minimo = minimos_propios[pid] if minimo_propio else _dec(p["min_stock"])
        unidades = vendidas.get(pid, _CERO)

        sin_stock = _dias_sin_stock(stock, movimientos.get(pid, []), desde, dias_rotacion,
                                    dias_con_venta.get(pid, set()))
        dias_con_stock = dias_rotacion - sin_stock
        dias_de_muestra = max(dias_con_stock, muestra_minima)
        sin_ventas = unidades <= 0

        vencido = vencidos.get(pid, _CERO)
        plazo_propio = p["lead_time_days"] is not None
        plazo = int(p["lead_time_days"]) if plazo_propio else plazo_entrega_dias
        horizonte = dias_cobertura + plazo
        maximo = _dec(p["max_stock"]) if p["max_stock"] is not None else None
        factor = factores.get(pid)
        # Racionales exactos, sin redondeos intermedios (ver `_factores_estacionales`): `proyectado` y la pérdida por vencer son fracciones y su suma no debe pedir una unidad de más ni de menos.
        proyectado = Fraction(unidades) * horizonte / dias_de_muestra * (factor if factor is not None else 1)
        por_vencer = _por_vencer(lotes.get(pid, []), hoy, horizonte, proyectado / horizonte) if descontar_por_vencer else Fraction(0)
        utilizable = max(Fraction(stock - vencido) - por_vencer, Fraction(0))
        disponible = utilizable + Fraction(pedido)
        por_rotacion = _techo(max(proyectado - disponible, Fraction(0)), escala)
        bajo_minimo = minimo > 0 and disponible < Fraction(minimo)
        sugerido = max(por_rotacion, _techo(Fraction(minimo) - disponible, escala)) if bajo_minimo else por_rotacion
        limitado = False
        if maximo is not None:
            tope = _piso(max(Fraction(maximo) - disponible, Fraction(0)), escala)
            limitado = sugerido > tope
            sugerido = min(sugerido, tope)
            por_rotacion = min(por_rotacion, tope)
            bajo_minimo = bajo_minimo and sugerido > 0
        if bajo_minimo and por_rotacion > 0:
            motivo = "ambos"
        else:
            motivo = "bajo_minimo" if bajo_minimo else ("por_rotacion" if por_rotacion > 0 else None)
        if solo_a_pedir and sugerido <= 0:
            continue

        cobertura = None if sin_ventas else float(round(_decimal(utilizable) * dias_de_muestra / unidades, 1))
        filas.append({
            "producto_id": pid, "codigo": p["codigo"], "nombre": p["name"], "unidad": p["unit_code"],
            "categoria": p["categoria"], "stock": _cantidad(stock, informe),
            "vencido": _cantidad(vencido, informe), "por_vencer": _cantidad(_techo(por_vencer, informe), informe),
            "en_camino": _cantidad(pedido, informe),
            "en_camino_sin_sucursal": _cantidad(en_camino_sin_sucursal.get(pid, _CERO), informe),
            "stock_minimo": _cantidad(minimo, informe), "stock_minimo_propio": minimo_propio,
            "unidades_vendidas": _cantidad(unidades, informe),
            "dias_con_stock": dias_con_stock, "rotacion_diaria": _cantidad(unidades / dias_de_muestra, informe),
            "cobertura_dias": cobertura, "sugerido": _cantidad(sugerido, escala), "motivo": motivo,
            "sin_ventas": sin_ventas, "posible_quiebre": stock <= 0 or sin_stock > 0,
            "variantes": variantes.get(pid, 0), "plazo_entrega_dias": plazo, "plazo_propio": plazo_propio,
            "stock_maximo": float(maximo) if maximo is not None else None,
            "limitado_por_maximo": limitado,
            "proveedor_id": p["proveedor_id"], "proveedor": p["proveedor"],
            "factor_estacional": round(float(factor), 2) if factor is not None else None,
        })
    # Menor cobertura primero (sin rotación al final), después lo que más hay que pedir, y el nombre para que dos
    # consultas seguidas den el mismo orden.
    return sorted(filas, key=lambda r: (r["cobertura_dias"] is None, r["cobertura_dias"] or 0, -r["sugerido"],
                                        r["nombre"].casefold(), r["producto_id"]))


# ── Parámetros propios del producto (ADR-020) ────────────────────────────


class ProductoNoEncontrado(LookupError):
    pass


class SinRevision(Exception):
    """A la base le falta una revisión del motor (`0003_parametros_reposicion`, `0004_proveedor_por_producto` o `0005_min_stock_por_sucursal`)."""


def _fila_del_producto(conn, item_id: int):
    proveedor = ("ci.supplier_party_id AS proveedor_id, sp.display_name AS proveedor"
                 if tiene_proveedor(conn) else "NULL AS proveedor_id, NULL AS proveedor")
    union = "LEFT JOIN parties sp ON sp.id = ci.supplier_party_id" if tiene_proveedor(conn) else ""
    filas = conn.execute(
        f"SELECT ci.id, ci.name, ci.min_stock, ci.lead_time_days, ci.max_stock, {proveedor} FROM catalog_items ci {union} "
        "WHERE ci.id = ?", (item_id,)).fetchall()
    if not filas:
        raise ProductoNoEncontrado(f"el producto {item_id} no existe")
    return filas[0]


def _bloquear_producto(conn, item_id: int) -> None:
    """Serializa las escrituras de reposición de UN producto (techo, plazo, proveedor y mínimos por sucursal) hasta el fin de la transacción. Hace falta porque
    `fijar_parametros` y `fijar_minimo_sucursal` validan el invariante «mínimo <= techo» con lo que leyeron: dos a la vez, cada una sobre lo que la otra todavía no
    confirmó, dejarían un mínimo de 80 y un techo de 50. Se toma ANTES de leer lo que se valida. PostgreSQL: el candado de la fila del producto (`FOR UPDATE`); bajo
    `READ COMMITTED` la lectura que sigue ya ve lo que confirmó quien lo tenía. SQLite (sólo pruebas): un `UPDATE` sin efecto toma el candado de escritura de la
    base, que ya serializa a los escritores (el mismo recurso que `reposicion_ordenes._serializar`). Un producto que no existe no bloquea nada: lo dice el que llama."""
    if isinstance(conn, sqlite3.Connection):
        conn.execute("UPDATE catalog_items SET min_stock = min_stock WHERE id = ?", (item_id,))
    else:
        conn.execute("SELECT id FROM catalog_items WHERE id = ? FOR UPDATE", (item_id,)).fetchall()


def _exigir_parametros(conn) -> None:
    if not tiene_parametros(conn):
        raise SinRevision("Falta la revisión 0003_parametros_reposicion del motor: corré `libracommerce-migrar upgrade` "
                          "(--prefijo del producto) antes de cargar plazos y techos de reposición.")


def parametros_de(conn, item_id: int) -> dict:
    """`{producto_id, nombre, plazo_entrega_dias, stock_maximo, stock_minimo, proveedor_id, proveedor}` de un producto: `None`
    en lo que no se definió (usa el plazo general / no tiene techo / sin proveedor habitual). `ProductoNoEncontrado` si no existe; `SinRevision` sin la
    revisión `0003`. No escribe."""
    _exigir_parametros(conn)
    p = _fila_del_producto(conn, item_id)
    return {"producto_id": p["id"], "nombre": p["name"],
            "plazo_entrega_dias": int(p["lead_time_days"]) if p["lead_time_days"] is not None else None,
            "stock_maximo": float(_dec(p["max_stock"])) if p["max_stock"] is not None else None,
            "stock_minimo": float(_dec(p["min_stock"])), "proveedor_id": p["proveedor_id"], "proveedor": p["proveedor"]}


#: «No tocar el proveedor»: `fijar_parametros` sin el argumento deja el proveedor como estaba (los clientes anteriores a v0.33.0 no lo
#: mandan). `None` lo borra.
SIN_CAMBIO = object()


def fijar_parametros(conn, item_id: int, *, plazo_entrega_dias, stock_maximo, proveedor_id=SIN_CAMBIO) -> dict:
    """Guarda el plazo de entrega y el techo de un producto; `None` en cualquiera de los dos lo borra (vuelve al plazo
    general / sin techo). Los dos son siempre los valores completos: no hay «no tocar». `proveedor_id` (ADR-021) sí puede
    omitirse (`SIN_CAMBIO`: queda como estaba); un id lo fija, `None` lo borra; tiene que ser un tercero que exista y esté
    activo, y pide la revisión `0004` (`SinRevision` si falta). Valida antes de escribir:
    el plazo, un entero de 1 a `MAX_PLAZO_ENTREGA_DIAS`; el techo, un número mayor que 0 y, si el producto tiene
    mínimo, no menor que él. `ValueError` con el motivo si no; `ProductoNoEncontrado`, `SinRevision`. No commitea."""
    _exigir_parametros(conn)
    _bloquear_producto(conn, item_id)
    p = _fila_del_producto(conn, item_id)
    if plazo_entrega_dias is not None:
        _entero_en_rango("plazo_entrega_dias", plazo_entrega_dias, MAX_PLAZO_ENTREGA_DIAS)
    techo = None
    if stock_maximo is not None:
        if isinstance(stock_maximo, bool) or not isinstance(stock_maximo, (int, float, Decimal, str)):
            raise ValueError(f"stock_maximo tiene que ser un número: {stock_maximo!r}")
        try:
            techo = Decimal(str(stock_maximo))
        except ArithmeticError as e:
            raise ValueError(f"stock_maximo tiene que ser un número: {stock_maximo!r}") from e
        if not techo.is_finite() or techo <= 0:
            raise ValueError(f"stock_maximo tiene que ser mayor que 0 (o vacío, sin techo): {stock_maximo!r}")
        minimo = _dec(p["min_stock"])
        if minimo > 0 and techo < minimo:
            raise ValueError(f"stock_maximo ({techo}) no puede ser menor que el stock mínimo del producto ({minimo})")
        if tiene_minimos_sucursal(conn):
            propios = [_dec(f["min_stock"]) for f in conn.execute(
                "SELECT min_stock FROM item_branch_min_stock WHERE item_id = ?", (item_id,)).fetchall()]
            if propios and techo < max(propios):
                raise ValueError(f"stock_maximo ({techo}) no puede ser menor que un stock mínimo por sucursal del producto ({max(propios)})")
    if proveedor_id is not SIN_CAMBIO:
        if not tiene_proveedor(conn):
            raise SinRevision("Falta la revisión 0004_proveedor_por_producto del motor: corré `libracommerce-migrar upgrade` "
                              "(--prefijo del producto) antes de cargar proveedores por producto.")
        if proveedor_id is not None:
            if isinstance(proveedor_id, bool) or not isinstance(proveedor_id, int):
                raise ValueError(f"proveedor_id tiene que ser un entero: {proveedor_id!r}")
            tercero = conn.execute("SELECT active FROM parties WHERE id = ?", (proveedor_id,)).fetchall()
            if not tercero:
                raise ValueError(f"el proveedor {proveedor_id} no existe")
            if not tercero[0]["active"]:
                raise ValueError(f"el proveedor {proveedor_id} está dado de baja")
    conn.execute("UPDATE catalog_items SET lead_time_days = ?, max_stock = ? WHERE id = ?",
                 (plazo_entrega_dias, str(techo) if techo is not None else None, item_id))
    if proveedor_id is not SIN_CAMBIO:
        conn.execute("UPDATE catalog_items SET supplier_party_id = ? WHERE id = ?", (proveedor_id, item_id))
    return parametros_de(conn, item_id)


# ── Mínimo por sucursal (ADR-024) ────────────────────────────────────────


def _exigir_minimos_sucursal(conn) -> None:
    if not tiene_minimos_sucursal(conn):
        raise SinRevision("Falta la revisión 0005_min_stock_por_sucursal del motor: corré `libracommerce-migrar upgrade` "
                          "(--prefijo del producto) antes de cargar mínimos por sucursal.")


def minimos_por_sucursal_de(conn, item_id: int) -> list[dict]:
    """El stock mínimo de un producto en **cada sucursal activa** (la predeterminada primero, luego por nombre): `[{sucursal_id, sucursal, stock_minimo,
    stock_minimo_propio, stock_minimo_global}]`. `stock_minimo` es el que usa la reposición en esa sucursal (el propio si lo tiene, si no el global) y
    `stock_minimo_propio` dice cuál de los dos es; `stock_minimo_global` es la referencia (`catalog_items.min_stock`). `ProductoNoEncontrado` si el producto
    no existe; `SinRevision` sin la revisión `0005`. No escribe."""
    _exigir_minimos_sucursal(conn)
    p = _fila_del_producto(conn, item_id)
    propios = {f["branch_id"]: _dec(f["min_stock"]) for f in conn.execute(
        "SELECT branch_id, min_stock FROM item_branch_min_stock WHERE item_id = ?", (item_id,)).fetchall()}
    global_ = float(_dec(p["min_stock"]))
    return [{"sucursal_id": b["id"], "sucursal": b["name"],
             "stock_minimo": float(propios[b["id"]]) if b["id"] in propios else global_,
             "stock_minimo_propio": b["id"] in propios, "stock_minimo_global": global_}
            for b in conn.execute("SELECT id, name FROM branches WHERE active = 1 ORDER BY is_default DESC, name, id").fetchall()]


def fijar_minimo_sucursal(conn, item_id: int, sucursal_id: int, stock_minimo) -> list[dict]:
    """Fija el stock mínimo de un producto en una sucursal; `None` borra el propio y esa sucursal vuelve al global. `0` es válido y significa «no me avises»
    en esa sucursal. Valida antes de escribir: la sucursal existe (y está activa para fijar un valor: borrar un propio de una sucursal dada de baja se
    permite, para poder limpiarlo), `stock_minimo` es un número finito mayor o igual que 0 y, si el producto tiene techo (`max_stock`), no lo pasa (el mismo
    invariante de ADR-020). `ValueError` con el motivo si no; `ProductoNoEncontrado`, `SinRevision`. Devuelve `minimos_por_sucursal_de`. Toma el candado del producto (`_bloquear_producto`) antes de validar, como `fijar_parametros`. No commitea."""
    _exigir_minimos_sucursal(conn)
    _bloquear_producto(conn, item_id)
    p = _fila_del_producto(conn, item_id)
    if isinstance(sucursal_id, bool) or not isinstance(sucursal_id, int):
        raise ValueError(f"sucursal_id tiene que ser un entero: {sucursal_id!r}")
    sucursal = conn.execute("SELECT active FROM branches WHERE id = ?", (sucursal_id,)).fetchall()
    if not sucursal:
        raise ValueError(f"la sucursal {sucursal_id} no existe")
    if stock_minimo is None:
        conn.execute("DELETE FROM item_branch_min_stock WHERE item_id = ? AND branch_id = ?", (item_id, sucursal_id))
        return minimos_por_sucursal_de(conn, item_id)
    if not sucursal[0]["active"]:
        raise ValueError(f"la sucursal {sucursal_id} está dada de baja")
    if isinstance(stock_minimo, bool) or not isinstance(stock_minimo, (int, float, Decimal, str)):
        raise ValueError(f"stock_minimo tiene que ser un número: {stock_minimo!r}")
    try:
        minimo = Decimal(str(stock_minimo))
    except ArithmeticError as e:
        raise ValueError(f"stock_minimo tiene que ser un número: {stock_minimo!r}") from e
    if not minimo.is_finite() or not 0 <= minimo <= MAX_STOCK_MINIMO:
        raise ValueError(f"stock_minimo tiene que ser un número entre 0 y {MAX_STOCK_MINIMO} (o vacío, el global): {stock_minimo!r}")
    if p["max_stock"] is not None and minimo > _dec(p["max_stock"]):
        raise ValueError(f"stock_minimo ({minimo}) no puede ser mayor que el stock máximo de reposición del producto ({_dec(p['max_stock'])})")
    conn.execute(
        "INSERT INTO item_branch_min_stock (item_id, branch_id, min_stock) VALUES (?, ?, ?) "
        "ON CONFLICT(item_id, branch_id) DO UPDATE SET min_stock = excluded.min_stock",
        (item_id, sucursal_id, format(minimo, "f")))
    return minimos_por_sucursal_de(conn, item_id)
