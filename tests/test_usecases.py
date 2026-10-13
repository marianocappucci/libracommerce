import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.db.schema import init_schema
from libracommerce.domain.catalog import CatalogItem, CatalogItemType, ItemVariant, Unit
from libracommerce.domain.entities import Party, PartyType
from libracommerce.domain.inventory import Location, StockMovement, StockMovementType
from libracommerce.domain.purchasing import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    PurchaseReceipt,
    PurchaseReceiptItem,
    PurchaseReceiptStatus,
)
from libracommerce.domain.sales import Sale, SaleItem, SaleStatus
from libracommerce.usecases.purchasing import confirm_purchase_receipt
from libracommerce.usecases.sales import cancel_sale, confirm_sale

WHEN = datetime(2026, 7, 25, 12, 0, 0)


@pytest.fixture
def repo() -> SqliteCommerceRepository:
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    return SqliteCommerceRepository(conn)


def _unit() -> Unit:
    return Unit("u", "Unidad")


def _product(repo: SqliteCommerceRepository, cost=Decimal("0")) -> CatalogItem:
    return repo.save_catalog_item(
        CatalogItem(None, CatalogItemType.PRODUCT, "Harina 1kg", _unit(), default_cost=cost)
    )


def _location(repo: SqliteCommerceRepository) -> Location:
    return repo.save_location(Location(None, "Deposito central"))


def _supplier(repo: SqliteCommerceRepository) -> Party:
    return repo.save_party(Party(None, PartyType.ORGANIZATION, "Proveedor SA"))


def _venta(product, cantidad, precio=Decimal("500")) -> Sale:
    """Venta de una sola linea de producto, que es el caso de casi todos los
    tests de anulacion/devolucion."""
    return Sale(
        None,
        "V-1",
        (SaleItem(CatalogItemType.PRODUCT, product.name, cantidad, precio,
                  item_id=product.id),),
    )


# confirm_sale


def test_confirm_sale_moves_stock_for_product_lines_only(repo: SqliteCommerceRepository):
    product = _product(repo)
    location = _location(repo)
    sale = Sale(
        None,
        "V-1",
        (
            SaleItem(CatalogItemType.PRODUCT, "Harina 1kg", Decimal("3"), Decimal("500"), item_id=product.id),
            SaleItem(CatalogItemType.SERVICE, "Consulta", Decimal("1"), Decimal("300")),
        ),
    )

    confirmed = confirm_sale(repo, sale, location.id, WHEN)

    assert confirmed.status == SaleStatus.CONFIRMED
    assert confirmed.confirmed_at == WHEN
    assert repo.current_stock(product.id, location.id) == Decimal("-3")
    movements = repo.list_stock_movements(product.id, location.id)
    assert len(movements) == 1
    assert movements[0].movement_type == StockMovementType.SALE
    assert movements[0].source_type == "sale"
    assert movements[0].source_id == confirmed.id


def test_confirm_sale_moves_stock_for_the_specific_variant_sold(repo: SqliteCommerceRepository):
    product = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Remera", _unit()))
    variant_m = repo.save_item_variant(ItemVariant(None, product.id, "REM-M-AZUL", "M / Azul"))
    variant_l = repo.save_item_variant(ItemVariant(None, product.id, "REM-L-AZUL", "L / Azul"))
    location = _location(repo)
    sale = Sale(
        None,
        "V-3",
        (
            SaleItem(
                CatalogItemType.PRODUCT, "Remera M/Azul", Decimal("2"), Decimal("5000"),
                item_id=product.id, variant_id=variant_m.id,
            ),
        ),
    )

    confirm_sale(repo, sale, location.id, WHEN)

    assert repo.current_stock(product.id, location.id, variant_id=variant_m.id) == Decimal("-2")
    assert repo.current_stock(product.id, location.id, variant_id=variant_l.id) == Decimal("0")
    assert repo.current_stock(product.id, location.id) == Decimal("0")


def test_confirm_sale_rejects_non_draft(repo: SqliteCommerceRepository):
    location = _location(repo)
    sale = Sale(None, "V-2", (), status=SaleStatus.CANCELLED)

    with pytest.raises(ValueError):
        confirm_sale(repo, sale, location.id, WHEN)


# confirm_purchase_receipt


def test_confirm_purchase_receipt_moves_stock_and_updates_cost(repo: SqliteCommerceRepository):
    product = _product(repo, cost=Decimal("100"))
    location = _location(repo)
    supplier = _supplier(repo)
    receipt = PurchaseReceipt(
        None,
        supplier_party_id=supplier.id,
        items=(PurchaseReceiptItem(item_id=product.id, quantity=Decimal("10"), unit_cost=Decimal("120")),),
    )

    confirmed = confirm_purchase_receipt(repo, receipt, location.id, WHEN)

    assert confirmed.status == PurchaseReceiptStatus.CONFIRMED
    assert confirmed.received_at == WHEN
    assert repo.current_stock(product.id, location.id) == Decimal("10")
    movements = repo.list_stock_movements(product.id, location.id)
    assert movements[0].movement_type == StockMovementType.PURCHASE
    assert movements[0].unit_cost == Decimal("120")
    assert repo.get_catalog_item(product.id).default_cost == Decimal("120")


def test_confirm_purchase_receipt_rejects_non_draft(repo: SqliteCommerceRepository):
    location = _location(repo)
    receipt = PurchaseReceipt(None, supplier_party_id=1, items=(), status=PurchaseReceiptStatus.CONFIRMED)

    with pytest.raises(ValueError):
        confirm_purchase_receipt(repo, receipt, location.id, WHEN)


