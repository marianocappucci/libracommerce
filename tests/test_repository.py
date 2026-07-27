import sqlite3
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.db.schema import init_schema
from libracommerce.domain.catalog import (
    CatalogItem,
    CatalogItemType,
    ItemCode,
    ItemCodeType,
    ItemPrice,
    ItemVariant,
    PriceList,
    Unit,
)
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


def test_save_item_code_assigns_id_and_lists_by_item(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))

    saved = repo.save_item_code(ItemCode(None, item.id, ItemCodeType.BARCODE, "7791234567890", is_primary=True))
    assert saved.id is not None

    codes = repo.list_item_codes(item.id)
    assert codes == (saved,) or list(codes) == [saved]


def test_find_item_by_code_resolves_the_catalog_item(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    repo.save_item_code(ItemCode(None, item.id, ItemCodeType.BARCODE, "7791234567890"))

    found = repo.find_item_by_code("7791234567890")
    assert found is not None
    assert found.id == item.id
    assert found.name == "Yerba"


def test_find_item_by_code_returns_none_for_unknown_code(repo: SqliteCommerceRepository):
    assert repo.find_item_by_code("nope") is None


def test_save_item_code_rejects_duplicate_code_within_same_type(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    other = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Azucar", _kg()))
    repo.save_item_code(ItemCode(None, item.id, ItemCodeType.BARCODE, "7791234567890"))

    with pytest.raises(sqlite3.IntegrityError):
        repo.save_item_code(ItemCode(None, other.id, ItemCodeType.BARCODE, "7791234567890"))


def _unit_u() -> Unit:
    return Unit("u", "Unidad")


def test_save_item_variant_assigns_id_and_round_trips(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Remera", _unit_u()))

    saved = repo.save_item_variant(
        ItemVariant(None, item.id, "REM-M-AZUL", "M / Azul", attributes={"talle": "M", "color": "azul"})
    )
    assert saved.id is not None
    assert repo.get_item_variant(saved.id) == saved

    variants = repo.list_item_variants(item.id)
    assert list(variants) == [saved]


def test_save_item_variant_rejects_duplicate_sku(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Remera", _unit_u()))
    repo.save_item_variant(ItemVariant(None, item.id, "REM-M-AZUL", "M / Azul"))

    with pytest.raises(sqlite3.IntegrityError):
        repo.save_item_variant(ItemVariant(None, item.id, "REM-M-AZUL", "M / Azul otra vez"))


def test_current_stock_is_tracked_independently_per_variant(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Remera", _unit_u()))
    variant_m = repo.save_item_variant(ItemVariant(None, item.id, "REM-M", "M"))
    variant_l = repo.save_item_variant(ItemVariant(None, item.id, "REM-L", "L"))
    location = repo.save_location(Location(None, "Deposito Central"))

    repo.append_stock_movement(
        StockMovement(
            None, item.id, location.id, StockMovementType.PURCHASE, Decimal("10"), datetime.now(),
            variant_id=variant_m.id,
        )
    )
    repo.append_stock_movement(
        StockMovement(
            None, item.id, location.id, StockMovementType.PURCHASE, Decimal("5"), datetime.now(),
            variant_id=variant_l.id,
        )
    )

    assert repo.current_stock(item.id, location.id, variant_id=variant_m.id) == Decimal("10")
    assert repo.current_stock(item.id, location.id, variant_id=variant_l.id) == Decimal("5")
    assert repo.current_stock(item.id, location.id) == Decimal("0")


def test_save_price_list_assigns_id_and_round_trips(repo: SqliteCommerceRepository):
    saved = repo.save_price_list(PriceList(None, "Mayorista", is_default=True))
    assert saved.id is not None
    # `created_at` lo pone el DEFAULT CURRENT_TIMESTAMP del schema -- save_price_list
    # no lo conoce (el dataclass que se le pasa nunca lo trae), solo get_price_list
    # lo devuelve real. Se compara todo lo demas y se confirma que quedo seteado.
    fetched = repo.get_price_list(saved.id)
    assert fetched == replace(saved, created_at=fetched.created_at)
    assert fetched.created_at is not None


def test_save_price_list_rejects_second_default(repo: SqliteCommerceRepository):
    repo.save_price_list(PriceList(None, "Mayorista", is_default=True))
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_price_list(PriceList(None, "Minorista", is_default=True))


def test_save_item_price_round_trips(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    price_list = repo.save_price_list(PriceList(None, "General"))

    saved = repo.save_item_price(
        ItemPrice(None, item.id, price_list.id, Decimal("1500.50"), valid_from=datetime(2026, 1, 1))
    )
    assert saved.id is not None

    prices = repo.list_item_prices(item.id)
    assert list(prices) == [saved]


def test_resolve_price_returns_none_when_no_price_configured(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    price_list = repo.save_price_list(PriceList(None, "General"))
    assert repo.resolve_price(item.id, price_list_id=price_list.id) is None


def test_resolve_price_uses_default_price_list_when_not_specified(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    price_list = repo.save_price_list(PriceList(None, "General", is_default=True))
    repo.save_item_price(
        ItemPrice(None, item.id, price_list.id, Decimal("1500"), valid_from=datetime(2026, 1, 1))
    )

    assert repo.resolve_price(item.id, at=datetime(2026, 6, 1)) == Decimal("1500")


def test_resolve_price_respects_validity_window(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    price_list = repo.save_price_list(PriceList(None, "General"))
    repo.save_item_price(
        ItemPrice(
            None, item.id, price_list.id, Decimal("1500"),
            valid_from=datetime(2026, 1, 1), valid_until=datetime(2026, 3, 1),
        )
    )

    assert repo.resolve_price(item.id, price_list_id=price_list.id, at=datetime(2026, 2, 1)) == Decimal("1500")
    assert repo.resolve_price(item.id, price_list_id=price_list.id, at=datetime(2026, 4, 1)) is None
    assert repo.resolve_price(item.id, price_list_id=price_list.id, at=datetime(2025, 12, 1)) is None


def test_resolve_price_applies_quantity_break(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    price_list = repo.save_price_list(PriceList(None, "General"))
    repo.save_item_price(
        ItemPrice(None, item.id, price_list.id, Decimal("1500"), valid_from=datetime(2026, 1, 1))
    )
    repo.save_item_price(
        ItemPrice(
            None, item.id, price_list.id, Decimal("1300"),
            valid_from=datetime(2026, 1, 1), min_quantity=Decimal("10"),
        )
    )

    assert repo.resolve_price(item.id, price_list_id=price_list.id, quantity=Decimal("3")) == Decimal("1500")
    assert repo.resolve_price(item.id, price_list_id=price_list.id, quantity=Decimal("10")) == Decimal("1300")


def test_resolve_price_prefers_branch_specific_over_general(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    price_list = repo.save_price_list(PriceList(None, "General"))
    repo.save_item_price(
        ItemPrice(None, item.id, price_list.id, Decimal("1500"), valid_from=datetime(2026, 1, 1))
    )
    repo.save_item_price(
        ItemPrice(
            None, item.id, price_list.id, Decimal("1600"),
            valid_from=datetime(2026, 1, 1), branch_id=2,
        )
    )

    assert repo.resolve_price(item.id, price_list_id=price_list.id, branch_id=2) == Decimal("1600")
    assert repo.resolve_price(item.id, price_list_id=price_list.id, branch_id=1) == Decimal("1500")


def test_save_location_round_trips(repo: SqliteCommerceRepository):
    location = Location(None, "Deposito Central", branch_id=1, location_type="warehouse")
    saved = repo.save_location(location)
    assert saved.id is not None
    assert repo.get_location(saved.id) == saved


def test_save_location_round_trips_description_and_default(repo: SqliteCommerceRepository):
    location = Location(None, "Principal", description="Deposito por defecto", is_default=True)
    saved = repo.save_location(location)
    assert repo.get_location(saved.id) == saved


def test_list_locations_puts_the_default_first_and_can_filter_inactive(
    repo: SqliteCommerceRepository,
):
    repo.save_location(Location(None, "Zzz Secundario"))
    repo.save_location(Location(None, "Aaa Principal", is_default=True))
    repo.save_location(Location(None, "Baja", active=False))

    names = [loc.name for loc in repo.list_locations()]
    assert names == ["Aaa Principal", "Baja", "Zzz Secundario"]
    assert [loc.name for loc in repo.list_locations(active_only=True)] == [
        "Aaa Principal",
        "Zzz Secundario",
    ]


def test_catalog_item_round_trips_min_stock(repo: SqliteCommerceRepository):
    item = CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg(), min_stock=Decimal("2.5"))
    saved = repo.save_catalog_item(item)
    assert repo.get_catalog_item(saved.id).min_stock == Decimal("2.5")


def test_list_catalog_items_filters_by_type_active_sellable_and_search(
    repo: SqliteCommerceRepository,
):
    repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    repo.save_catalog_item(
        CatalogItem(None, CatalogItemType.PRODUCT, "Yerba vieja", _kg(), active=False)
    )
    repo.save_catalog_item(
        CatalogItem(None, CatalogItemType.PRODUCT, "Insumo interno", _kg(), sellable=False)
    )
    repo.save_catalog_item(CatalogItem(None, CatalogItemType.SERVICE, "Consulta", _kg()))

    assert len(repo.list_catalog_items()) == 4
    assert [i.name for i in repo.list_catalog_items(active_only=True)] == [
        "Consulta",
        "Insumo interno",
        "Yerba",
    ]
    assert [i.name for i in repo.list_catalog_items(sellable_only=True)] == [
        "Consulta",
        "Yerba",
        "Yerba vieja",
    ]
    assert [i.name for i in repo.list_catalog_items(item_type=CatalogItemType.SERVICE)] == [
        "Consulta"
    ]
    assert [i.name for i in repo.list_catalog_items(search="yerba")] == ["Yerba", "Yerba vieja"]


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


def test_save_sale_persists_variant_id(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Remera", _kg()))
    variant = repo.save_item_variant(ItemVariant(None, item.id, "REM-M-AZUL", "M / Azul"))

    line = SaleItem(
        kind=CatalogItemType.PRODUCT,
        item_id=item.id,
        variant_id=variant.id,
        description_snapshot="Remera M / Azul",
        quantity=Decimal("1"),
        unit_price=Decimal("5000"),
    )
    sale = Sale(None, "V-0003", (line,), total=Decimal("5000"))

    saved = repo.save_sale(sale)
    fetched = repo.get_sale(saved.id)

    assert fetched.items[0].variant_id == variant.id


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


def test_list_purchase_orders_returns_newest_first(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    supplier = repo.save_party(Party(None, PartyType.ORGANIZATION, "Distribuidora SA"))
    first = repo.save_purchase_order(
        PurchaseOrder(None, "OC-0010", supplier_party_id=supplier.id, items=(PurchaseOrderItem(item.id, Decimal("5"), Decimal("100")),))
    )
    second = repo.save_purchase_order(
        PurchaseOrder(None, "OC-0011", supplier_party_id=supplier.id, items=(PurchaseOrderItem(item.id, Decimal("5"), Decimal("100")),))
    )

    orders = repo.list_purchase_orders()

    assert [o.id for o in orders] == [second.id, first.id]


def test_list_purchase_receipts_returns_newest_first(repo: SqliteCommerceRepository):
    item = repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", _kg()))
    supplier = repo.save_party(Party(None, PartyType.ORGANIZATION, "Distribuidora SA"))
    first = repo.save_purchase_receipt(
        PurchaseReceipt(None, supplier_party_id=supplier.id, items=(PurchaseReceiptItem(item.id, Decimal("5"), Decimal("100")),))
    )
    second = repo.save_purchase_receipt(
        PurchaseReceipt(None, supplier_party_id=supplier.id, items=(PurchaseReceiptItem(item.id, Decimal("5"), Decimal("100")),))
    )

    receipts = repo.list_purchase_receipts()

    assert [r.id for r in receipts] == [second.id, first.id]
