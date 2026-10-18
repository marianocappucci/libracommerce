"""Un solo reloj para «hoy» y «ahora»: `libracore.fechas` (ADR-065 de este motor, ADR-070 de LibraCore).

El motor tenía su propia fuente de la hora (`libracommerce.hora`, ADR-052) aparte de la de LibraCore. Con la misma regla (UTC-3 fijo)
no daban distinto, pero eran dos: un test o un llamador que fijara el instante de `libracore.fechas` no movía ni las vigencias de
precio, ni las promociones, ni los vencimientos de acá. Ahora `hora` lee de `libracore.fechas`.

Estos tests fijan la zona del proceso en UTC (`TZ=UTC` + `time.tzset()`) y pisan **la función de LibraCore** que lee el reloj
(`libracore.fechas._ahora_utc`) a las 02:30Z del 18/10/2026 = 23:30 del 17/10 en Argentina. Rojos si `hora` vuelve a leer el reloj por
su cuenta: el motor daría las 02:30 del 18 (o la hora real de quien corra la suite)."""
from __future__ import annotations

import datetime
import sys
import time
from decimal import Decimal

import pytest
import test_vencimientos as _vto
from libracore import fechas
from test_web_booleanos import mundo  # noqa: F401  (fixture)
from test_web_compras import _app, _deposito, _producto, _proveedor

from libracommerce import hora
from libracommerce.domain.catalog import CatalogItem, CatalogItemType, ItemPrice, PriceList, Unit
from libracommerce.erp import listas_precio, promociones, stock, vencimientos

destino = _vto.destino          # las fixtures de siempre: una base por motor, con la cadena al día
abrir_vto = _vto.abrir_vto

#: 23:30 del 17/10/2026 en Argentina = 02:30Z del 18/10/2026.
INSTANTE = datetime.datetime(2026, 10, 18, 2, 30, tzinfo=datetime.UTC)
AHORA_AR = datetime.datetime(2026, 10, 17, 23, 30)
HOY_AR = datetime.date(2026, 10, 17)