def test_confirm_purchase_receipt_marks_linked_order_partial(repo: SqliteCommerceRepository):
    product = _product(repo)
    location = _location(repo)
    supplier = _supplier(repo)
    order = repo.save_purchase_order(
        PurchaseOrder(
            None,
            "OC-1",
            supplier_party_id=supplier.id,
            items=(PurchaseOrderItem(item_id=product.id, quantity_ordered=Decimal("10"), unit_cost=Decimal("100")),),
        )
    )
    receipt = PurchaseReceipt(
        None,
        supplier_party_id=supplier.id,
        purchase_order_id=order.id,
        items=(PurchaseReceiptItem(item_id=product.id, quantity=Decimal("4"), unit_cost=Decimal("100")),),
    )

    confirm_purchase_receipt(repo, receipt, location.id, WHEN)

    updated_order = repo.get_purchase_order(order.id)
    assert updated_order.status == PurchaseOrderStatus.PARTIAL
    assert updated_order.items[0].quantity_received == Decimal("4")
    assert not updated_order.is_fully_received()


def test_confirm_purchase_receipt_marks_linked_order_received_when_complete(repo: SqliteCommerceRepository):
    product = _product(repo)
    location = _location(repo)
    supplier = _supplier(repo)
    order = repo.save_purchase_order(
        PurchaseOrder(
            None,
            "OC-2",
            supplier_party_id=supplier.id,
            items=(PurchaseOrderItem(item_id=product.id, quantity_ordered=Decimal("10"), unit_cost=Decimal("100")),),
        )
    )
    receipt = PurchaseReceipt(
        None,
        supplier_party_id=supplier.id,
        purchase_order_id=order.id,
        items=(PurchaseReceiptItem(item_id=product.id, quantity=Decimal("10"), unit_cost=Decimal("100")),),
    )

    confirm_purchase_receipt(repo, receipt, location.id, WHEN)

    updated_order = repo.get_purchase_order(order.id)
    assert updated_order.status == PurchaseOrderStatus.RECEIVED
    assert updated_order.is_fully_received()


def test_confirm_purchase_receipt_without_order_skips_order_sync(repo: SqliteCommerceRepository):
    product = _product(repo)
    location = _location(repo)
    supplier = _supplier(repo)
    receipt = PurchaseReceipt(
        None,
        supplier_party_id=supplier.id,
        items=(PurchaseReceiptItem(item_id=product.id, quantity=Decimal("5"), unit_cost=Decimal("80")),),
    )

    confirmed = confirm_purchase_receipt(repo, receipt, location.id, WHEN)

    assert confirmed.purchase_order_id is None
    assert repo.current_stock(product.id, location.id) == Decimal("5")


# ── cancel_sale ──────────────────────────────────────────


def test_cancel_sale_repone_todo_el_stock(repo: SqliteCommerceRepository):
    product = _product(repo)
    location = _location(repo)
    sale = confirm_sale(repo, _venta(product, Decimal("3")), location.id, WHEN)
    assert repo.current_stock(product.id, location.id) == Decimal("-3")

    anulada = cancel_sale(repo, sale, WHEN)

    assert anulada.status == SaleStatus.CANCELLED
    assert repo.current_stock(product.id, location.id) == Decimal("0")


def test_cancel_sale_revierte_lo_que_salio_y_no_lo_que_dice_la_linea(
    repo: SqliteCommerceRepository,
):
    """El caso de las recetas: lo que se descontó no fue el producto vendido.

    La reversión se hace sobre el ledger, así que repone el insumo que salió
    de verdad sin tener que volver a resolver la receta.
    """
    plato = _product(repo)
    insumo = repo.save_catalog_item(
        CatalogItem(None, CatalogItemType.PRODUCT, "Harina a granel", _unit())
    )
    location = _location(repo)
    sale = confirm_sale(repo, _venta(plato, Decimal("1")), location.id, WHEN)
    # El producto compone: además del descuento del plato, salió el insumo.
    repo.append_stock_movement(
        StockMovement(
            id=None, item_id=insumo.id, location_id=location.id,
            movement_type=StockMovementType.SALE, quantity_delta=Decimal("-0.5"),
            occurred_at=WHEN, source_type="sale", source_id=sale.id,
        )
    )

    cancel_sale(repo, sale, WHEN)

    assert repo.current_stock(plato.id, location.id) == Decimal("0")
    assert repo.current_stock(insumo.id, location.id) == Decimal("0")


def test_anular_dos_veces_no_duplica_la_reposicion(repo: SqliteCommerceRepository):
    """Un reintento del botón no puede inventar stock."""
    product = _product(repo)
    location = _location(repo)
    sale = confirm_sale(repo, _venta(product, Decimal("2")), location.id, WHEN)

    anulada = cancel_sale(repo, sale, WHEN)
    otra_vez = cancel_sale(repo, anulada, WHEN)

    assert otra_vez.status == SaleStatus.CANCELLED
    assert repo.current_stock(product.id, location.id) == Decimal("0")


def test_no_se_anula_un_borrador(repo: SqliteCommerceRepository):
    product = _product(repo)
    sale = repo.save_sale(_venta(product, Decimal("1")))

    with pytest.raises(ValueError, match="confirmada"):
        cancel_sale(repo, sale, WHEN)


def test_return_sale_items_se_retiro_y_la_cuenta_vive_en_el_dominio():
    """ADR-053: nadie llamaba al caso de uso viejo (`return_sale_items`); la devolución es `erp.ventas.devolver_items`, que usa
    `domain.sales.reintegro_prorrateado` (probada en `test_reintegro_prorrateado.py`)."""
    from libracommerce.domain import sales as dominio
    from libracommerce.usecases import sales as casos

    assert not hasattr(casos, "return_sale_items")
    assert hasattr(dominio, "reintegro_prorrateado")
