"""«Ahora» y «hoy» son los de Argentina en todo el motor, aunque el servidor esté en UTC (ADR-052, que completa ADR-042 y ADR-044).

De 21 a 24 hs de Argentina un servidor en UTC ya está en el día siguiente. Los tests fijan el reloj del motor (`hora.ahora_utc`) a las
22:30 de Argentina —01:30Z del día siguiente—, así que no dependen de la hora en que corra la suite ni de la zona de la máquina."""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from test_web_booleanos import mundo  # noqa: F401  (fixture)

from libracommerce import hora
from libracommerce.domain.catalog import CatalogItem, CatalogItemType, ItemPrice, PriceList, Unit
from libracommerce.erp import listas_precio, promociones, stock, vencimientos

#: 22:30 del 14/03/2026 en Argentina = 01:30Z del 15/03/2026.
INSTANTE = datetime.datetime(2026, 3, 15, 1, 30, tzinfo=datetime.UTC)
AHORA_LOCAL = datetime.datetime(2026, 3, 14, 22, 30)
HOY_LOCAL = datetime.date(2026, 3, 14)


@pytest.fixture
def a_las_22_30(monkeypatch):
    monkeypatch.setattr(hora, "ahora_utc", lambda: INSTANTE)


def test_a_las_22_30_de_argentina_el_dia_sigue_siendo_el_de_argentina(a_las_22_30):
    assert INSTANTE.date() == datetime.date(2026, 3, 15)        # en UTC ya es mañana: el defecto
    assert hora.ahora_local() == AHORA_LOCAL
    assert hora.hoy_local() == HOY_LOCAL
    assert vencimientos.hoy_argentina() == HOY_LOCAL


def test_el_reloj_real_no_depende_de_la_zona_del_proceso():
    """Sin fijar nada, `ahora_utc` es un instante con zona UTC y `ahora_local` va 3 horas atrás, sin zona."""
    real = hora.ahora_utc()
    assert real.tzinfo is not None and real.utcoffset() == datetime.timedelta(0)
    assert hora.ahora_local().tzinfo is None
    assert abs((hora.ahora_local() - real.replace(tzinfo=None)) + datetime.timedelta(hours=3)) < datetime.timedelta(seconds=5)


def test_el_precio_vigente_sin_desde_arranca_a_la_hora_de_argentina(mundo, a_las_22_30):  # noqa: F811
    """`desde` vacío es «ahora»: con el reloj del sistema en UTC quedaba escrito 01:30 del día siguiente, tres horas en el futuro
    contra las vigencias (que están en hora local), y el precio no valía hasta las 01:30 hora Argentina."""
    w = mundo
    with w.abrir() as conn:
        listas_precio.set_precio_vigente(conn, w.lista, w.p1, 77.0)
        conn.commit()
        desde = conn.execute("SELECT valid_from FROM item_prices WHERE price_list_id = ? ORDER BY id DESC LIMIT 1",
                             (w.lista,)).fetchone()[0]
    assert str(desde).replace(" ", "T").startswith("2026-03-14T22:30:00")


def test_resolve_price_sin_at_compara_contra_la_hora_de_argentina(repo, a_las_22_30):
    """Un precio que vence a las 23:00 (hora local, como lo carga un `datetime-local`) vale a las 22:30 de Argentina. Con la hora del
    sistema en UTC (01:30 del día siguiente) ya había vencido."""
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", Unit("u", "Unidad")))
    lista = repo.save_price_list(PriceList(None, "General", is_default=True))
    repo.save_item_price(ItemPrice(None, item.id, lista.id, Decimal("1500"), valid_from=datetime.datetime(2026, 3, 14, 0, 0),
                                   valid_until=datetime.datetime(2026, 3, 14, 23, 0)))
    assert repo.resolve_price(item.id) == Decimal("1500")


def test_la_promocion_se_crea_con_la_hora_de_argentina(mundo, a_las_22_30):  # noqa: F811
    w = mundo
    with w.abrir() as conn:
        pid = promociones.crear_promocion(conn, nombre="2x1", tipo="nxm", paga=1, items=[{"producto_id": w.p1, "cantidad": 2}])
        conn.commit()
        creada = conn.execute("SELECT created_at FROM promotions WHERE id = ?", (pid,)).fetchone()[0]
    assert str(creada).startswith("2026-03-14T22:30:00")


def test_la_preferencia_guardada_lleva_la_hora_de_argentina(repo, a_las_22_30):
    repo.set_setting("formato", "x")
    fila = repo._conn.execute("SELECT updated_at FROM commerce_settings WHERE key = 'formato'").fetchone()
    assert str(fila[0]).startswith("2026-03-14 22:30:00")
    repo.set_setting("formato", "y")        # el UPDATE (ON CONFLICT) también
    fila = repo._conn.execute("SELECT updated_at FROM commerce_settings WHERE key = 'formato'").fetchone()
    assert str(fila[0]).startswith("2026-03-14 22:30:00")


def test_el_ajuste_de_stock_sin_fecha_queda_con_el_dia_de_argentina(mundo, a_las_22_30):  # noqa: F811
    """El camino de ADR-044, ahora por el reloj único: no hace falta fijar `hoy_argentina` aparte."""
    w = mundo
    with w.abrir() as conn:
        stock.add_movimiento_stock(conn, w.p2, "ajuste", 5, deposito_id=w.dep1)
        conn.commit()
        fecha = conn.execute("SELECT occurred_at FROM stock_movements WHERE item_id = ? ORDER BY id DESC LIMIT 1",
                             (w.p2,)).fetchone()[0]
    assert str(fecha).startswith("2026-03-14")
