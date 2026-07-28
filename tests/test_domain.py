from datetime import datetime
from decimal import Decimal

import pytest

from libracommerce.domain.catalog import CatalogItem, CatalogItemType, ItemPrice, ItemVariant, Unit
from libracommerce.domain.entities import Party, PartyRole, PartyType
from libracommerce.domain.inventory import Location, StockMovement, StockMovementType
from libracommerce.domain.sales import Sale, SaleItem, SaleStatus, SalePayment


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


# --- cobro de la venta (pago mixto + vuelto) ----------------------------


def test_payment_rejects_zero_or_negative_amount():
    with pytest.raises(ValueError):
        SalePayment(method="efectivo", amount=Decimal("0"))
    with pytest.raises(ValueError):
        SalePayment(method="efectivo", amount=Decimal("-100"))


def test_payment_rejects_received_less_than_amount():
    """Recibir menos que el monto del pago no es un vuelto negativo: es un
    dato mal cargado."""
    with pytest.raises(ValueError):
        SalePayment(method="efectivo", amount=Decimal("1000"), received_amount=Decimal("900"))


def test_change_is_what_was_received_minus_the_payment():
    pago = SalePayment(method="efectivo", amount=Decimal("12450"), received_amount=Decimal("15000"))
    assert pago.change == Decimal("2550")


def test_change_is_zero_for_methods_without_received_amount():
    assert SalePayment(method="tarjeta_debito", amount=Decimal("5000")).change == Decimal("0")


def _venta_con_pagos(*pagos, total="10000"):
    linea = SaleItem(
        kind=CatalogItemType.PRODUCT, item_id=1, description_snapshot="X",
        quantity=Decimal("1"), unit_price=Decimal(total),
    )
    return Sale(None, "V-1", (linea,), total=Decimal(total), payments=tuple(pagos))


def test_paid_total_ignores_the_change():
    """Lo que el cliente entrego de mas no es plata de la venta: 15.000
    entregados sobre 10.000 siguen siendo 10.000 cobrados."""
    venta = _venta_con_pagos(
        SalePayment(method="efectivo", amount=Decimal("10000"), received_amount=Decimal("15000")),
    )
    assert venta.paid_total() == Decimal("10000")
    assert venta.change_due() == Decimal("5000")
    assert venta.is_fully_paid()


def test_mixed_payment_adds_up():
    venta = _venta_con_pagos(
        SalePayment(method="efectivo", amount=Decimal("4000"), received_amount=Decimal("5000")),
        SalePayment(method="tarjeta_debito", amount=Decimal("6000")),
    )
    assert venta.paid_total() == Decimal("10000")
    assert venta.change_due() == Decimal("1000")
    assert venta.is_fully_paid()


def test_partial_payment_is_not_fully_paid():
    venta = _venta_con_pagos(SalePayment(method="efectivo", amount=Decimal("6000")))
    assert not venta.is_fully_paid()


def test_sale_without_payments_is_not_fully_paid_even_if_total_is_zero():
    """Una venta en cero sin cobro registrado no esta paga: esta sin cobrar."""
    venta = Sale(None, "V-1", ())
    assert not venta.is_fully_paid()
