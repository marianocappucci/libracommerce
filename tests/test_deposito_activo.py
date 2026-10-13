"""El depósito por defecto no se desactiva en NINGUNA capa, y un movimiento de stock no toca un depósito inactivo (ADR-051).

Antes la guarda vivía sólo en la capa ERP (`catalogo.update_deposito`): `repo.save_location` guardaba el default inactivo sin
chistar (LibraDesk lo hace por ahí) y `add_movimiento_stock` seguía cargándole stock. Contra los dos motores."""
from __future__ import annotations

import datetime
from dataclasses import replace
from decimal import Decimal

import pytest
from conftest import USUARIO
from test_web_booleanos import mundo  # noqa: F401  (fixture)
from test_web_compras import _app, _deposito, _producto, _proveedor

from libracommerce.domain.catalog import CatalogItem, CatalogItemType, Unit
from libracommerce.domain.entities import Party, PartyType
from libracommerce.domain.inventory import (
    MENSAJE_DEFAULT_INACTIVO,
    DepositoInexistente,
    DepositoPorDefectoInactivo,
    Location,
    StockMovement,
    StockMovementType,
)
from libracommerce.domain.purchasing import PurchaseReceipt, PurchaseReceiptItem
from libracommerce.domain.sales import Sale, SaleItem
from libracommerce.erp import catalogo, stock, ventas
from libracommerce.usecases.inventory import transfer_stock
from libracommerce.usecases.purchasing import confirm_purchase_receipt
from libracommerce.usecases.sales import cancel_sale, confirm_sale

AHORA = datetime.datetime(2026, 10, 13, 12, 0, 0)


def _desactivar(conn, did):
    """Apaga un depósito por SQL, saltando todas las guardas: es cómo queda una base de antes de la regla."""
    conn.execute("UPDATE locations SET active = 0 WHERE id = ?", (did,))
    conn.commit()


def _filas(conn, did):
    return conn.execute("SELECT COUNT(*) FROM stock_movements WHERE location_id = ?", (did,)).fetchone()[0]


# ── 1. El depósito por defecto no se guarda inactivo: el repositorio ────────────────────────────────────────────────


def test_el_repositorio_no_guarda_un_default_nuevo_inactivo(repo):
    with pytest.raises(DepositoPorDefectoInactivo, match="por defecto"):
        repo.save_location(Location(None, "Central", active=False, is_default=True))
    assert repo.list_locations() == []


def test_el_repositorio_no_desactiva_el_default_existente(repo):
    central = repo.save_location(Location(None, "Central", is_default=True))
    with pytest.raises(DepositoPorDefectoInactivo) as e:
        repo.save_location(replace(central, active=False))
    assert str(e.value) == MENSAJE_DEFAULT_INACTIVO
    assert repo.get_location(central.id).active is True


def test_el_repositorio_si_desactiva_un_deposito_que_no_es_el_default(repo):
    repo.save_location(Location(None, "Central", is_default=True))
    otro = repo.save_location(Location(None, "Otro"))
    repo.save_location(replace(otro, active=False))
    assert repo.get_location(otro.id).active is False


def test_la_regla_es_de_ValueError_para_que_el_422_de_los_routers_la_atrape():
    assert issubclass(DepositoPorDefectoInactivo, ValueError) and issubclass(DepositoInexistente, ValueError)


# ── 1b. …y la ERP dice lo mismo, con el mismo texto ─────────────────────────────────────────────────────────────────


def test_la_erp_rechaza_desactivar_el_default_con_el_mismo_mensaje(mundo):  # noqa: F811
    w = mundo
    with w.abrir() as conn, pytest.raises(DepositoPorDefectoInactivo) as e:
        catalogo.update_deposito(conn, w.dep1, "Principal", "", 0)
    assert str(e.value) == MENSAJE_DEFAULT_INACTIVO
    r = w.client.put(f"/api/depositos/{w.dep1}", json={"nombre": "Principal", "activo": False})
    assert r.status_code == 422 and r.json()["detail"] == MENSAJE_DEFAULT_INACTIVO


def test_dar_de_baja_una_sucursal_con_el_deposito_por_defecto_ya_se_rechazaba(mundo):  # noqa: F811
    """La baja de una sucursal apaga sus depósitos con un UPDATE directo (no pasa por `save_location`), pero
    `_verificar_baja_de_sucursal` ya frena la baja si uno es el default del motor: se fija acá para que no se pierda."""
    w = mundo
    with w.abrir() as conn:
        catalogo.set_default_deposito(conn, w.dep_suc)
        conn.commit()
    with w.abrir() as conn, pytest.raises(ValueError, match="por defecto de la instancia"):
        catalogo.update_sucursal(conn, w.suc, "Casa Central", "", "", 0)
    with w.abrir() as conn:
        assert catalogo.get_deposito(conn, w.dep_suc)["activo"]


def test_marcar_como_default_un_deposito_que_no_existe_no_deja_al_motor_sin_default(mundo):  # noqa: F811
    w = mundo
    with w.abrir() as conn:
        with pytest.raises(DepositoInexistente):
            catalogo.set_default_deposito(conn, 99999)
        conn.rollback()
        assert catalogo.get_default_deposito_id(conn) == w.dep1


