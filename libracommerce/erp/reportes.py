"""Reportes agregados sobre las ventas del motor (P9-M4).

Contalibra (P7) y Restolibra (P8) tenían este módulo copiado como
`app/db_reportes.py`: cinco de las siete funciones de `libracore.db.reportes`
leen `ventas`/`productos`, que dejaron de ser la fuente de verdad cuando las
ventas pasaron a `sales`/`sale_items` y el catálogo a `catalog_items`. Las dos
copias eran idénticas salvo el docstring. Las otras dos (`get_reporte_caja` y
`get_reporte_caja_medios`) sólo tocan `caja_movimientos` y siguen en LibraCore.

Son la implementación de `libracore.reportes.PuertoDeReportes` para un producto
que vende con este motor: `puerto_de_reportes(get_connection)` devuelve el
puerto ya atado a la fábrica de conexiones, listo para
`libracore.reportes_router.build_reportes_router(reportes=...)`.

`reporte_productos_top` es más simple que el de LibraCore: `sale_items` está
normalizado y es un `GROUP BY` directo, sin desarmar el JSON de `ventas.items`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _rango(desde: str, hasta: str, columna: str) -> tuple[str, list]:
    where, params = [], []
    if desde:
        where.append(f"{columna} >= ?")
        params.append(desde)
    if hasta:
        where.append(f"{columna} <= ?")
        params.append(hasta)
    return (("WHERE " + " AND ".join(where)) if where else ""), params


def reporte_ventas(conn, desde: str = "", hasta: str = "", agrupacion: str = "dia") -> list[dict]:
    """Ventas agrupadas por día, semana o mes."""
    fmt = {"dia": "%Y-%m-%d", "semana": "%Y-W%W", "mes": "%Y-%m"}.get(agrupacion, "%Y-%m-%d")
    w, params = _rango(desde, hasta, "occurred_on")
    sql = f"""
        SELECT strftime('{fmt}', occurred_on) AS periodo,
               COUNT(*) AS cantidad,
               ROUND(SUM(total), 2) AS total
        FROM sales {w}
        GROUP BY periodo ORDER BY periodo
    """
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def reporte_medios_pago(conn, desde: str = "", hasta: str = "") -> list[dict]:
    """Totales por medio de pago en el período (`ventas_pagos` es de LibraCore;
    la venta, de acá)."""
    w, params = _rango(desde, hasta, "s.occurred_on")
    sql = f"""
        SELECT vp.medio, COUNT(DISTINCT vp.venta_id) AS operaciones,
               ROUND(SUM(vp.monto), 2) AS total
        FROM ventas_pagos vp
        JOIN sales s ON s.id = vp.venta_id {w}
        GROUP BY vp.medio ORDER BY total DESC
    """
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def reporte_productos_top(conn, desde: str = "", hasta: str = "", limit: int = 20) -> list[dict]:
    """Productos más vendidos (por cantidad y por monto) en el período."""
    w, params = _rango(desde, hasta, "s.occurred_on")
    sql = f"""
        SELECT si.description_snapshot AS nombre,
               ROUND(SUM(CAST(si.quantity AS REAL)), 2) AS cantidad,
               ROUND(SUM(CAST(si.quantity AS REAL) * CAST(si.unit_price AS REAL)), 2) AS total
        FROM sales s
        JOIN sale_items si ON si.sale_id = s.id {w}
        GROUP BY nombre ORDER BY cantidad DESC LIMIT ?
    """
    return [dict(r) for r in conn.execute(sql, params + [limit]).fetchall()]


def reporte_stock_bajo(conn) -> list[dict]:
    """Productos con existencia por debajo del mínimo, según el ledger."""
    sql = """
        SELECT ci.id, ci.name AS nombre, ic.code AS codigo, ci.min_stock AS stock_minimo,
               ROUND(COALESCE(SUM(sm.quantity_delta), 0), 3) AS stock_actual
        FROM catalog_items ci
        LEFT JOIN item_codes ic ON ic.item_id = ci.id AND ic.is_primary = 1
        LEFT JOIN stock_movements sm ON sm.item_id = ci.id
        GROUP BY ci.id, ic.code
        -- La expresion repetida en vez del alias: PostgreSQL no acepta alias
        -- de la SELECT ni en el HAVING ni dentro de una expresion del ORDER BY.
        HAVING ROUND(COALESCE(SUM(sm.quantity_delta), 0), 3) < ci.min_stock
        ORDER BY (ci.min_stock - ROUND(COALESCE(SUM(sm.quantity_delta), 0), 3)) DESC
    """
    return [dict(r) for r in conn.execute(sql).fetchall()]


def reporte_resumen(conn, desde: str = "", hasta: str = "") -> dict:
    """KPIs rápidos del período: ventas (de acá), facturas y saldo de caja (de
    LibraCore)."""
    w_ventas, params = _rango(desde, hasta, "occurred_on")
    w, _ = _rango(desde, hasta, "fecha")
    v = conn.execute(f"SELECT COUNT(*) cnt, ROUND(SUM(total),2) total FROM sales {w_ventas}", params).fetchone()
    f_row = conn.execute(f"SELECT COUNT(*) cnt FROM facturas {w}", params).fetchone()
    caja = conn.execute(
        f"SELECT ROUND(SUM(CASE WHEN tipo='ingreso' THEN monto ELSE -monto END),2) saldo "
        f"FROM caja_movimientos {w}", params
    ).fetchone()
    return {
        "ventas_cantidad": v["cnt"] or 0,
        "ventas_total": v["total"] or 0.0,
        "facturas_cantidad": f_row["cnt"] or 0,
        "caja_saldo": caja["saldo"] or 0.0,
    }


def puerto_de_reportes(conexion: Callable[[], Any]):
    """El `PuertoDeReportes` de LibraCore con las cinco lecturas de acá, cada
    una abriendo su conexión con la fábrica del producto (`get_connection`).
    Las de caja no entran: el router las toma de `libracore.db.reportes`."""
    from libracore.reportes import PuertoDeReportes

    def _con(fn):
        def _f(*args, **kwargs):
            with conexion() as conn:
                return fn(conn, *args, **kwargs)
        _f.__name__ = fn.__name__
        return _f

    return PuertoDeReportes(
        ventas=_con(reporte_ventas),
        medios_pago=_con(reporte_medios_pago),
        productos_top=_con(reporte_productos_top),
        stock_bajo=_con(reporte_stock_bajo),
        resumen=_con(reporte_resumen),
    )
