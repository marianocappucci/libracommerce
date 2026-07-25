import sqlite3
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.db.schema import init_schema
from libracommerce.domain.catalog import CatalogItem, CatalogItemType, Unit
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


@pytest.fixture
def repo() -> SqliteCommerceRepository:
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    return SqliteCommerceRepository(conn)


def _kg() -> Unit:
    return Unit("kg", "Kilogramo", True, 3)


def test_save_party_assigns_id_and_round_trips(repo: SqliteCommerceRepository):
    party = Party(None, PartyType.PERSON, "Ana", email="ana@example.com")
    saved = repo.save_party(party)
    assert saved.id is not None

    fetched = repo.get_party(saved.id)
    assert fetched == saved


def test_save_party_updates_existing_row(repo: SqliteCommerceRepository):
    saved = repo.save_party(Party(None, PartyType.PERSON, "Ana"))
    updated = repo.save_party(Party(saved.id, PartyType.PERSON, "Ana Maria", email="am@example.com"))

    fetched = repo.get_party(saved.id)
    assert fetched.display_name == "Ana Maria"
    assert fetched.email == "am@example.com"
    assert updated.id == saved.id


def test_save_catalog_item_persists_unit_and_metadata(repo: SqliteCommerceRepository):
    item = CatalogItem(
        None,
        CatalogItemType.PRODUCT,
        "Yerba",
        _kg(),
        default_sale_price=Decimal("1500.50"),
        default_cost=Decimal("900"),
        metadata={"brand": "Playadito"},
    )
    saved = repo.save_catalog_item(item)
    assert saved.id is not None

    fetched = repo.get_catalog_item(saved.id)
    assert fetched.name == "Yerba"
    assert fetched.unit == _kg()
    assert fetched.default_sale_price == Decimal("1500.50")
    assert fetched.metadata == {"brand": "Playadito"}


def test_save_catalog_item_reuses_unit_across_items(repo: SqliteCommerceRepository):
    repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Azucar", _kg()))

    units = repo._conn.execute("SELECT COUNT(*) FROM units WHERE code = 'kg'").fetchone()[0]
    assert units == 1


def test_save_location_round_trips(repo: SqliteCommerceRepository):
    location = Location(None, "Deposito Central", branch_id=1, location_type="warehouse")
    saved = repo.save_location(location)
    assert saved.id is not None
    assert repo.get_location(saved.id) == saved