# ── 2. Un movimiento de stock no toca un depósito inactivo ──────────────────────────────────────────────────────────


def test_un_movimiento_hacia_un_deposito_inactivo_se_rechaza_sin_escribir(mundo):  # noqa: F811
    w = mundo
    with w.abrir() as conn:
        _desactivar(conn, w.dep2)
        with pytest.raises(DepositoInexistente, match="no existe o no está activo"):
            stock.add_movimiento_stock(conn, w.p1, "entrada", 5, "x", deposito_id=w.dep2)
        with pytest.raises(DepositoInexistente):
            stock.add_movimiento_stock(conn, w.p1, "salida", -1, "x", deposito_id=w.dep2)   # desde, también
        assert _filas(conn, w.dep2) == 0


def test_un_movimiento_a_un_deposito_que_no_existe_es_un_error_de_dominio_y_no_una_FK(mundo):  # noqa: F811
    w = mundo
    with w.abrir() as conn, pytest.raises(DepositoInexistente):
        stock.add_movimiento_stock(conn, w.p1, "entrada", 5, "x", deposito_id=99999)


def test_el_ajuste_y_la_salida_manual_en_un_deposito_inactivo_se_rechazan(mundo):  # noqa: F811
    w = mundo
    with w.abrir() as conn:
        _desactivar(conn, w.dep2)
        with pytest.raises(DepositoInexistente):
            stock.ajustar_stock(conn, w.p1, 7, "conteo", deposito_id=w.dep2)
        with pytest.raises(DepositoInexistente):
            stock.salida_manual(conn, w.p1, "salida", 1, "x", deposito_id=w.dep2)
        assert _filas(conn, w.dep2) == 0


def test_descontar_stock_de_una_venta_en_un_deposito_inactivo_se_rechaza(mundo):  # noqa: F811
    w = mundo
    with w.abrir() as conn:
        _desactivar(conn, w.dep2)
        with pytest.raises(DepositoInexistente):
            stock.descontar_stock_venta(conn, 1, [{"producto_id": w.p1, "qty": 1}], deposito_id=w.dep2)
        assert _filas(conn, w.dep2) == 0


def test_sin_deposito_el_movimiento_cae_en_el_default_activo(mundo):  # noqa: F811
    w = mundo
    with w.abrir() as conn:
        antes = _filas(conn, w.dep1)
        stock.add_movimiento_stock(conn, w.p1, "entrada", 1, "sin depósito")
        conn.commit()
        assert _filas(conn, w.dep1) == antes + 1


# ── 2b. Una base de ANTES de la regla (default inactivo): el código no le carga stock ───────────────────────────────


def test_con_un_default_inactivo_heredado_el_movimiento_va_al_primer_deposito_activo(mundo):  # noqa: F811
    """LibraDesk desactivaba el default por `save_location`, así que una base puede tenerlo. `get_default_deposito_id` lo saltea: nunca
    devuelve un inactivo, y la venta sin depósito explícito sigue andando (en el activo de menor id) en vez de cargarle stock a un
    depósito dado de baja o de bloquear el mostrador."""
    w = mundo
    with w.abrir() as conn:
        _desactivar(conn, w.dep1)     # el default, por SQL
        assert conn.execute("SELECT is_default FROM locations WHERE id = ?", (w.dep1,)).fetchone()[0]
        asegurado = catalogo.get_default_deposito_id(conn)
        assert asegurado not in (None, w.dep1)
        antes = _filas(conn, w.dep1)
        stock.add_movimiento_stock(conn, w.p1, "entrada", 1, "sin depósito")
        conn.commit()
        assert _filas(conn, w.dep1) == antes
        assert _filas(conn, asegurado) >= 1


def test_sin_ningun_deposito_activo_el_movimiento_sin_deposito_es_un_error_de_dominio(mundo):  # noqa: F811
    w = mundo
    with w.abrir() as conn:
        conn.execute("UPDATE locations SET active = 0")
        conn.commit()
        assert catalogo.get_default_deposito_id(conn) is None
        with pytest.raises(DepositoInexistente, match="ningún depósito activo"):
            stock.add_movimiento_stock(conn, w.p1, "entrada", 1, "x")


# ── 2c. Lo que NO se rechaza: deshacer lo que el ledger ya tiene ────────────────────────────────────────────────────


def test_anular_una_venta_cuyo_deposito_se_dio_de_baja_despues_sigue_andando(mundo):  # noqa: F811
    """`anular_venta` repone en el depósito de donde salió la mercadería. Si ese depósito se apagó después de la venta, negarse dejaría
    la venta sin poder anularse (`sobre_existencias=True`)."""
    w = mundo
    with w.abrir() as conn:
        _desactivar(conn, w.dep1)
        antes = stock.get_stock_actual(conn, w.p1, w.dep1)
        assert ventas.anular_venta(conn, w.venta, usuario_id=USUARIO["id"]) is True
        conn.commit()
        assert stock.get_stock_actual(conn, w.p1, w.dep1) == antes + 5


