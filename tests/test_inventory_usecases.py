"""Transferencia entre depositos y guarda de disponibilidad.

Las dos piezas que el motor no tenia y cada consumidor resolvia por su
cuenta. Los tests que importan de verdad son los tres de atomicidad: son la
diferencia entre esto y la version de Contalibra que se subio.
"""

import sqlite3
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal

import pytest

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.db.schema import init_schema
from libracommerce.domain.catalog import CatalogItem, CatalogItemType, Unit
from libracommerce.domain.inventory import Location, StockMovement, StockMovementType
from libracommerce.domain.sales import Sale, SaleItem
from libracommerce.usecases.inventory import (
    StockInsuficienteError,
    transfer_stock,
    verificar_disponibilidad,
)
from libracommerce.usecases.sales import confirm_sale

WHEN = datetime(2026, 8, 11, 10, 0, 0)


@pytest.fixture
def repo() -> SqliteCommerceRepository:
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    return SqliteCommerceRepository(conn)


def _producto(repo) -> CatalogItem:
    return repo.save_catalog_item(
        CatalogItem(None, CatalogItemType.PRODUCT, "Plug RJ45", Unit("u", "Unidad"))
    )


def _deposito(repo, nombre: str) -> Location:
    return repo.save_location(Location(None, nombre))


def _cargar(repo, item, location, cantidad) -> None:
    """Existencia inicial por compra, que es como entra el stock de verdad."""
    repo.append_stock_movement(
        StockMovement(
            None, item.id, location.id, StockMovementType.PURCHASE, Decimal(cantidad), WHEN
        )
    )


# ── Transferencia, camino feliz ──────────────────────────────────────────


def test_transferir_mueve_el_stock_de_un_deposito_al_otro(repo):
    item, central, camioneta = _producto(repo), _deposito(repo, "Central"), _deposito(repo, "Kangoo")
    _cargar(repo, item, central, 189)

    transfer_stock(
        repo,
        item_id=item.id,
        from_location_id=central.id,
        to_location_id=camioneta.id,
        quantity=Decimal("40"),
        occurred_at=WHEN,
    )

    assert repo.current_stock(item.id, central.id) == Decimal("149")
    assert repo.current_stock(item.id, camioneta.id) == Decimal("40")


def test_la_entrada_apunta_a_la_salida_y_el_par_se_puede_recuperar(repo):
    item, central, camioneta = _producto(repo), _deposito(repo, "Central"), _deposito(repo, "Kangoo")
    _cargar(repo, item, central, 100)

    salida, entrada = transfer_stock(
        repo,
        item_id=item.id,
        from_location_id=central.id,
        to_location_id=camioneta.id,
        quantity=Decimal("10"),
        occurred_at=WHEN,
    )

    assert salida.movement_type == StockMovementType.TRANSFER_OUT
    assert entrada.movement_type == StockMovementType.TRANSFER_IN
    assert entrada.source_id == salida.id
    # Sin tabla de transferencias, esta es la unica forma de reconstruir el par.
    contraparte = repo.list_stock_movements_by_source("transfer", salida.id)
    assert [m.id for m in contraparte] == [entrada.id]


