"""Orchestrate what a confirmed sale means beyond persisting the sale row.

Cash/register movements are deliberately out of scope here: caja lives in
LibraCore's bounded context (see arquitectura-familia-libra-alcance.md),
not LibraCommerce's. This use case only owns the stock side.
"""

from dataclasses import replace
from datetime import datetime

from libracommerce.domain.catalog import CatalogItemType
from libracommerce.domain.inventory import StockMovement, StockMovementType
from libracommerce.domain.sales import Sale, SaleStatus
from libracommerce.ports.persistence import CommerceRepository
from libracommerce.usecases.inventory import verificar_deposito, verificar_disponibilidad


def confirm_sale(
    repo: CommerceRepository,
    sale: Sale,
    location_id: int,
    occurred_at: datetime,
    *,
    validar_stock: bool = False,
) -> Sale:
    """Confirm a draft sale and append an outbound stock movement per product line.

    Service lines never move stock. Persists the sale first so stock
    movements can reference its id as source_id, whether the sale already
    existed or is being confirmed on first save.

    `validar_stock` decide si una linea que deja el deposito en negativo
    aborta la venta entera (`StockInsuficienteError`) o se graba igual.

    **El default es `False`, y no es una preferencia de diseno: es
    compatibilidad.** Los tres consumidores en produccion vienen vendiendo sin
    esta validacion, y en un mostrador negarse a cobrar porque el inventario
    esta mal cargado es peor que quedar en negativo: el cliente ya tiene el
    producto en la mano. El vertical que sí quiera bloquear lo pide.

    Cuando se valida, las lecturas y las escrituras van en una sola
    transaccion: si la tercera linea no tiene stock no queda grabada ni la
    venta ni el movimiento de las dos primeras.

    `location_id` tiene que existir y estar activo (`DepositoInexistente`, un
    `ValueError`; ADR-051), y se valida antes de guardar la venta.
    """
    if sale.status != SaleStatus.DRAFT:
        raise ValueError(
            f"Solo se puede confirmar una venta en estado draft (actual: {sale.status})"
        )
    verificar_deposito(repo, location_id)
    if not validar_stock:
        return _confirmar(repo, sale, location_id, occurred_at)
    with repo.transaction():
        return _confirmar(repo, sale, location_id, occurred_at, validar_stock=True)


def _confirmar(
    repo: CommerceRepository,
    sale: Sale,
    location_id: int,
    occurred_at: datetime,
    *,
    validar_stock: bool = False,
) -> Sale:
    saved = repo.save_sale(replace(sale, status=SaleStatus.CONFIRMED, confirmed_at=occurred_at))
    for line in saved.items:
        if line.kind != CatalogItemType.PRODUCT:
            continue
        if validar_stock:
            verificar_disponibilidad(
                repo, line.item_id, location_id, line.quantity, variant_id=line.variant_id
            )
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

    No valida el depósito (ADR-051): repone en el de donde salió cada movimiento,
    que pudo darse de baja después de la venta, y negarse la dejaría sin poder anularse.
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