def test_devolver_a_un_deposito_inactivo_se_sigue_rechazando(mundo):  # noqa: F811
    w = mundo
    with w.abrir() as conn:
        _desactivar(conn, w.dep2)
        with pytest.raises(DepositoInexistente):
            ventas.devolver_items(conn, w.venta, {w.linea: 1}, deposito_id=w.dep2)


# ── 3. La recepción ─────────────────────────────────────────────────────────────────────────────────────────────────


def test_confirmar_una_recepcion_en_un_deposito_inactivo_da_422(abrir):
    """La recepción de tres pasos (`POST …/confirm`) no miraba el depósito: ahora `confirm_purchase_receipt` lo valida y el router
    contesta 422 (antes: stock cargado a un depósito dado de baja)."""
    proveedor, producto, deposito = _proveedor(abrir), _producto(abrir), _deposito(abrir)
    client = _app(abrir)
    recepcion = client.post("/api/purchase-receipts", json={"proveedor_id": proveedor}).json()
    client.post(f"/api/purchase-receipts/{recepcion['id']}/items", json={"item_id": producto, "quantity": 2, "unit_cost": 10})
    with abrir() as conn:
        _desactivar(conn, deposito)
    r = client.post(f"/api/purchase-receipts/{recepcion['id']}/confirm", json={"deposito_id": deposito})
    assert r.status_code == 422 and "no existe o no está activo" in r.json()["detail"], r.text
    with abrir() as conn:
        assert _filas(conn, deposito) == 0


# ── 4. Los casos de uso del dominio validan antes de escribir ───────────────────────────────────────────────────────


@pytest.fixture
def dominio(repo):
    """Un repositorio (los dos motores) con dos depósitos activos, uno inactivo, un producto y un proveedor."""
    class Mundo:
        pass

    d = Mundo()
    d.repo = repo
    d.producto = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", Unit("u", "Unidad")))
    d.a = repo.save_location(Location(None, "A", is_default=True))
    d.b = repo.save_location(Location(None, "B"))
    d.baja = repo.save_location(Location(None, "Baja", active=False))
    d.proveedor = repo.save_party(Party(None, PartyType.ORGANIZATION, "Prov"))
    return d


def _venta(d):
    return Sale(None, "V-1", (SaleItem(CatalogItemType.PRODUCT, "Yerba", Decimal(1), Decimal(100), item_id=d.producto.id),))


@pytest.mark.parametrize("malo", ["baja", "inexistente"])
def test_confirm_sale_valida_el_deposito_antes_de_guardar_la_venta(dominio, malo):
    d = dominio
    location_id = d.baja.id if malo == "baja" else 99999
    with pytest.raises(DepositoInexistente):
        confirm_sale(d.repo, _venta(d), location_id, AHORA)
    assert d.repo.list_stock_movements_by_source("sale", 1) == []
    assert confirm_sale(d.repo, _venta(d), d.a.id, AHORA).id is not None     # con uno bueno, anda


@pytest.mark.parametrize("malo", ["baja", "inexistente"])
def test_confirm_purchase_receipt_valida_el_deposito_antes_de_guardar(dominio, malo):
    d = dominio
    location_id = d.baja.id if malo == "baja" else 99999
    recibo = PurchaseReceipt(None, d.proveedor.id, (PurchaseReceiptItem(d.producto.id, Decimal(3), Decimal(10)),))
    with pytest.raises(DepositoInexistente):
        confirm_purchase_receipt(d.repo, recibo, location_id, AHORA)
    assert d.repo.list_purchase_receipts() == []
    confirmada = confirm_purchase_receipt(d.repo, recibo, d.a.id, AHORA)
    assert d.repo.current_stock(d.producto.id, d.a.id) == 3 and confirmada.id is not None


@pytest.mark.parametrize("lado", ["origen", "destino"])
def test_transfer_stock_valida_el_origen_y_el_destino(dominio, lado):
    d = dominio
    d.repo.append_stock_movement(StockMovement(None, d.producto.id, d.a.id, StockMovementType.PURCHASE, Decimal(10), AHORA))
    origen, destino = (d.baja.id, d.a.id) if lado == "origen" else (d.a.id, d.baja.id)
    with pytest.raises(DepositoInexistente):
        transfer_stock(d.repo, item_id=d.producto.id, from_location_id=origen, to_location_id=destino, quantity=Decimal(1),
                       occurred_at=AHORA)
    assert d.repo.current_stock(d.producto.id, d.a.id) == 10          # no salió nada
    assert d.repo.current_stock(d.producto.id, d.baja.id) == 0


def test_cancel_sale_no_valida_el_deposito_de_donde_salio_la_venta(dominio):
    """Repone en el depósito de cada movimiento; si se dio de baja después, la venta igual se anula."""
    d = dominio
    d.repo.save_location(replace(d.b, active=True))
    venta = confirm_sale(d.repo, _venta(d), d.b.id, AHORA)
    d.repo.save_location(replace(d.b, active=False))
    cancel_sale(d.repo, venta, AHORA)
    assert d.repo.current_stock(d.producto.id, d.b.id) == 0