def test_cada_pata_lleva_su_propio_reason_code(repo):
    """Un consumidor tiene que poder conservar su vocabulario.

    Contalibra muestra `COALESCE(reason_code, movement_type)` **sin mapa** en
    su pantalla de actividad: sin esto, adoptar este caso de uso le cambiaria
    'transferencia_salida' por 'transfer_out' en una pantalla que el cliente
    usa, y ningun test suyo lo notaria.
    """
    item, central, camioneta = _producto(repo), _deposito(repo, "Central"), _deposito(repo, "Kangoo")
    _cargar(repo, item, central, 50)

    salida, entrada = transfer_stock(
        repo,
        item_id=item.id,
        from_location_id=central.id,
        to_location_id=camioneta.id,
        quantity=Decimal("5"),
        occurred_at=WHEN,
        reason_code_salida="transferencia_salida",
        reason_code_entrada="transferencia_entrada",
    )

    assert salida.reason_code == "transferencia_salida"
    assert entrada.reason_code == "transferencia_entrada"
    # Y persistido, no solo en el objeto que devuelve la funcion.
    persistidos = list(repo.list_stock_movements(item.id, central.id)) + list(
        repo.list_stock_movements(item.id, camioneta.id)
    )
    guardados = {m.movement_type: m.reason_code for m in persistidos}
    assert guardados[StockMovementType.TRANSFER_OUT] == "transferencia_salida"
    assert guardados[StockMovementType.TRANSFER_IN] == "transferencia_entrada"


def test_sin_reason_code_las_dos_patas_quedan_en_none(repo):
    """El default no inventa un vocabulario propio del motor."""
    item, central, camioneta = _producto(repo), _deposito(repo, "Central"), _deposito(repo, "Kangoo")
    _cargar(repo, item, central, 50)

    salida, entrada = transfer_stock(
        repo,
        item_id=item.id,
        from_location_id=central.id,
        to_location_id=camioneta.id,
        quantity=Decimal("5"),
        occurred_at=WHEN,
    )

    assert salida.reason_code is None
    assert entrada.reason_code is None


# ── Transferencia, lo que tiene que rechazar ─────────────────────────────


def test_no_transfiere_mas_de_lo_que_hay(repo):
    item, central, camioneta = _producto(repo), _deposito(repo, "Central"), _deposito(repo, "Kangoo")
    _cargar(repo, item, central, 5)

    with pytest.raises(StockInsuficienteError) as excinfo:
        transfer_stock(
            repo,
            item_id=item.id,
            from_location_id=central.id,
            to_location_id=camioneta.id,
            quantity=Decimal("6"),
            occurred_at=WHEN,
        )

    assert excinfo.value.disponible == Decimal("5")
    assert excinfo.value.pedido == Decimal("6")


def test_el_rechazo_no_deja_ningun_movimiento(repo):
    """La guarda tiene que abortar *antes* de escribir, no despues."""
    item, central, camioneta = _producto(repo), _deposito(repo, "Central"), _deposito(repo, "Kangoo")
    _cargar(repo, item, central, 5)

    with pytest.raises(StockInsuficienteError):
        transfer_stock(
            repo,
            item_id=item.id,
            from_location_id=central.id,
            to_location_id=camioneta.id,
            quantity=Decimal("6"),
            occurred_at=WHEN,
        )

    assert repo.current_stock(item.id, central.id) == Decimal("5")
    assert repo.current_stock(item.id, camioneta.id) == Decimal("0")


def test_permitir_negativo_saltea_la_guarda(repo):
    """Para el inventario ya mal cargado, donde la realidad fisica manda."""
    item, central, camioneta = _producto(repo), _deposito(repo, "Central"), _deposito(repo, "Kangoo")

    transfer_stock(
        repo,
        item_id=item.id,
        from_location_id=central.id,
        to_location_id=camioneta.id,
        quantity=Decimal("3"),
        occurred_at=WHEN,
        permitir_negativo=True,
    )

    assert repo.current_stock(item.id, central.id) == Decimal("-3")
    assert repo.current_stock(item.id, camioneta.id) == Decimal("3")


def test_rechaza_cantidad_no_positiva(repo):
    item, central, camioneta = _producto(repo), _deposito(repo, "Central"), _deposito(repo, "Kangoo")
    _cargar(repo, item, central, 10)

    for cantidad in (Decimal("0"), Decimal("-1")):
        with pytest.raises(ValueError, match="positiva"):
            transfer_stock(
                repo,
                item_id=item.id,
                from_location_id=central.id,
                to_location_id=camioneta.id,
                quantity=cantidad,
                occurred_at=WHEN,
            )


