from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from libracommerce.domain.catalog import CatalogItemType


class SaleStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    PARTIALLY_RETURNED = "partially_returned"
    RETURNED = "returned"


@dataclass(frozen=True)
class SaleItem:
    """A sale line. Products must reference a registered CatalogItem;
    services may reference one (pre-loaded, priced) or be entirely
    ad-hoc (item_id=None, free-text description_snapshot only) — a
    professional invoicing a one-off service that was never catalogued.
    """

    kind: CatalogItemType
    description_snapshot: str
    quantity: Decimal
    unit_price: Decimal
    item_id: int | None = None
    variant_id: int | None = None
    discount_amount: Decimal = Decimal("0")
    tax_rate: Decimal = Decimal("0")
    tax_amount: Decimal = Decimal("0")
    unit_cost_snapshot: Decimal | None = None

    def __post_init__(self):
        if self.kind == CatalogItemType.PRODUCT and self.item_id is None:
            raise ValueError(
                "Una línea de producto requiere item_id: a diferencia de los "
                "servicios, las ventas de productos siempre deben referenciar "
                "un CatalogItem registrado."
            )
        if self.variant_id is not None and self.item_id is None:
            raise ValueError(
                "Una línea con variant_id requiere item_id: una variante "
                "siempre pertenece a un item del catálogo."
            )

    @property
    def line_total(self) -> Decimal:
        return self.quantity * self.unit_price - self.discount_amount + self.tax_amount


@dataclass(frozen=True)
class SalePayment:
    """Un cobro de la venta. Son varios cuando el cliente paga mixto — parte
    en efectivo, parte con tarjeta.

    `received_amount` es cuánto entregó el cliente cuando paga en efectivo y
    hay que darle vuelto. Se guarda para que un arqueo con diferencia se
    pueda reconstruir después; en los demás medios va None, porque no existe
    el concepto de entregar de más.
    """

    method: str
    amount: Decimal
    received_amount: Decimal | None = None
    reference: str = ""

    def __post_init__(self):
        if self.amount <= 0:
            raise ValueError("El monto de un pago debe ser mayor que cero.")
        if self.received_amount is not None and self.received_amount < self.amount:
            raise ValueError(
                "Lo recibido no puede ser menor que el monto del pago: "
                f"recibido={self.received_amount}, monto={self.amount}."
            )

    @property
    def change(self) -> Decimal:
        """Vuelto de este pago. Cero cuando no se registró lo recibido, que
        es el caso de todo medio que no sea efectivo."""
        if self.received_amount is None:
            return Decimal("0")
        return self.received_amount - self.amount


@dataclass(frozen=True)
class Sale:
    id: int | None
    number: str
    items: tuple[SaleItem, ...]
    status: SaleStatus = SaleStatus.DRAFT
    customer_party_id: int | None = None
    branch_id: int | None = None
    register_id: int | None = None
    source_type: str = "pos"
    source_id: int | None = None
    subtotal: Decimal = Decimal("0")
    discount_total: Decimal = Decimal("0")
    tax_total: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    confirmed_at: datetime | None = None
    occurred_on: str | None = None
    customer_name_snapshot: str = ""
    created_by: int | None = None
    notes: str = ""
    status_detail: str | None = None
    # Va al final a proposito: agregarlo entre los campos existentes correria
    # el orden posicional del dataclass y le cambiaria el significado a
    # cualquier consumidor que construya Sale() sin keywords.
    payments: tuple[SalePayment, ...] = ()

    def calculated_total(self) -> Decimal:
        return sum((item.line_total for item in self.items), Decimal("0"))

    def paid_total(self) -> Decimal:
        """Suma de los cobros registrados. No incluye el vuelto: lo que el
        cliente entregó de más no es plata de la venta."""
        return sum((payment.amount for payment in self.payments), Decimal("0"))

    def change_due(self) -> Decimal:
        """Vuelto total a devolver."""
        return sum((payment.change for payment in self.payments), Decimal("0"))

    def is_fully_paid(self) -> bool:
        return self.payments != () and self.paid_total() >= self.total


_CENTAVO = Decimal("0.01")


