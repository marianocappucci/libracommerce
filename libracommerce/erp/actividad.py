"""La línea de tiempo de actividad para un producto que vende con este motor
(P9-M4).

`libracore.db.logs.get_actividad_log` es un `UNION ALL` de siete partes. Dos de
ellas —ventas y stock— leen `ventas` y `movimientos_stock`, que en Contalibra y
Restolibra dejaron de existir como fuente de verdad: las ventas están en `sales`
y el stock es el ledger `stock_movements`. Los dos productos tenían copiada la
función entera (230 líneas, idénticas) para reescribir esas dos partes.

Desde LibraCore v1.87.0 las partes son constantes con nombre y la función acepta
`partes=`. Acá viven las dos de este motor; `partes_de_comercio()` las combina
con las cinco de LibraCore (caja, facturas, turnos, remitos, presupuestos) en el
mismo orden que tenían los productos.

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
    """Las siete partes del UNION para un producto de este motor: ventas y
    stock de acá, el resto de LibraCore."""
    from libracore.db.logs import (
        PARTE_CAJA,
        PARTE_FACTURAS,
        PARTE_PRESUPUESTOS,
        PARTE_REMITOS,
        PARTE_TURNOS,
    )

    return (PARTE_VENTAS_COMERCIO, PARTE_CAJA, PARTE_STOCK_COMERCIO,
            PARTE_FACTURAS, PARTE_TURNOS, PARTE_REMITOS, PARTE_PRESUPUESTOS)


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
