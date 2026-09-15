"""La línea de tiempo de actividad para un producto que vende con este motor
(P9-M4).

`libracore.db.logs.get_actividad_log` es un `UNION ALL` de partes. Dos de
ellas —ventas y stock— leen `ventas` y `movimientos_stock`, que en Contalibra y
Restolibra dejaron de existir como fuente de verdad: las ventas están en `sales`
y el stock es el ledger `stock_movements`. Los dos productos tenían copiada la
función entera (230 líneas, idénticas) para reescribir esas dos partes.

Desde LibraCore v1.87.0 las partes son constantes con nombre y la función acepta
`partes=`. Acá viven las dos de este motor; `partes_de_comercio()` las combina
con `logs.PARTES_CORE` (caja, facturas, turnos, remitos, presupuestos y, desde
v1.98.0, cierres diarios) en el mismo orden que tenían los productos, tomando
la tupla completa del `libracore` instalado en vez de copiar sus nombres acá
—así una parte nueva que el core sume a `PARTES_CORE` (como pasó con los
cierres diarios) entra sola a la línea de tiempo de este motor, sin tocar
este archivo—. Ningún nombre de parte del core más allá de `PARTE_CAJA` se
importa acá a propósito: los consumidores de este motor pinean `libracore`
por su cuenta, y un `libracore` viejo que todavía no tenga una parte nueva
(por ejemplo `PARTE_CIERRES_DIARIOS`) tiene que seguir andando igual, sin ese
import roto.

Las nueve columnas de cada parte, en este orden: `ts, fecha, tipo, descripcion,
monto, usuario, turno_id, ref_id, ref_tabla`. `fecha` es TEXT en todas —
PostgreSQL exige tipos compatibles entre las ramas de un UNION—.

La parte de ventas lee el turno de `venta_links`, que hasta M5 declara el
producto (ver `erp.ventas`).
"""

from __future__ import annotations

PARTE_VENTAS_COMERCIO = """
        SELECT
            s.created_at AS ts,
            s.occurred_on AS fecha,
            'venta'       AS tipo,
            'Venta ' || s.number ||
              CASE WHEN s.customer_name_snapshot != ''
                   THEN ' — ' || s.customer_name_snapshot ELSE '' END
              || ' (' || COALESCE(s.status_detail, s.status) || ')'  AS descripcion,
            CAST(s.total AS REAL) AS monto,  -- ver nota del CAST en la parte de stock
            COALESCE(u.nombre, '')        AS usuario,
            vl.turno_id,
            s.id          AS ref_id,
            'ventas'      AS ref_tabla
        FROM sales s
        LEFT JOIN venta_links vl ON vl.venta_id = s.id
        LEFT JOIN usuarios u ON u.id = s.created_by
"""

PARTE_STOCK_COMERCIO = """
        SELECT
            sm.created_at AS ts,
            substr(sm.occurred_at, 1, 10) AS fecha,
            'stock'       AS tipo,
            COALESCE(sm.reason_code, sm.movement_type) || ' ' || ci.name ||
              -- Doble CAST a propósito: `quantity_delta` es NUMERIC y SQLite
              -- guarda "-3.0" como el entero -3, con lo que el texto quedaría
              -- "-3 kg" en vez del "-3.0 kg" que venía mostrando la versión
              -- sobre `movimientos_stock` (columna REAL). Se preserva el
              -- formato para no cambiar lo que se ve en pantalla.
              ' (' || CAST(CAST(sm.quantity_delta AS REAL) AS TEXT) || ' ' || ci.unit_code || ')'
              || CASE WHEN sm.note != '' THEN ' — ' || sm.note ELSE '' END
              AS descripcion,
            ABS(CAST(sm.quantity_delta AS REAL)) AS monto,
            COALESCE(u.nombre, '') AS usuario,
            NULL          AS turno_id,
            sm.id         AS ref_id,
            'movimientos_stock' AS ref_tabla
        FROM stock_movements sm
        JOIN catalog_items ci ON ci.id = sm.item_id
        LEFT JOIN usuarios u ON u.id = sm.created_by
"""


def partes_de_comercio() -> tuple[str, ...]:
    """Las partes del UNION para un producto de este motor: ventas y stock
    de acá, más `logs.PARTES_CORE` del `libracore` instalado, en su orden.
    `PARTE_CAJA` va explícita en el medio (orden histórico: ventas, caja,
    stock, después el resto del core) y se descuenta de `PARTES_CORE` por
    identidad para no repetirla; el resto de la tupla del core entra tal cual
    venga, así que una parte nueva del core (como `PARTE_CIERRES_DIARIOS`
    desde v1.98.0) se suma sola sin que este archivo tenga que nombrarla."""
    from libracore.db.logs import PARTE_CAJA, PARTES_CORE

    resto_core = tuple(p for p in PARTES_CORE if p is not PARTE_CAJA)
    return (PARTE_VENTAS_COMERCIO, PARTE_CAJA, PARTE_STOCK_COMERCIO) + resto_core


def get_actividad_log(conn, tipos=None, usuario_id=None, turno_id=None,
                      desde: str = "", hasta: str = "", limit: int = 200, offset: int = 0) -> list[dict]:
    """La línea de tiempo unificada, sobre la conexión abierta. Cada fila:
    `{fecha, tipo, descripcion, monto, usuario, turno_id, ref_id, ref_tabla}`."""
    from libracore.db import logs

    return logs.get_actividad_log(tipos=tipos, usuario_id=usuario_id, turno_id=turno_id,
                                  desde=desde, hasta=hasta, limit=limit, offset=offset,
                                  partes=partes_de_comercio(), conn=conn)


def get_actividad_count(conn, tipos=None, usuario_id=None, turno_id=None,
                        desde: str = "", hasta: str = "") -> int:
    """Cuenta total de filas para paginación."""
    from libracore.db import logs

    return logs.get_actividad_count(tipos=tipos, usuario_id=usuario_id, turno_id=turno_id,
                                    desde=desde, hasta=hasta,
                                    partes=partes_de_comercio(), conn=conn)