def test_rechaza_origen_igual_a_destino(repo):
    """Sin esta guarda seria un no-op con dos filas de ruido en el historial."""
    item, central = _producto(repo), _deposito(repo, "Central")
    _cargar(repo, item, central, 10)

    with pytest.raises(ValueError, match="mismo deposito"):
        transfer_stock(
            repo,
            item_id=item.id,
            from_location_id=central.id,
            to_location_id=central.id,
            quantity=Decimal("1"),
            occurred_at=WHEN,
        )


# ── Atomicidad: el motivo por el que esto no se porto tal cual ───────────


def test_si_falla_la_segunda_escritura_no_queda_la_primera(repo, monkeypatch):
    """El defecto de la version de Contalibra, reproducido y cubierto.

    Alla las dos escrituras van en conexiones distintas: si la segunda falla,
    la mercaderia ya salio del origen y no llego al destino, sin ningun error
    visible despues. Aca la salida tiene que volver atras con la entrada.
    """
    item, central, camioneta = _producto(repo), _deposito(repo, "Central"), _deposito(repo, "Kangoo")
    _cargar(repo, item, central, 100)

    original = repo.append_stock_movement
    llamadas = {"n": 0}

    def falla_en_la_entrada(movement):
        llamadas["n"] += 1
        if llamadas["n"] == 2:
            raise sqlite3.OperationalError("disco lleno")
        return original(movement)

    monkeypatch.setattr(repo, "append_stock_movement", falla_en_la_entrada)

    with pytest.raises(sqlite3.OperationalError):
        transfer_stock(
            repo,
            item_id=item.id,
            from_location_id=central.id,
            to_location_id=camioneta.id,
            quantity=Decimal("40"),
            occurred_at=WHEN,
        )

    assert llamadas["n"] == 2, "la prueba no ejercito la segunda escritura"
    assert repo.current_stock(item.id, central.id) == Decimal("100"), (
        "la salida quedo grabada sin su entrada: se perdio mercaderia"
    )
    assert repo.current_stock(item.id, camioneta.id) == Decimal("0")


def test_control_sin_transaccion_la_mercaderia_se_pierde(repo, monkeypatch):
    """Grupo de control del test de arriba: reproduce el modelo de Contalibra.

    Sin esto, el test anterior podria estar en verde porque la segunda
    escritura nunca corrio, no porque el rollback funcione. Aca se neutraliza
    `transaction()` --que es el modelo viejo: cada escritura commitea sola-- y
    se afirma que **la mercaderia SI se pierde**. Si algun dia este test se
    pone en verde por si solo, el de arriba dejo de medir lo que cree medir.
    """
    item, central, camioneta = _producto(repo), _deposito(repo, "Central"), _deposito(repo, "Kangoo")
    _cargar(repo, item, central, 100)

    monkeypatch.setattr(repo, "transaction", nullcontext)

    original = repo.append_stock_movement
    llamadas = {"n": 0}

    def falla_en_la_entrada(movement):
        llamadas["n"] += 1
        if llamadas["n"] == 2:
            raise sqlite3.OperationalError("disco lleno")
        return original(movement)

    monkeypatch.setattr(repo, "append_stock_movement", falla_en_la_entrada)

    with pytest.raises(sqlite3.OperationalError):
        transfer_stock(
            repo,
            item_id=item.id,
            from_location_id=central.id,
            to_location_id=camioneta.id,
            quantity=Decimal("40"),
            occurred_at=WHEN,
        )

    assert repo.current_stock(item.id, central.id) == Decimal("60")
    assert repo.current_stock(item.id, camioneta.id) == Decimal("0")


def test_transaction_no_admite_anidamiento(repo):
    with pytest.raises(RuntimeError, match="anidamiento"):
        with repo.transaction():
            with repo.transaction():
                pass