@pytest.fixture
def utc_a_las_23_30_ar(monkeypatch):
    """El proceso en UTC y el reloj de LibraCore fijo a las 23:30 de Argentina del 17/10 (en UTC ya es el 18)."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    monkeypatch.setattr(fechas, "_ahora_utc", lambda: INSTANTE)
    yield
    monkeypatch.undo()          # saca el TZ y el reloj antes de volver a leer la zona del proceso
    time.tzset()


def test_el_dia_del_proceso_en_utc_ya_es_mañana_y_el_motor_sigue_en_el_de_argentina(utc_a_las_23_30_ar):
    assert time.strftime("%Z") == "UTC"
    assert INSTANTE.date() == datetime.date(2026, 10, 18)       # lo que daría el reloj del proceso: el defecto
    assert fechas.hoy() == HOY_AR
    assert hora.ahora_local() == AHORA_AR
    assert hora.hoy_local() == HOY_AR
    assert vencimientos.hoy_argentina() == HOY_AR


def test_hora_usa_la_zona_y_el_instante_de_libracore():
    """No hay una segunda definición del huso ni del reloj."""
    assert hora.ZONA_LOCAL is fechas.ZONA_AR
    assert listas_precio._ZONA_LOCAL is fechas.ZONA_AR


def test_sin_libracore_el_nucleo_sigue_andando_con_la_misma_regla(monkeypatch):
    """El núcleo no depende de LibraCore en runtime (`dependencies = []`): sin él, `hora` lee el reloj por su cuenta, UTC-3 fijo."""
    import importlib.util

    monkeypatch.setitem(sys.modules, "libracore", None)         # `import libracore` levanta ImportError
    monkeypatch.delitem(sys.modules, "libracore.fechas", raising=False)
    spec = importlib.util.spec_from_file_location("hora_sin_libracore", hora.__file__)
    suelta = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(suelta)
    assert suelta._fechas is None
    assert suelta.ZONA_LOCAL == fechas.ZONA_AR
    assert abs((suelta.ahora_utc() - datetime.datetime.now(datetime.UTC)).total_seconds()) < 5
    assert suelta.hoy_local() == (suelta.ahora_utc() + datetime.timedelta(hours=-3)).date()


def test_pisar_el_gancho_de_hora_sigue_valiendo(monkeypatch):
    """Los tests anteriores a este ADR pisan `hora.ahora_utc`: tiene que seguir moviendo «hoy» y «ahora» en todo el motor."""
    monkeypatch.setattr(hora, "ahora_utc", lambda: INSTANTE)
    assert hora.ahora_local() == AHORA_AR
    assert vencimientos.hoy_argentina() == HOY_AR


def test_el_reloj_real_es_el_de_libracore_y_no_depende_de_la_zona_del_proceso(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        real = hora.ahora_utc()
        assert real.utcoffset() == datetime.timedelta(0)
        assert abs(real - datetime.datetime.now(datetime.UTC)) < datetime.timedelta(seconds=5)
        assert abs((fechas.ahora() - real).total_seconds()) < 5
    finally:
        monkeypatch.undo()
        time.tzset()


def test_el_precio_vigente_sin_desde_arranca_a_la_hora_de_argentina(mundo, utc_a_las_23_30_ar):  # noqa: F811
    w = mundo
    with w.abrir() as conn:
        listas_precio.set_precio_vigente(conn, w.lista, w.p1, 77.0)
        conn.commit()
        desde = conn.execute("SELECT valid_from FROM item_prices WHERE price_list_id = ? ORDER BY id DESC LIMIT 1",
                             (w.lista,)).fetchone()[0]
    assert str(desde).replace(" ", "T").startswith("2026-10-17T23:30:00")


def test_resolve_price_sin_at_compara_contra_la_hora_de_argentina(repo, utc_a_las_23_30_ar):
    """Un precio vigente hasta las 23:45 del 17 vale a las 23:30 de Argentina; con la hora del proceso (02:30 del 18) ya había vencido."""
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", Unit("u", "Unidad")))
    lista = repo.save_price_list(PriceList(None, "General", is_default=True))
    repo.save_item_price(ItemPrice(None, item.id, lista.id, Decimal("1500"), valid_from=datetime.datetime(2026, 10, 17, 0, 0),
                                   valid_until=datetime.datetime(2026, 10, 17, 23, 45)))
    assert repo.resolve_price(item.id) == Decimal("1500")


def test_la_promocion_se_crea_y_se_evalua_con_la_hora_de_argentina(mundo, utc_a_las_23_30_ar):  # noqa: F811
    w = mundo
    with w.abrir() as conn:
        pid = promociones.crear_promocion(conn, nombre="2x1", tipo="nxm", paga=1, items=[{"producto_id": w.p1, "cantidad": 2}])
        conn.commit()
        creada = conn.execute("SELECT created_at FROM promotions WHERE id = ?", (pid,)).fetchone()[0]
    assert str(creada).startswith("2026-10-17T23:30:00")
    # Una promoción de 23:00 a 23:45 del 17 está vigente "ahora": con la hora del proceso (02:30 del 18) ya no.
    r = w.client.post("/api/promociones", json={"nombre": "Noche", "tipo": "nxm", "paga": 1,
                                                "items": [{"producto_id": w.p2, "cantidad": 2}],
                                                "desde": "2026-10-17T23:00:00", "hasta": "2026-10-17T23:45:00"})
    assert r.status_code == 200, r.text
    r = w.client.post("/api/promociones/calcular", json={"items": [{"producto_id": w.p2, "qty": 2, "precio": 50.0}]})
    assert r.status_code == 200, r.text
    assert r.json()["ahorro"] == 50.0


def test_los_vencimientos_y_el_ajuste_de_stock_usan_el_dia_de_argentina(abrir_vto, utc_a_las_23_30_ar):
    producto, deposito = _producto(abrir_vto), _deposito(abrir_vto)
    with abrir_vto() as conn:
        stock.add_movimiento_stock(conn, producto, "ajuste", 5, deposito_id=deposito)
        conn.commit()
        fecha = conn.execute("SELECT occurred_at FROM stock_movements WHERE item_id = ? ORDER BY id DESC LIMIT 1",
                             (producto,)).fetchone()[0]
        reporte = vencimientos.proximos_a_vencer(conn)
    assert str(fecha).startswith("2026-10-17")
    assert str(reporte["hoy"]) == "2026-10-17"


def test_la_recepcion_se_estampa_con_el_reloj_unico_y_conserva_su_formato(abrir_vto, utc_a_las_23_30_ar):
    """`compras_router` estampa la recepción como un instante con zona UTC (ADR-052 «no cubre»; ADR-065 lo deja así): el formato no cambia,
    pero el instante sale del reloj de LibraCore, así que un test lo puede fijar."""
    proveedor, producto, deposito, client = _proveedor(abrir_vto), _producto(abrir_vto), _deposito(abrir_vto), _app(abrir_vto)
    r = client.post("/api/purchase-receipts/recibir", json={
        "proveedor_id": proveedor, "deposito_id": deposito, "clave_operacion": "reloj-unico-1",
        "items": [{"item_id": producto, "quantity": 3, "unit_cost": 20}]})
    assert r.status_code == 200, r.text
    assert r.json()["received_at"] == "2026-10-18T02:30:00+00:00"
    with abrir_vto() as conn:
        # Lo que ADR-065 NO cambia y deja dicho: el movimiento de stock lleva el instante en UTC, así que el reporte por día
        # (`substr(occurred_at, 1, 10)`) lo cuenta el 18 aunque en Argentina sea el 17. Si se unifica el formato, este assert cambia.
        assert conn.execute("SELECT occurred_at FROM stock_movements WHERE item_id = ?", (producto,)).fetchone()[0] \
            == "2026-10-18T02:30:00+00:00"
    r = client.post("/api/purchase-receipts", json={"proveedor_id": proveedor, "document_reference": "REM-9"})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    client.post(f"/api/purchase-receipts/{rid}/items", json={"item_id": producto, "quantity": 1, "unit_cost": 20})
    r = client.post(f"/api/purchase-receipts/{rid}/confirm", json={"deposito_id": deposito})
    assert r.status_code == 200, r.text
    assert r.json()["received_at"] == "2026-10-18T02:30:00+00:00"