def test_stock_movements_are_immutable_and_listed_in_order(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    location = repo.save_location(Location(None, "Deposito Central"))

    first = repo.append_stock_movement(
        StockMovement(
            None, item.id, location.id, StockMovementType.PURCHASE, Decimal("10"), datetime(2026, 7, 1)
        )
    )
    second = repo.append_stock_movement(
        StockMovement(
            None, item.id, location.id, StockMovementType.SALE, Decimal("-3"), datetime(2026, 7, 2)
        )
    )

    movements = repo.list_stock_movements(item.id, location.id)
    assert [m.id for m in movements] == [first.id, second.id]
    assert repo.current_stock(item.id, location.id) == Decimal("7")


def test_save_sale_persists_items_and_can_be_confirmed(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    customer = repo.save_party(Party(None, PartyType.PERSON, "Ana"))

    line = SaleItem(
        kind=CatalogItemType.PRODUCT,
        item_id=item.id,
        description_snapshot="Yerba",
        quantity=Decimal("2"),
        unit_price=Decimal("1500"),
        tax_amount=Decimal("315"),
    )
    sale = Sale(None, "V-0001", (line,), customer_party_id=customer.id, total=Decimal("3315"))

    saved = repo.save_sale(sale)
    assert saved.id is not None

    confirmed = repo.save_sale(
        replace(saved, status=SaleStatus.CONFIRMED, confirmed_at=datetime(2026, 7, 25, 12, 0))
    )
    fetched = repo.get_sale(confirmed.id)

    assert fetched.status == SaleStatus.CONFIRMED
    assert fetched.confirmed_at == datetime(2026, 7, 25, 12, 0)
    assert len(fetched.items) == 1
    assert fetched.items[0].kind == CatalogItemType.PRODUCT
    assert fetched.items[0].line_total == Decimal("3315")
    assert fetched.total == Decimal("3315")


def test_save_sale_persists_ad_hoc_service_item_without_catalog_link(repo: SqliteCommerceRepository):
    line = SaleItem(
        kind=CatalogItemType.SERVICE,
        item_id=None,
        description_snapshot="Consulta fuera de catálogo",
        quantity=Decimal("1"),
        unit_price=Decimal("5000"),
    )
    sale = Sale(None, "V-0002", (line,), status=SaleStatus.CONFIRMED, total=Decimal("5000"))

    saved = repo.save_sale(sale)
    fetched = repo.get_sale(saved.id)

    assert len(fetched.items) == 1
    assert fetched.items[0].kind == CatalogItemType.SERVICE
    assert fetched.items[0].item_id is None
    assert fetched.items[0].description_snapshot == "Consulta fuera de catálogo"


def test_save_sale_rejects_product_item_without_catalog_link_at_domain_level(repo: SqliteCommerceRepository):
    with pytest.raises(ValueError):
        SaleItem(
            kind=CatalogItemType.PRODUCT,
            item_id=None,
            description_snapshot="Producto sin catalogar",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
        )


def test_save_purchase_order_persists_items_and_tracks_partial_receiving(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    supplier = repo.save_party(Party(None, PartyType.ORGANIZATION, "Distribuidora SA"))

    line = PurchaseOrderItem(item.id, quantity_ordered=Decimal("20"), unit_cost=Decimal("900"))
    order = PurchaseOrder(
        None, "OC-0001", supplier_party_id=supplier.id, items=(line,), ordered_at=datetime(2026, 7, 20)
    )

    saved = repo.save_purchase_order(order)
    assert saved.id is not None
    assert repo.get_purchase_order(saved.id).status == PurchaseOrderStatus.DRAFT

    partially_received_line = PurchaseOrderItem(
        item.id, quantity_ordered=Decimal("20"), unit_cost=Decimal("900"), quantity_received=Decimal("12")
    )
    updated = repo.save_purchase_order(
        replace(saved, status=PurchaseOrderStatus.PARTIAL, items=(partially_received_line,))
    )
    fetched = repo.get_purchase_order(updated.id)

    assert fetched.status == PurchaseOrderStatus.PARTIAL
    assert len(fetched.items) == 1
    assert fetched.items[0].pending_quantity == Decimal("8")
    assert not fetched.is_fully_received()


def test_save_purchase_receipt_persists_lot_and_expiry(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    supplier = repo.save_party(Party(None, PartyType.ORGANIZATION, "Distribuidora SA"))
    order = repo.save_purchase_order(
        PurchaseOrder(
            None,
            "OC-0002",
            supplier_party_id=supplier.id,
            items=(PurchaseOrderItem(item.id, Decimal("10"), Decimal("900")),),
        )
    )

    line = PurchaseReceiptItem(
        item.id, quantity=Decimal("10"), unit_cost=Decimal("910"), lot_code="L-2026-07", expires_at=datetime(2027, 1, 1)
    )
    receipt = PurchaseReceipt(
        None,
        supplier_party_id=supplier.id,
        items=(line,),
        purchase_order_id=order.id,
        received_at=datetime(2026, 7, 25),
    )

    saved = repo.save_purchase_receipt(receipt)
    confirmed = repo.save_purchase_receipt(replace(saved, status=PurchaseReceiptStatus.CONFIRMED))
    fetched = repo.get_purchase_receipt(confirmed.id)

    assert fetched.status == PurchaseReceiptStatus.CONFIRMED
    assert fetched.purchase_order_id == order.id
    assert fetched.items[0].lot_code == "L-2026-07"
    assert fetched.items[0].expires_at == datetime(2027, 1, 1)
