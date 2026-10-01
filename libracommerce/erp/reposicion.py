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

Agrupa por producto, no por variante (`variantes` dice cuántas activas tiene). Sólo productos activos, de tipo
`product` y `purchasable`; se agrega en Python con `Decimal` y las consultas son las mismas en SQLite y PostgreSQL.
"""

from __future__ import annotations

import datetime
from decimal import ROUND_CEILING, Decimal

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


def _techo(valor: Decimal, escala: int) -> Decimal:
    """`valor` redondeado hacia arriba a `escala` decimales."""
    paso = Decimal(10) ** escala
    return (valor * paso).to_integral_value(rounding=ROUND_CEILING) / paso


def _cantidad(valor: Decimal, escala: int):
    """Un `int` para lo que se pide de una unidad entera (`escala` 0), un `float` redondeado para el resto."""
    return int(valor) if escala == 0 else float(round(valor, escala))


def _entero_en_rango(nombre: str, valor, maximo: int) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or not 1 <= valor <= maximo:
        raise ValueError(f"{nombre} tiene que ser un entero entre 1 y {maximo}: {valor!r}")
    return valor


def _productos(conn, categoria: str | None, producto_id: int | None) -> list:
    donde = ["ci.active = 1", "ci.item_type = 'product'", "ci.purchasable = 1"]
    params: list = []
    if producto_id is not None:
        donde.append("ci.id = ?")
        params.append(producto_id)
    filas = conn.execute(
        f"""SELECT ci.id, ci.name, ci.unit_code, ci.min_stock, COALESCE(cat.name, '') AS categoria,
                   ic.code AS codigo, u.allows_fraction, u.decimal_scale
            FROM catalog_items ci
            LEFT JOIN categories cat ON cat.id = ci.category_id
            LEFT JOIN item_codes ic ON ic.item_id = ci.id AND ic.is_primary = 1
            LEFT JOIN units u ON u.code = ci.unit_code
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


def _vencido(conn, ids: set[int], depositos: set[int], hoy: datetime.date) -> dict[int, Decimal]:
    """Por producto marcado, la suma de los saldos positivos de los lotes ya vencidos (`vence < hoy`) en `depositos`.
    Vacío si la base no tiene la revisión `0002` o nadie está marcado. Ver "Lo vencido no se cuenta" en el módulo."""
    if not ids or not tiene_revision(conn):
        return {}
    marcados = {f["id"] for f in conn.execute("SELECT id FROM catalog_items WHERE tracks_expiry = 1").fetchall()}
    marcados &= ids
    if not marcados:
        return {}
    vencido: dict[int, Decimal] = {}
    saldos = saldos_por_bucket(conn, "sm.expires_at IS NOT NULL", [], depositos)
    for (item, _dep, _variante, _lote, vence), saldo in saldos.items():
        if item in marcados and vence is not None and saldo > 0 and datetime.date.fromisoformat(vence) < hoy:
            vencido[item] = vencido.get(item, _CERO) + saldo
    return vencido


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


