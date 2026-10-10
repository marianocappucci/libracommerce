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
from libracommerce.domain.sales import LineaReintegro, Sale, SaleStatus, reintegro_prorrateado
from libracommerce.ports.persistence import CommerceRepository
from libracommerce.usecases.inventory import verificar_disponibilidad


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
    """
    if sale.status != SaleStatus.DRAFT:
        raise ValueError(
            f"Solo se puede confirmar una venta en estado draft (actual: {sale.status})"
        )
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
    es lo que el producto necesita para mover la caja (mover la caja no se
    hace acá: el dinero no es de este contexto).

    🔑 **El importe es lo efectivamente pagado, no el precio de lista**
    (ADR-043): `reintegro_prorrateado` reparte el descuento de la venta
    (`discount_total`) y el de cada línea (`discount_amount`) en proporción a
    lo que vale cada unidad, con la misma cuenta que `erp.ventas.devolver_items`
    (ADR-040). Es una diferencia de acumulados, así que devoluciones sucesivas
    suman EXACTO lo cobrado, y una guarda impide reintegrar de más. Como el
    ledger de acá sí dice de qué línea volvió lo ya devuelto, cada línea se
    valúa por separado (con una sola línea por producto, o a igual precio, da
    lo mismo que el camino del ERP). Sin descuentos es `cantidad * unit_price`.

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

    # Primero se valida todo y se calcula el importe (la guarda del prorrateo
    # puede levantar), y recién después se escribe en el ledger: un pedido
    # rechazado no deja stock repuesto a medias.
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

    importe = reintegro_prorrateado(
        [
            # La clave es la posición: cada línea vale lo suyo. Un servicio no
            # se devuelve (clave `None`) pero entra en la suma de la venta.
            LineaReintegro(
                indice if linea.kind == CatalogItemType.PRODUCT else None,
                linea.quantity, linea.unit_price, linea.discount_amount,
            )
            for indice, linea in enumerate(sale.items)
        ],
        sale.discount_total,
        ya_devuelto,
        devoluciones,
    )

    for indice, cantidad in devoluciones.items():
        linea = sale.items[indice]
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
