from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from decimal import Decimal


class CatalogItemType(StrEnum):
    PRODUCT = "product"
    SERVICE = "service"


class ItemCodeType(StrEnum):
    INTERNAL = "internal"
    BARCODE = "barcode"
    SKU = "sku"
    OTHER = "other"


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
    min_stock: Decimal = Decimal("0")


@dataclass(frozen=True)
class ItemCode:
    id: int | None
    item_id: int
    code_type: ItemCodeType
    code: str
    is_primary: bool = False


@dataclass(frozen=True)
class PriceList:
    id: int | None
    name: str
    description: str = ""
    active: bool = True
    is_default: bool = False
    created_at: str | None = None


@dataclass(frozen=True)
class ItemPrice:
    id: int | None
    item_id: int
    price_list_id: int
    amount: Decimal
    valid_from: datetime
    currency: str = "ARS"
    valid_until: datetime | None = None
    min_quantity: Decimal | None = None
    branch_id: int | None = None

    def __post_init__(self):
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until debe ser posterior a valid_from")


@dataclass(frozen=True)
class ItemVariant:
    """Una combinacion concreta de atributos de un item (ej. talle/color en
    ropa). La despensa puede seguir usando un item sin ninguna variante --
    esto es un detalle opcional del catalogo, no un requisito general.
    """

    id: int | None
    item_id: int
    sku: str
    name: str
    attributes: dict[str, str] = field(default_factory=dict)
    active: bool = True
