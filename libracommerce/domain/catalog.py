from dataclasses import dataclass, field
from enum import StrEnum
from decimal import Decimal


class CatalogItemType(StrEnum):
    PRODUCT = "product"
    SERVICE = "service"


@dataclass(frozen=True)
class Category:
    id: int | None
    name: str
    parent_id: int | None = None
    active: bool = True


@dataclass(frozen=True)
class Unit:
    code: str
    name: str
    allows_fraction: bool = False
    decimal_scale: int = 0


@dataclass(frozen=True)
class CatalogItem:
    id: int | None
    item_type: CatalogItemType
    name: str
    unit: Unit
    category_id: int | None = None
    description: str = ""
    active: bool = True
    sellable: bool = True
    purchasable: bool = True
    tax_profile: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    default_sale_price: Decimal = Decimal("0")
    default_cost: Decimal = Decimal("0")
