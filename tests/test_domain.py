from datetime import datetime
from decimal import Decimal

import pytest

from libracommerce.domain.catalog import CatalogItem, CatalogItemType, ItemPrice, ItemVariant, Unit
from libracommerce.domain.entities import Party, PartyRole, PartyType
from libracommerce.domain.inventory import Location, StockMovement, StockMovementType
from libracommerce.domain.sales import Sale, SaleItem, SaleStatus


def test_party_and_catalog_item_are_product_agnostic():
    party = Party(None, PartyType.PERSON, "Ana")
    item = CatalogItem(None, CatalogItemType.PRODUCT, "Yerba", Unit("kg", "Kilogramo", True, 3))
    assert party.display_name == "Ana"
    assert item.sellable


def test_party_roles_are_plain_string_values():
    assert PartyRole.CUSTOMER == "customer"
    assert PartyRole.SUPPLIER == "supplier"


def test_stock_movement_signs_are_explicit():
    inbound = StockMovement(None, 1, 1, StockMovementType.PURCHASE, Decimal("10"), datetime.now())
    outbound = StockMovement(None, 1, 1, StockMovementType.SALE, Decimal("-2"), datetime.now())
    assert inbound.is_inbound()
    assert outbound.is_outbound()


def test_location_defaults_to_active_warehouse():
    location = Location(None, "Deposito Central")
    assert location.active
    assert location.location_type == "warehouse"


def test_sale_total_uses_line_snapshots():
    item = SaleItem(
        kind=CatalogItemType.PRODUCT,
        item_id=1,
        description_snapshot="Producto",
        quantity=Decimal("2"),
        unit_price=Decimal("100"),
        tax_amount=Decimal("21"),
    )
    sale = Sale(None, "V-1", (item,))
    assert sale.calculated_total() == Decimal("221")


def test_sale_defaults_to_draft_status():
    sale = Sale(None, "V-1", ())
    assert sale.status == SaleStatus.DRAFT
    assert sale.calculated_total() == Decimal("0")


def test_product_sale_item_requires_item_id():
    with pytest.raises(ValueError):
        SaleItem(
            kind=CatalogItemType.PRODUCT,
            item_id=None,
            description_snapshot="Yerba",
            quantity=Decimal("1"),
            unit_price=Decimal("1500"),
        )


def test_service_sale_item_can_be_ad_hoc():
    item = SaleItem(
        kind=CatalogItemType.SERVICE,
        item_id=None,
        description_snapshot="Consulta fuera de catálogo",
        quantity=Decimal("1"),
        unit_price=Decimal("5000"),
    )
    assert item.item_id is None
    assert item.line_total == Decimal("5000")


def test_service_sale_item_can_reference_a_catalog_service():
    item = SaleItem(
        kind=CatalogItemType.SERVICE,
        item_id=7,
        description_snapshot="Corte de pelo",
        quantity=Decimal("1"),
        unit_price=Decimal("3000"),
    )
    assert item.item_id == 7


def test_item_price_rejects_valid_until_not_after_valid_from():
    with pytest.raises(ValueError):
        ItemPrice(
            id=None,
            item_id=1,
            price_list_id=1,
            amount=Decimal("100"),
            valid_from=datetime(2026, 1, 1),
            valid_until=datetime(2026, 1, 1),
        )


def test_item_variant_carries_attributes():
    variant = ItemVariant(None, 1, "REM-M-AZUL", "M / Azul", attributes={"talle": "M", "color": "azul"})
    assert variant.attributes == {"talle": "M", "color": "azul"}
    assert variant.active


def test_sale_item_with_variant_requires_item_id():
    with pytest.raises(ValueError):
        SaleItem(
            kind=CatalogItemType.SERVICE,
            item_id=None,
            variant_id=5,
            description_snapshot="No debería poder pasar esto",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
        )
