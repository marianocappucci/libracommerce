"""Lotes en el camino de la venta: FEFO, avisos y planificación (ADR-018, A-4 PR-2, 2026-09-30).

Este módulo es el piso de `erp.stock` y de `erp.vencimientos`: **no importa ninguno de los dos** (`stock` lo importa para
vender por lote y `vencimientos` para leer saldos; al revés habría un ciclo). Por eso acá viven también, y desde acá las
reexportan esos dos, las piezas que ninguno de los dos puede pedirle al otro: la normalización de lote y vencimiento y la
consulta de saldos por bucket.

**El lote es una dimensión del ledger**: un *bucket* es `(producto, depósito, variante, lote, vencimiento)` y su saldo es
la suma de `quantity_delta`. El bucket con lote y vencimiento en NULL es el stock **«sin lote»**.

**FEFO** (primero vence, primero sale), sólo para los productos marcados (`catalog_items.tracks_expiry = 1`):

1. los lotes con fecha, por `expires_at` ascendente (los vencidos incluidos: salen primero, con aviso), desempate por
   código de lote;
2. los lotes con código pero **sin** fecha, por código;
3. el bucket «sin lote», **último**.

`plan_fefo` sólo **planifica** (no escribe): devuelve los tramos y `erp.stock.descontar_stock_venta` escribe una fila
`sale` por tramo, copiando `lot_code` y `expires_at` del bucket. Lo que sobra cuando los lotes no alcanzan va **todo** a
una sola fila del bucket «sin lote» (que queda negativo, como hoy: ni se bloquea la venta ni se inventa un lote); esa
fila también lleva lo que el «sin lote» sí tenía, así que un marcado sin lotes escribe una fila idéntica a la de un
producto sin marcar. `Tramo.faltante` dice cuánto de esa fila no tenía respaldo en ningún bucket.

**Concurrencia** (PostgreSQL): `tomar_productos` bloquea los productos marcados con el mismo
`UPDATE catalog_items SET tracks_expiry = tracks_expiry WHERE id = ?` que usan `asignar_vencimiento_a_saldo` y
`dar_de_baja_lote`, **en orden ascendente de id** (dos ventas con varios productos no se cruzan) y **antes** de leer los
saldos: así dos ventas del mismo producto se serializan, y también contra asignar, dar de baja y cargar. Un producto sin
marcar no se bloquea (como hoy).

**Avisos** (`avisos_de_venta`, `planificar_salida`): informativos; el producto vencido **se vende** (decisión del humano,
2026-09-30). Ninguna función de acá commitea.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime as _datetime
from decimal import Decimal, InvalidOperation

from .catalogo import get_default_deposito_id
from .hooks import SIN_GANCHOS, Hooks

#: Largo máximo de un código de lote: es lo que se imprime en el envase, no un texto libre.
MAX_LARGO_LOTE = 64

#: Anticipación del aviso de vencimiento por defecto (la misma que el reporte de `erp.vencimientos`) y su tope.
DIAS_AVISO = 15
MAX_DIAS_AVISO = 365

#: Decimales con que se limpia el ruido de `float` de una suma (mismo criterio que `erp.reposicion`).
ESCALA_RUIDO = 10
CERO = Decimal("0")
#: Diferencia relativa por debajo de la cual una suma de tramos cuenta como igual a lo pedido (ruido de `float`).
UMBRAL_DE_RUIDO = Decimal("1e-12")

TIPO_LOTE_VENCIDO = "lote_vencido"
TIPO_POR_VENCER = "por_vencer"
TIPO_FALTANTE_SIN_LOTE = "faltante_sin_lote"


# ── Normalización (las usa `erp.stock.add_movimiento_stock`; se reexportan desde `erp.stock`) ──────────


def normalizar_lote(lot_code) -> str:
    """El código de lote recortado. `ValueError` si no es un texto, si queda vacío o si pasa de `MAX_LARGO_LOTE`
    caracteres: un lote sin código se expresa con `None` (no mandando el parámetro), no con una cadena vacía."""
    if not isinstance(lot_code, str):
        raise ValueError(f"lot_code tiene que ser un texto: {lot_code!r}")
    lote = lot_code.strip()
    if not lote:
        raise ValueError("lot_code no puede estar vacío; para un movimiento sin lote no se manda")
    if len(lote) > MAX_LARGO_LOTE:
        raise ValueError(f"lot_code no puede pasar de {MAX_LARGO_LOTE} caracteres: {lote[:20]!r}...")
    return lote


def normalizar_vencimiento(expires_at) -> str:
    """Un vencimiento como `'AAAA-MM-DD'`. Acepta un `date`, un `datetime` (se toma su fecha, tal como viene: sin
    convertir de zona) o un texto ISO 8601, con o sin hora (`'2026-10-05'`, `'2026-10-05T00:00:00'`,
    `'2026-10-05 10:30'`, `'...Z'`). Cualquier otra cosa es un `ValueError` con el valor a la vista."""
    if isinstance(expires_at, _datetime):
        return expires_at.date().isoformat()
    if isinstance(expires_at, _date):
        return expires_at.isoformat()
    if isinstance(expires_at, str) and expires_at.strip():
        texto = expires_at.strip()
        try:
            return _datetime.fromisoformat(texto).date().isoformat()  # 3.11+: acepta también la fecha sola
        except ValueError:
            pass
    raise ValueError(f"expires_at no es una fecha válida (AAAA-MM-DD o ISO 8601): {expires_at!r}")


# ── Saldos por bucket ────────────────────────────────────────────────────


def dec(valor) -> Decimal:
    return Decimal(str(valor)) if valor is not None else CERO


def saldo_limpio(valor) -> Decimal:
    """Un saldo como `Decimal` sin el ruido de la suma en `float` (`0.1 + 0.2`): a 10 decimales."""
    return round(dec(valor), ESCALA_RUIDO)


def lote_de_fila(valor) -> str | None:
    """El lote guardado, normalizado: un texto vacío o de espacios es «sin código»."""
    texto = str(valor).strip() if valor is not None else ""
    return texto or None


def vence_de_fila(valor) -> str | None:
    """El vencimiento guardado como `'AAAA-MM-DD'`, sea cual sea la forma en que se escribió. Uno que no se puede
    leer cuenta como sin vencimiento (el ledger lo escriben varios caminos; un reporte no debe caerse por uno)."""
    if valor is None:
        return None
    try:
        return normalizar_vencimiento(str(valor))
    except ValueError:
        return None


def saldos_por_bucket(conn, sql_items: str, params: list, depositos=None) -> dict[tuple, Decimal]:
    """`{(item, depósito, variante, lote, vence): saldo}` de los movimientos de `sql_items` (un `WHERE` sobre `sm`),
    con el lote y el vencimiento normalizados. Con `depositos` (algo que soporte `in`, p. ej. un dict por id), sólo los
    de esos depósitos; sin él, todos. Los saldos en cero se descartan."""
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
        if depositos is not None and f["location_id"] not in depositos:
            continue
        clave = (f["item_id"], f["location_id"], f["variant_id"], lote_de_fila(f["lot_code"]),
                 vence_de_fila(f["expires_at"]))
        saldos[clave] = saldos.get(clave, CERO) + saldo_limpio(f["saldo"])
    return {k: saldo_limpio(v) for k, v in saldos.items() if saldo_limpio(v) != 0}


def _donde_del_bucket(item_id: int, deposito_id: int, variante_id: int | None) -> tuple[str, list]:
    """El `WHERE` (sobre `sm`) de todos los buckets de un producto, depósito y variante. `variant_id = NULL` no
    compara en SQL, así que la variante `None` (el producto sin variantes) va con `IS NULL`."""
    if variante_id is None:
        return "sm.item_id = ? AND sm.location_id = ? AND sm.variant_id IS NULL", [item_id, deposito_id]
    return "sm.item_id = ? AND sm.location_id = ? AND sm.variant_id = ?", [item_id, deposito_id, variante_id]


def _buckets(conn, item_id: int, deposito_id: int, variante_id: int | None) -> dict[tuple, Decimal]:
    """`{(lote, vence): saldo}` de un producto, depósito y variante (los saldos en cero no figuran)."""
    donde, params = _donde_del_bucket(item_id, deposito_id, variante_id)
    return {k[3:]: v for k, v in saldos_por_bucket(conn, donde, params).items()}


# ── La sonda de opt-in y el bloqueo por producto ─────────────────────────


def tiene_marca(conn) -> bool:
    """Si la base tiene la revisión `0002_vencimientos_lotes` (la columna `catalog_items.tracks_expiry`). Se sondea por
    metadatos (`PRAGMA table_info`, que el adaptador traduce a `information_schema` en PostgreSQL) y **no** con un
    `SELECT` de la columna: en PostgreSQL un `SELECT` fallido **aborta la transacción** entera, y la venta que está
    corriendo dentro de ella se perdería por una columna que un producto sin vencimientos nunca usó. No se cachea: el
    «sí» de un proceso no vale para otra base (un test, o un producto que rehace su schema, la deja sin la columna) y
    el «no» puede dejar de serlo con `libracommerce-migrar upgrade`."""
    return "tracks_expiry" in {f[1] for f in conn.execute("PRAGMA table_info(catalog_items)").fetchall()}


def ids_marcados(conn, ids) -> set[int]:
    """Cuáles de `ids` son productos marcados como perecederos (`tracks_expiry = 1`): **una** consulta, y ninguna si no
    hay ids. **Tolera una base sin la revisión `0002`**: nadie está marcado. Es la sonda de opt-in: si devuelve un
    conjunto vacío, la venta y la anulación corren el código de siempre."""
    ids = sorted({i for i in ids if i})
    if not ids or not tiene_marca(conn):
        return set()
    marcadores = ",".join("?" for _ in ids)
    filas = conn.execute(
        f"SELECT id FROM catalog_items WHERE id IN ({marcadores}) AND tracks_expiry = 1", ids
    ).fetchall()
    return {f[0] for f in filas}


def tomar_productos(conn, ids) -> None:
    """Toma los productos: un `UPDATE` de sí mismos los bloquea hasta el commit (fila en PostgreSQL, base en SQLite),
    **en orden ascendente de id** para que dos ventas con varios productos no se crucen. Hay que llamarla **antes** de
    leer los saldos y releerlos después. Es el bloqueo de `asignar_vencimiento_a_saldo` y `dar_de_baja_lote`, así que
    también serializa contra ellos."""
    for i in sorted(set(ids)):
        conn.execute("UPDATE catalog_items SET tracks_expiry = tracks_expiry WHERE id = ?", (i,))


# ── FEFO ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Tramo:
    """Una fila `sale` a escribir: `cantidad` (positiva) del bucket `(lote, vence)`. El tramo sin lote (`lote` y `vence`
    en `None`) es siempre el último y puede traer `faltante`: la parte de su cantidad que no tenía respaldo (lo que los
    lotes no alcanzaron a cubrir y el bucket «sin lote» tampoco)."""

    lote: str | None
    vence: str | None
    cantidad: Decimal
    faltante: Decimal = CERO

    @property
    def sin_lote(self) -> bool:
        return self.lote is None and self.vence is None


def _cantidad(valor) -> Decimal:
    """La cantidad pedida como `Decimal` **exacto** (la representación más corta del `float`, sin redondear): una
    cantidad positiva nunca se convierte en cero ni pierde decimales por el camino."""
    if isinstance(valor, bool):
        raise ValueError(f"cantidad tiene que ser un número: {valor!r}")
    try:
        cant = Decimal(str(valor))
    except InvalidOperation:
        raise ValueError(f"cantidad tiene que ser un número: {valor!r}") from None
    if not cant.is_finite():
        raise ValueError(f"cantidad tiene que ser un número finito: {valor!r}")
    return cant


def _orden_fefo(clave: tuple[str | None, str | None]) -> tuple:
    lote, vence = clave
    # (1) con fecha, por fecha y después por código; (2) con código y sin fecha, por código.
    return (0, vence, lote or "") if vence is not None else (1, "", lote or "")


def _repartir(buckets: dict[tuple, Decimal], exacta: Decimal) -> list[Tramo]:
    """Reparte `exacta` entre los buckets con saldo > 0 en orden FEFO y **descuenta** lo repartido de `buckets` (así
    la siguiente línea del mismo producto ve lo que dejó la anterior). Sin lotes que consumir, el único tramo es el del
    «sin lote» por la cantidad entera.

    Regla: **una cantidad positiva nunca se pierde.** Los lotes se reparten con la cantidad limpiada a 10 decimales (el
    ruido de `float`, `0.1 + 0.2`, no deja restos ni filas de faltante), pero la **suma de los tramos es siempre igual
    a la cantidad exacta** (salvo el ruido de `float`, menos de 1e-12 relativo): si la limpieza dejó una diferencia real
    (menos de 1e-10) se suma al último tramo, y si dejó el
    plan vacío (una cantidad de 4e-11) el único tramo es el «sin lote» con la cantidad original."""
    tramos: list[Tramo] = []
    if exacta <= 0:
        return tramos
    restante = saldo_limpio(exacta)
    if restante > 0:
        for clave in sorted((k for k, s in buckets.items() if s > 0 and k != (None, None)), key=_orden_fefo):
            toma = min(restante, buckets[clave])
            tramos.append(Tramo(clave[0], clave[1], toma))
            buckets[clave] -= toma
            restante -= toma
            if restante <= 0:
                break
        if restante > 0:
            # Lo que queda va TODO a una fila del «sin lote»: lo que ese bucket tenía y, si no alcanza, el faltante.
            disponible = max(buckets.get((None, None), CERO), CERO)
            tramos.append(Tramo(None, None, restante, faltante=max(restante - disponible, CERO)))
            buckets[(None, None)] = buckets.get((None, None), CERO) - restante
    if not tramos:
        # La limpieza dejó el plan vacío (una cantidad de 4e-11): nunca se vuelve cero, va entera y sin tocar al «sin lote».
        disponible = max(buckets.get((None, None), CERO), CERO)
        tramos.append(Tramo(None, None, exacta, faltante=max(exacta - disponible, CERO)))
        buckets[(None, None)] = buckets.get((None, None), CERO) - exacta
        return tramos
    resto = exacta - sum((t.cantidad for t in tramos), CERO)
    if abs(resto) > UMBRAL_DE_RUIDO * max(Decimal(1), exacta):
        # Una diferencia real (1.00000000004 → 1.0 al limpiar): se suma al último tramo. El ruido del `float`
        # (4e-17 de 0.1 + 0.2) queda afuera: no vale una fila ni un lote «0.20000000000000004».
        ultimo = tramos[-1]
        tramos[-1] = Tramo(ultimo.lote, ultimo.vence, ultimo.cantidad + resto, ultimo.faltante)
        buckets[(ultimo.lote, ultimo.vence)] = buckets.get((ultimo.lote, ultimo.vence), CERO) - resto
    return tramos


def plan_fefo(conn, item_id: int, deposito_id: int, variante_id: int | None, cantidad) -> list[Tramo]:
    """Los tramos (una fila `sale` por bucket consumido) con que se vendería `cantidad` del producto en ese depósito y
    variante, en el orden FEFO de este módulo. **No escribe ni bloquea**: quien vende toma antes los productos
    (`tomar_productos`) y escribe después. Sólo mira los buckets de ESE depósito y de ESA variante (`None` = el producto
    sin variantes). `cantidad` ≤ 0, lista vacía. Los lotes vencidos se consumen igual (con aviso, no se bloquea)."""
    return _repartir(_buckets(conn, item_id, deposito_id, variante_id), _cantidad(cantidad))


# ── Avisos ───────────────────────────────────────────────────────────────


def _hoy(hoy: _date | None) -> _date:
    if hoy is not None:
        return hoy
    # Import diferido: `erp.vencimientos` importa este módulo, así que un import de arriba sería un ciclo. Se usa SU
    # `hoy_argentina` para que «hoy» sea uno solo en todo el motor (y se pueda fijar en un test en un solo lugar).
    from .vencimientos import hoy_argentina

    return hoy_argentina()


def _validar_dias(dias) -> int:
    if isinstance(dias, bool) or not isinstance(dias, int) or not 1 <= dias <= MAX_DIAS_AVISO:
        raise ValueError(f"dias tiene que ser un entero entre 1 y {MAX_DIAS_AVISO}: {dias!r}")
    return dias


def _num(valor: Decimal):
    """Un `int` si es entero, un `float` si no: `5` y no `5.0` para lo que se cuenta de a unidades."""
    return int(valor) if valor == valor.to_integral_value() else float(valor)


def _dias_para(vence: str | None, hoy: _date) -> int | None:
    return None if vence is None else (_date.fromisoformat(vence) - hoy).days


def _estado(lote: str | None, vence: str | None, dias_para: int | None) -> str:
    if vence is None:
        return "sin_lote" if lote is None else "sin_fecha"
    return "vencido" if dias_para < 0 else "vigente"


def _aviso(tipo, producto_id, nombre, lote, vence, dias_para, cantidad: Decimal, deposito_id, variante_id) -> dict:
    return {"tipo": tipo, "producto_id": producto_id, "nombre": nombre, "lote": lote, "vence": vence,
            "dias_para_vencer": dias_para, "cantidad": _num(cantidad), "deposito_id": deposito_id,
            "variante_id": variante_id}


def _avisos_de_tramo(tramo: Tramo, producto_id, nombre, deposito_id, variante_id, hoy: _date, dias: int) -> list[dict]:
    """Los avisos de una fila vendida: a lo sumo uno (`faltante_sin_lote` si es la del «sin lote» y trae faltante;
    `lote_vencido` o `por_vencer` según la fecha de su lote)."""
    if tramo.sin_lote:
        if tramo.faltante > 0:
            return [_aviso(TIPO_FALTANTE_SIN_LOTE, producto_id, nombre, None, None, None, tramo.faltante, deposito_id,
                           variante_id)]
        return []
    dias_para = _dias_para(tramo.vence, hoy)
    if dias_para is None:
        return []
    if dias_para < 0:
        tipo = TIPO_LOTE_VENCIDO
    elif dias_para <= dias:
        tipo = TIPO_POR_VENCER
    else:
        return []
    return [_aviso(tipo, producto_id, nombre, tramo.lote, tramo.vence, dias_para, tramo.cantidad, deposito_id,
                   variante_id)]


def _nombres(conn, ids) -> dict[int, str]:
    ids = sorted(set(ids))
    if not ids:
        return {}
    marcadores = ",".join("?" for _ in ids)
    return {f[0]: f[1] for f in conn.execute(f"SELECT id, name FROM catalog_items WHERE id IN ({marcadores})", ids)}


def _juntar(avisos: list[dict]) -> list[dict]:
    """Suma las cantidades de los avisos del mismo producto, depósito, variante, tipo, lote y vencimiento (dos líneas
    del mismo lote son un solo aviso), en el orden en que aparecieron."""
    juntos: dict[tuple, dict] = {}
    for a in avisos:
        clave = (a["tipo"], a["producto_id"], a["deposito_id"], a["variante_id"], a["lote"], a["vence"])
        if clave in juntos:
            total = dec(juntos[clave]["cantidad"]) + dec(a["cantidad"])
            juntos[clave]["cantidad"] = _num(total)
        else:
            juntos[clave] = dict(a)
    return list(juntos.values())


def avisos_de_venta(conn, venta_id: int, *, hoy: _date | None = None, dias: int = DIAS_AVISO) -> list[dict]:
    """Los avisos de una venta ya registrada, leídos de las filas `sale` que escribió (`reason_code='venta'`, las de
    un lote con su `lot_code` y `expires_at`). Sólo lectura.

    Cada aviso: `{tipo, producto_id, nombre, lote, vence, dias_para_vencer, cantidad, deposito_id, variante_id}`, uno
    por producto, depósito, variante y lote (las líneas del mismo lote se suman):

    - `lote_vencido`: salió de un lote con `vence < hoy` («hoy» es la fecha de Argentina, UTC-3);
    - `por_vencer`: salió de un lote que vence entre hoy y `hoy + dias`, ambos inclusive (`dias` es de 1 a 365,
      15 por defecto);
    - `faltante_sin_lote`: la venta dejó el bucket «sin lote» de un producto **marcado** en negativo, es decir salió
      mercadería que ningún lote ni el «sin lote» tenía (`lote` y `vence` en `None`; `cantidad` es lo que quedó sin
      respaldo, sin pasar de lo que esa venta sacó de ahí).

    Una venta sin lotes ni marcados (la de siempre) devuelve `[]`. No hay bloqueo: un producto vencido se vende."""
    dias = _validar_dias(dias)
    hoy = _hoy(hoy)
    filas = conn.execute(
        """SELECT id, item_id, location_id, variant_id, quantity_delta, lot_code, expires_at
           FROM stock_movements WHERE source_id = ? AND reason_code = 'venta' ORDER BY id""",
        (venta_id,),
    ).fetchall()
    if not filas:
        return []
    marcados = ids_marcados(conn, [f["item_id"] for f in filas])
    filas = [f for f in filas if f["item_id"] in marcados]
    nombres = _nombres(conn, [f["item_id"] for f in filas])
    avisos: list[dict] = []
    sin_lote: dict[tuple, dict] = {}     # (item, depósito, variante) -> {"cantidad", "hasta_id"}
    for f in filas:
        lote, vence = lote_de_fila(f["lot_code"]), vence_de_fila(f["expires_at"])
        cantidad = -dec(f["quantity_delta"])
        if cantidad <= 0:
            continue
        if lote is None and vence is None:
            dato = sin_lote.setdefault((f["item_id"], f["location_id"], f["variant_id"]),
                                       {"cantidad": CERO, "hasta_id": 0})
            dato["cantidad"] += cantidad
            dato["hasta_id"] = max(dato["hasta_id"], f["id"])
            continue
        avisos += _avisos_de_tramo(Tramo(lote, vence, cantidad), f["item_id"], nombres[f["item_id"]],
                                   f["location_id"], f["variant_id"], hoy, dias)
    for (item, dep, variante), dato in sin_lote.items():
        # El saldo del bucket justo después de la última fila de esta venta ahí: lo que otras ventas hagan después no
        # cambia lo que ésta dejó.
        donde, params = _donde_del_bucket(item, dep, variante)
        saldo = saldos_por_bucket(conn, donde + " AND sm.id <= ?", params + [dato["hasta_id"]]).get(
            (item, dep, variante, None, None), CERO)
        if saldo < 0:
            avisos.append(_aviso(TIPO_FALTANTE_SIN_LOTE, item, nombres[item], None, None, None,
                                 min(dato["cantidad"], -saldo), dep, variante))
    return _juntar(avisos)


def planificar_salida(conn, items, deposito_id: int | None = None, *, hoy: _date | None = None,
                      dias: int = DIAS_AVISO, hooks: Hooks = SIN_GANCHOS) -> dict:
    """Qué lote saldría si se vendieran estas líneas (las mismas que `erp.stock.descontar_stock_venta`: dicts con
    `producto_id`, `qty` y, si hay, `variante_id`), para que el POS lo confirme **antes de cobrar**. Lectura **pura**:
    no escribe, no bloquea ni commitea. Con `deposito_id=None` mira el depósito por defecto, como la venta.

    Devuelve `{hoy, dias, salidas, avisos}`. `salidas` es una fila por tramo, sólo de los productos **marcados** (los
    demás salen como siempre y no eligen lote), en el orden de las líneas: `{linea, producto_id, nombre, deposito_id,
    variante_id, lote, vence, dias_para_vencer, cantidad, estado, faltante}` con `estado` = `vencido`, `vigente`,
    `sin_fecha` (lote sin fecha) o `sin_lote`. Varias líneas del mismo producto consumen **en secuencia** (la segunda ve
    lo que dejó la primera), igual que la venta real; los insumos de una receta (`hooks.resolver_receta`) se planifican
    como los descuenta la venta. `avisos` son los de `avisos_de_venta` calculados sobre lo planificado. Es una
    simulación sobre el estado de ahora: otra venta concurrente puede cambiar el resultado antes de cobrar."""
    dias = _validar_dias(dias)
    hoy = _hoy(hoy)
    deposito = deposito_id if deposito_id is not None else get_default_deposito_id(conn)
    lineas: list[tuple[int, int, int | None, float]] = []     # (línea, producto, variante, cantidad)
    for n, item in enumerate(items):
        pid = item.get("producto_id")
        if not pid:
            continue
        qty = abs(float(item.get("qty", 0)))
        insumos = hooks.resolver_receta(pid, item)
        if insumos:
            lineas += [(n, i.item_id, None, float(i.cantidad) * qty) for i in insumos]
        else:
            lineas.append((n, pid, item.get("variante_id"), qty))
    marcados = ids_marcados(conn, [p for _, p, _, _ in lineas])
    nombres = _nombres(conn, marcados)
    buckets: dict[tuple, dict] = {}
    salidas: list[dict] = []
    avisos: list[dict] = []
    for n, pid, variante, cantidad in lineas:
        if pid not in marcados:
            continue
        b = buckets.setdefault((pid, variante), _buckets(conn, pid, deposito, variante))
        for t in _repartir(b, _cantidad(cantidad)):
            dias_para = _dias_para(t.vence, hoy)
            salidas.append({
                "linea": n, "producto_id": pid, "nombre": nombres[pid], "deposito_id": deposito,
                "variante_id": variante, "lote": t.lote, "vence": t.vence, "dias_para_vencer": dias_para,
                "cantidad": _num(t.cantidad), "estado": _estado(t.lote, t.vence, dias_para),
                "faltante": _num(t.faltante),
            })
            avisos += _avisos_de_tramo(t, pid, nombres[pid], deposito, variante, hoy, dias)
    return {"hoy": hoy.isoformat(), "dias": dias, "salidas": salidas, "avisos": _juntar(avisos)}


__all__ = [
    "CERO", "DIAS_AVISO", "ESCALA_RUIDO", "MAX_DIAS_AVISO", "MAX_LARGO_LOTE", "TIPO_FALTANTE_SIN_LOTE",
    "TIPO_LOTE_VENCIDO", "TIPO_POR_VENCER", "Tramo", "avisos_de_venta", "dec", "ids_marcados", "lote_de_fila",
    "normalizar_lote", "normalizar_vencimiento", "plan_fefo", "planificar_salida", "saldo_limpio", "saldos_por_bucket",
    "tiene_marca", "tomar_productos", "vence_de_fila",
]
