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

    line = SaleItem(item.id, "Yerba", Decimal("2"), Decimal("1500"), tax_amount=Decimal("315"))
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
    assert fetched.items[0].line_total == Decimal("3315")
    assert fetched.total == Decimal("3315")
