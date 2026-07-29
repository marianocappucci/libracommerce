"""Orchestrate what a confirmed sale means beyond persisting the sale row.

Cash/register movements are deliberately out of scope here: caja lives in
LibraCore's bounded context (see arquitectura-familia-libra-alcance.md),
not LibraCommerce's. This use case only owns the stock side.
"""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from libracommerce.domain.catalog import CatalogItemType
from libracommerce.domain.inventory import StockMovement, StockMovementType
from libracommerce.domain.sales import Sale, SaleStatus
from libracommerce.ports.persistence import CommerceRepository


def confirm_sale(
    repo: CommerceRepository, sale: Sale, location_id: int, occurred_at: datetime
) -> Sale:
    """Confirm a draft sale and append an outbound stock movement per product line.

    Service lines never move stock. Persists the sale first so stock
    movements can reference its id as source_id, whether the sale already
    existed or is being confirmed on first save.
    """
    if sale.status != SaleStatus.DRAFT:
        raise ValueError(
            f"Solo se puede confirmar una venta en estado draft (actual: {sale.status})"
        )
    saved = repo.save_sale(replace(sale, status=SaleStatus.CONFIRMED, confirmed_at=occurred_at))
    for line in saved.items:
        if line.kind != CatalogItemType.PRODUCT:
            continue
        repo.append_stock_movement(
            StockMovement(
                id=None,
                item_id=line.item_id,
                variant_id=line.variant_id,
                location_id=location_id,
                movement_type=StockMovementType.SALE,
                quantity_delta=-line.quantity,
                occurred_at=occurred_at,
                source_type="sale",
                source_id=saved.id,
            )
        )
    return saved


def cancel_sale(
    repo: CommerceRepository, sale: Sale, occurred_at: datetime
) -> Sale:
    """Anula una venta confirmada reponiendo todo el stock que descontó.

    La reposición se hace **invirtiendo los movimientos que la venta generó**,
    leídos del ledger, y no recalculándolos desde las líneas: lo que salió
    del depósito puede no ser el producto vendido (los insumos de una receta,
    en gastronomía), y el ledger ya lo sabe.

    Es idempotente: anular una venta ya anulada no vuelve a mover stock. Sin
    eso, un reintento del botón duplicaría la reposición y dejaría stock
    inventado.

    La caja queda deliberadamente afuera, igual que en `confirm_sale`: el
    dinero vive en el contexto de LibraCore, no en el de LibraCommerce.
    """
    if sale.status == SaleStatus.CANCELLED:
        return sale
    if sale.status != SaleStatus.CONFIRMED:
        raise ValueError(
            f"Solo se puede anular una venta confirmada (actual: {sale.status})"
        )

    for movimiento in repo.list_stock_movements_by_source("sale", sale.id):
        if movimiento.movement_type != StockMovementType.SALE:
            # Segunda red además del guard de estado: la reversión que esta
            # misma función escribe queda con el mismo `source_id`, y sin
            # este filtro una anulación repetida sobre una venta cuyo estado
            # no llegó a guardarse revertiría la reversión.
            continue
        repo.append_stock_movement(
            replace(
                movimiento,
                id=None,
                movement_type=StockMovementType.RETURN,
                quantity_delta=-movimiento.quantity_delta,
                occurred_at=occurred_at,
                reason_code="anulacion",
            )
        )
    return repo.save_sale(replace(sale, status=SaleStatus.CANCELLED))


def return_sale_items(
    repo: CommerceRepository,
    sale: Sale,
    devoluciones: dict[int, Decimal],
    location_id: int,
    occurred_at: datetime,
) -> tuple[Sale, Decimal]:
    """Devuelve algunas líneas de una venta confirmada.

    `devoluciones` mapea la POSICIÓN de la línea a la cantidad que vuelve —
    posición y no id porque `SaleItem` no tiene id propio (`save_sale`
    reinserta las líneas en cada guardado), mismo criterio que
    `remove_item`/`set_item_quantity` del POS.

    Devuelve la venta actualizada y **cuánta plata hay que reintegrar**, que
    es lo que el producto necesita para mover la caja: eso no se calcula acá
    porque el dinero no es de este contexto.

    La venta queda en `RETURNED` si volvió todo y en `PARTIALLY_RETURNED` si
    volvió una parte, así que el historial distingue "el cliente devolvió una
    cosa" de "esta venta no existió".
    """
    if sale.status not in (SaleStatus.CONFIRMED, SaleStatus.PARTIALLY_RETURNED):
        raise ValueError(
            f"Solo se puede devolver sobre una venta confirmada (actual: {sale.status})"
        )
    if not devoluciones:
        raise ValueError("no se indicó ninguna línea a devolver")

    ya_devuelto = _devuelto_por_linea(repo, sale)
    importe = Decimal("0")

    for indice, cantidad in devoluciones.items():
        if indice < 0 or indice >= len(sale.items):
            raise ValueError(f"la venta no tiene una línea en la posición {indice}")
        if cantidad <= 0:
            raise ValueError("la cantidad a devolver debe ser mayor que cero")
        linea = sale.items[indice]
        if linea.kind != CatalogItemType.PRODUCT:
            # Cuánto se devolvió de cada línea se lleva en el ledger de
            # stock, y un servicio no deja rastro ahí: se podría devolver el
            # mismo servicio infinitas veces sin que nada lo frene. Antes que
            # reintegrar plata sin control, se rechaza.
            raise ValueError(
                f"no se puede devolver una línea de servicio "
                f"({linea.description_snapshot}): anular la venta entera"
            )
        disponible = linea.quantity - ya_devuelto.get(indice, Decimal("0"))
        if cantidad > disponible:
            # Sin este control se podría devolver diez veces lo que se
            # vendió una, inventando stock y reintegrando plata que nunca
            # entró.
            raise ValueError(
                f"no se puede devolver {cantidad} de {linea.description_snapshot}: "
                f"quedan {disponible} sin devolver"
            )

        repo.append_stock_movement(
            StockMovement(
                id=None,
                item_id=linea.item_id,
                variant_id=linea.variant_id,
                location_id=location_id,
                movement_type=StockMovementType.RETURN,
                quantity_delta=cantidad,
                occurred_at=occurred_at,
                source_type="sale_return",
                source_id=sale.id,
                reason_code=str(indice),
            )
        )
        importe += cantidad * linea.unit_price

    devuelto_total = sum(
        (ya_devuelto.get(i, Decimal("0")) + devoluciones.get(i, Decimal("0"))
         for i in range(len(sale.items))),
        Decimal("0"),
    )
    vendido_total = sum((linea.quantity for linea in sale.items), Decimal("0"))
    estado = (
        SaleStatus.RETURNED if devuelto_total >= vendido_total
        else SaleStatus.PARTIALLY_RETURNED
    )
    return repo.save_sale(replace(sale, status=estado)), importe


def _devuelto_por_linea(repo: CommerceRepository, sale: Sale) -> dict[int, Decimal]:
    """Cuánto se devolvió ya de cada línea, según el ledger de stock.

    La posición de la línea viaja en `reason_code` del movimiento: no hay
    dónde más guardarla sin agregarle una tabla propia a las devoluciones, y
    el ledger es de todos modos la fuente de verdad de lo que volvió al
    depósito.
    """
    devuelto: dict[int, Decimal] = {}
    for movimiento in repo.list_stock_movements_by_source("sale_return", sale.id):
        if movimiento.reason_code is None or not movimiento.reason_code.isdigit():
            continue
        indice = int(movimiento.reason_code)
        devuelto[indice] = devuelto.get(indice, Decimal("0")) + movimiento.quantity_delta
    return devuelto
