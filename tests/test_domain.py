from datetime import datetime
from decimal import Decimal

from libracommerce.domain.catalog import CatalogItem, CatalogItemType, Unit
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
    item = SaleItem(1, "Producto", Decimal("2"), Decimal("100"), tax_amount=Decimal("21"))
    sale = Sale(None, "V-1", (item,))
    assert sale.calculated_total() == Decimal("221")


def test_sale_defaults_to_draft_status():
    sale = Sale(None, "V-1", ())
    assert sale.status == SaleStatus.DRAFT
    assert sale.calculated_total() == Decimal("0")