def _a_centavos(valor: Decimal) -> Decimal:
    return valor.quantize(_CENTAVO, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LineaReintegro:
    """Lo que `reintegro_prorrateado` necesita saber de una línea de la venta.

    `clave` es lo que identifica a la unidad que se devuelve: `(ítem, variante)`
    en el camino del ERP, donde el ledger no dice de qué línea volvió lo ya
    devuelto, o la posición de la línea en `usecases.sales`, donde sí lo dice.
    `None` marca una línea que nunca se devuelve (un servicio): entra en la
    suma de la venta, porque carga descuento, pero no tiene unidades que valuar.
    """

    clave: Hashable | None
    quantity: Decimal
    unit_price: Decimal
    discount_amount: Decimal = Decimal("0")


def reintegro_prorrateado(
    lineas: Iterable[LineaReintegro],
    descuento_venta: Decimal,
    ya_devuelto: Mapping[Hashable, Decimal],
    pedido: Mapping[Hashable, Decimal],
) -> Decimal:
    """Cuánto se reintegra por `pedido` (unidades por clave) cuando antes ya volvió `ya_devuelto`: lo
    **efectivamente pagado** por esas unidades, no su precio de lista (ADR-040, ADR-043).

    Es la cuenta única de las dos devoluciones del motor: `erp.ventas.devolver_items` (filas SQL) y
    `usecases.sales.return_sale_items` (dominio) la llaman con las mismas reglas.

    Misma semántica de totales que `erp.margen`: el neto de una línea es `quantity*unit_price - discount_amount`; del
    descuento de la venta sólo cuenta lo que las líneas no explican (`max(discount_total - Σ discount_amount, 0)`, con
    tope en lo que vale la venta), porque el descuento puede estar en la línea Y en `discount_total`, que es el mismo
    dinero; y lo cobrable es `Σ netos - descuento de la venta`. Cada unidad vale su parte de eso: `neto de la clave /
    unidades de la clave`, por `cobrable / Σ netos`. Las líneas de servicio entran en la suma pero nunca se devuelven.

    🔑 **Diferencia de acumulados, no suma de importes redondeados**: `redondeo(prorrateo(ya + pedido)) -
    redondeo(prorrateo(ya))`. Redondear cada devolución por separado deriva centavos (3 × $100 con $10 de descuento
    dan 96,67 + 96,67 + 96,67 = 290,01 sobre $290); así las devoluciones parciales suman EXACTO lo cobrado cuando
    vuelve todo. Sin descuentos el factor es 1 y da `cantidad*unit_price`, como siempre.

    Levanta `ValueError` si el acumulado superara lo cobrado. No suma IVA (`tax_total` es 0 en el mostrador)."""
    lineas = tuple(lineas)
    netos = [li.quantity * li.unit_price - li.discount_amount for li in lineas]
    base = sum(netos, Decimal(0))
    en_lineas = sum((li.discount_amount for li in lineas), Decimal(0))
    de_la_venta = min(max(descuento_venta - en_lineas, Decimal(0)), max(base, Decimal(0)))
    cobrable = base - de_la_venta
    if base <= 0:
        return Decimal(0)

    neto_clave: dict[Hashable, Decimal] = {}
    unidades_clave: dict[Hashable, Decimal] = {}
    for li, neto in zip(lineas, netos, strict=True):
        if li.clave is not None:
            neto_clave[li.clave] = neto_clave.get(li.clave, Decimal(0)) + neto
            unidades_clave[li.clave] = unidades_clave.get(li.clave, Decimal(0)) + li.quantity

    def bruto(unidades: Mapping[Hashable, Decimal]) -> Decimal:
        return sum(
            (n * neto_clave[k] / unidades_clave[k]
             for k, n in unidades.items() if k in neto_clave and unidades_clave[k] > 0),
            Decimal(0),
        )

    antes = bruto(ya_devuelto)
    despues = antes + bruto(pedido)
    acumulado = _a_centavos(cobrable * despues / base)
    if acumulado > _a_centavos(cobrable):
        raise ValueError(
            f"el reintegro acumulado ({acumulado}) superaría lo cobrado por la venta ({_a_centavos(cobrable)})"
        )
    return acumulado - _a_centavos(cobrable * antes / base)