def test_lo_escrito_fuera_de_transaction_sigue_commiteando(repo):
    """`_commit()` no puede haber roto el camino normal de escritura."""
    item, central = _producto(repo), _deposito(repo, "Central")
    _cargar(repo, item, central, 7)
    assert repo.current_stock(item.id, central.id) == Decimal("7")


# ── La guarda en la venta ────────────────────────────────────────────────


def _venta(producto, cantidad) -> Sale:
    return Sale(
        None,
        "V-1",
        (
            SaleItem(
                CatalogItemType.PRODUCT, producto.name, cantidad, Decimal("100"),
                item_id=producto.id,
            ),
        ),
    )


def test_la_venta_sin_validar_sigue_vendiendo_en_negativo(repo):
    """El default no cambia: es el comportamiento de los tres en produccion."""
    item, deposito = _producto(repo), _deposito(repo, "Salon")

    confirm_sale(repo, _venta(item, Decimal("2")), deposito.id, WHEN)

    assert repo.current_stock(item.id, deposito.id) == Decimal("-2")


def test_la_venta_con_validar_stock_rechaza(repo):
    item, deposito = _producto(repo), _deposito(repo, "Salon")
    _cargar(repo, item, deposito, 1)

    with pytest.raises(StockInsuficienteError):
        confirm_sale(repo, _venta(item, Decimal("2")), deposito.id, WHEN, validar_stock=True)


def test_la_venta_rechazada_no_deja_ni_la_venta_ni_los_movimientos(repo):
    """Con dos lineas: la primera tiene stock, la segunda no.

    Sin transaccion quedaria grabada la venta con el descuento de la primera
    linea -- una venta a medias, que es peor que ninguna.
    """
    hay = repo.save_catalog_item(
        CatalogItem(None, CatalogItemType.PRODUCT, "Con stock", Unit("u", "Unidad"))
    )
    no_hay = repo.save_catalog_item(
        CatalogItem(None, CatalogItemType.PRODUCT, "Sin stock", Unit("u", "Unidad"))
    )
    deposito = _deposito(repo, "Salon")
    _cargar(repo, hay, deposito, 10)

    venta = Sale(
        None,
        "V-2",
        (
            SaleItem(CatalogItemType.PRODUCT, hay.name, Decimal("1"), Decimal("100"), item_id=hay.id),
            SaleItem(
                CatalogItemType.PRODUCT, no_hay.name, Decimal("1"), Decimal("100"), item_id=no_hay.id
            ),
        ),
    )

    with pytest.raises(StockInsuficienteError):
        confirm_sale(repo, venta, deposito.id, WHEN, validar_stock=True)

    assert repo.current_stock(hay.id, deposito.id) == Decimal("10"), (
        "desconto la primera linea de una venta que no se concreto"
    )
    ventas = repo._conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    assert ventas == 0, "quedo grabada una venta que fue rechazada"


def test_una_linea_de_servicio_no_valida_stock(repo):
    """Un servicio no tiene existencias: validarlo lo haria imposible de vender."""
    servicio = repo.save_catalog_item(
        CatalogItem(None, CatalogItemType.SERVICE, "Mano de obra", Unit("h", "Hora"))
    )
    deposito = _deposito(repo, "Salon")

    venta = Sale(
        None,
        "V-3",
        (
            SaleItem(
                CatalogItemType.SERVICE, servicio.name, Decimal("2"), Decimal("5000"),
                item_id=servicio.id,
            ),
        ),
    )
    confirmada = confirm_sale(repo, venta, deposito.id, WHEN, validar_stock=True)

    assert confirmada.id is not None


# ── verificar_disponibilidad, usada suelta ───────────────────────────────


def test_verificar_disponibilidad_devuelve_lo_que_hay(repo):
    item, deposito = _producto(repo), _deposito(repo, "Central")
    _cargar(repo, item, deposito, 12)

    assert verificar_disponibilidad(repo, item.id, deposito.id, Decimal("12")) == Decimal("12")