def sugerencia_reposicion(conn, *, dias_rotacion: int = DIAS_ROTACION, dias_cobertura: int = DIAS_COBERTURA,
                          plazo_entrega_dias: int = PLAZO_ENTREGA_DIAS, sucursal_id: int | None = None,
                          categoria: str | None = None, producto_id: int | None = None,
                          solo_a_pedir: bool = True, descontar_vencido: bool = True,
                          hoy: datetime.date | None = None) -> list[dict]:
    """Qué pedir, por producto, del más urgente al menos (menor cobertura primero, luego mayor `sugerido`; los
    que no tienen rotación, al final). Cada fila: `producto_id`, `codigo`, `nombre`, `unidad`, `categoria`, `stock`,
    `vencido`, `en_camino`, `en_camino_sin_sucursal`, `stock_minimo`, `unidades_vendidas`, `dias_con_stock`, `rotacion_diaria`,
    `cobertura_dias` (`None` sin rotación), `sugerido`, `motivo` (`bajo_minimo`, `por_rotacion`, `ambos`, o `None`
    si no hay nada que pedir), `sin_ventas`, `posible_quiebre` y `variantes`. Ver el docstring del módulo.

    `solo_a_pedir` (el default) deja sólo los de `sugerido > 0`. `descontar_vencido` (el default) resta del stock lo
    que está en lotes vencidos. `hoy` es para las pruebas: el default es la fecha
    del servidor. Levanta `ValueError` con un parámetro fuera de rango o una `sucursal_id` que no existe.
    No commitea (no escribe)."""
    _entero_en_rango("dias_rotacion", dias_rotacion, MAX_DIAS_ROTACION)
    _entero_en_rango("dias_cobertura", dias_cobertura, MAX_DIAS_COBERTURA)
    _entero_en_rango("plazo_entrega_dias", plazo_entrega_dias, MAX_PLAZO_ENTREGA_DIAS)
    if sucursal_id is not None and not conn.execute("SELECT 1 FROM branches WHERE id = ?", (sucursal_id,)).fetchall():
        raise ValueError(f"la sucursal {sucursal_id} no existe")

    hoy = hoy or datetime.date.today()
    desde = hoy - datetime.timedelta(days=dias_rotacion - 1)
    horizonte = dias_cobertura + plazo_entrega_dias
    muestra_minima = min(dias_rotacion, _MIN_DIAS_DE_MUESTRA)

    productos = _productos(conn, categoria, producto_id)
    ids = {p["id"] for p in productos}
    depositos = _depositos(conn, sucursal_id)

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

    vencidos = _vencido(conn, ids, depositos, hoy) if descontar_vencido else {}
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
        minimo = _dec(p["min_stock"])
        unidades = vendidas.get(pid, _CERO)

        sin_stock = _dias_sin_stock(stock, movimientos.get(pid, []), desde, dias_rotacion,
                                    dias_con_venta.get(pid, set()))
        dias_con_stock = dias_rotacion - sin_stock
        dias_de_muestra = max(dias_con_stock, muestra_minima)
        sin_ventas = unidades <= 0

        vencido = vencidos.get(pid, _CERO)
        utilizable = max(stock - vencido, _CERO)
        disponible = utilizable + pedido
        por_rotacion = _techo(max(unidades * horizonte / dias_de_muestra - disponible, _CERO), escala)
        bajo_minimo = minimo > 0 and disponible < minimo
        sugerido = max(por_rotacion, _techo(minimo - disponible, escala)) if bajo_minimo else por_rotacion
        if bajo_minimo and por_rotacion > 0:
            motivo = "ambos"
        else:
            motivo = "bajo_minimo" if bajo_minimo else ("por_rotacion" if por_rotacion > 0 else None)
        if solo_a_pedir and sugerido <= 0:
            continue

        cobertura = None if sin_ventas else float(round(utilizable * dias_de_muestra / unidades, 1))
        filas.append({
            "producto_id": pid, "codigo": p["codigo"], "nombre": p["name"], "unidad": p["unit_code"],
            "categoria": p["categoria"], "stock": _cantidad(stock, informe),
            "vencido": _cantidad(vencido, informe), "en_camino": _cantidad(pedido, informe),
            "en_camino_sin_sucursal": _cantidad(en_camino_sin_sucursal.get(pid, _CERO), informe),
            "stock_minimo": _cantidad(minimo, informe), "unidades_vendidas": _cantidad(unidades, informe),
            "dias_con_stock": dias_con_stock, "rotacion_diaria": _cantidad(unidades / dias_de_muestra, informe),
            "cobertura_dias": cobertura, "sugerido": _cantidad(sugerido, escala), "motivo": motivo,
            "sin_ventas": sin_ventas, "posible_quiebre": stock <= 0 or sin_stock > 0,
            "variantes": variantes.get(pid, 0),
        })
    # Menor cobertura primero (sin rotación al final), después lo que más hay que pedir, y el nombre para que dos
    # consultas seguidas den el mismo orden.
    return sorted(filas, key=lambda r: (r["cobertura_dias"] is None, r["cobertura_dias"] or 0, -r["sugerido"],
                                        r["nombre"].casefold(), r["producto_id"]))
