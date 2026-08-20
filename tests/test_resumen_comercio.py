"""El bloque de comercio del resumen: lo que este motor le aporta al panel.

El nucleo —facturacion y caja— lo trae LibraCore y lo tienen los seis
productos. Esto lo tienen **los cuatro que montan LibraCommerce**; el que no lo
tiene no lo manda, y un bloque ausente no es un bloque en cero
(wiki/analyses/panel-del-dueno-multisucursal.md).

Lo que estos tests fijan:

1. Las ventas anuladas **no** cuentan.
2. El periodo filtra.
3. 🔴 "Bajo minimo" respeta el minimo configurado: `min_stock = 0` significa
   "no me avises", no "avisame siempre". Sin ese filtro, todo producto sin
   minimo entraria en la alerta y el numero seria el tamano del catalogo.

Corre contra los dos motores, como el resto de la suite: la consulta del stock
lleva un `GROUP BY ... HAVING` y un subselect, que es justo donde SQLite y
PostgreSQL se pueden separar.
"""
from datetime import datetime
from decimal import Decimal

from libracommerce.domain.catalog import CatalogItem, CatalogItemType, Unit
from libracommerce.domain.inventory import Location, StockMovement, StockMovementType
from libracommerce.domain.sales import Sale, SaleItem, SaleStatus

PERIODO = ("2026-08-01", "2026-08-31")
CUANDO = datetime(2026, 8, 10, 12, 0, 0)
UNIDAD = Unit("u", "Unidad")


def _producto(repo, nombre, *, min_stock="0", activo=True):
    return repo.save_catalog_item(CatalogItem(
        None, CatalogItemType.PRODUCT, nombre, UNIDAD,
        min_stock=Decimal(min_stock), active=activo,
    ))


def _cargar(repo, item, location, cantidad):
    repo.append_stock_movement(StockMovement(
        None, item.id, location.id, StockMovementType.PURCHASE,
        Decimal(str(cantidad)), CUANDO,
    ))


def _venta(repo, *, numero, total, fecha="2026-08-10", status=SaleStatus.CONFIRMED):
    return repo.save_sale(Sale(
        None, numero,
        (SaleItem(CatalogItemType.SERVICE, "Consumo", Decimal("1"), Decimal(str(total))),),
        status=status,
        subtotal=Decimal(str(total)),
        total=Decimal(str(total)),
        occurred_on=fecha,
    ))


def test_una_base_vacia_da_ceros(repo):
    r = repo.resumen_comercio(*PERIODO)
    assert r["ventas"] == {"cantidad": 0, "monto": 0.0}
    assert r["stock_bajo_minimo"] == 0


def test_las_ventas_del_periodo_se_suman(repo):
    _venta(repo, numero="V-1", total=1000)
    _venta(repo, numero="V-2", total=500)

    r = repo.resumen_comercio(*PERIODO)

    assert r["ventas"]["cantidad"] == 2
    assert r["ventas"]["monto"] == 1500.0


def test_una_venta_anulada_no_cuenta(repo):
    _venta(repo, numero="V-1", total=1000)
    _venta(repo, numero="V-2", total=999, status=SaleStatus.CANCELLED)

    r = repo.resumen_comercio(*PERIODO)

    assert r["ventas"]["cantidad"] == 1
    assert r["ventas"]["monto"] == 1000.0


def test_lo_de_otro_periodo_no_entra(repo):
    _venta(repo, numero="V-1", total=1000, fecha="2026-07-15")

    assert repo.resumen_comercio(*PERIODO)["ventas"]["cantidad"] == 0


def test_un_producto_bajo_su_minimo_se_cuenta(repo):
    item = _producto(repo, "Gaseosa", min_stock="10")
    deposito = repo.save_location(Location(None, "Deposito"))
    _cargar(repo, item, deposito, 3)

    assert repo.resumen_comercio(*PERIODO)["stock_bajo_minimo"] == 1


def test_un_producto_por_encima_del_minimo_no_se_cuenta(repo):
    item = _producto(repo, "Agua", min_stock="10")
    deposito = repo.save_location(Location(None, "Deposito"))
    _cargar(repo, item, deposito, 50)

    assert repo.resumen_comercio(*PERIODO)["stock_bajo_minimo"] == 0


def test_sin_minimo_configurado_no_entra_en_la_alerta(repo):
    """🔴 `min_stock = 0` es "no me avises".

    Sin este filtro, un producto sin minimo y sin stock —que es el estado por
    defecto de todo el catalogo— contaria como alerta, y el numero seria el
    tamano del catalogo en vez de una senal.
    """
    _producto(repo, "Sin minimo", min_stock="0")

    assert repo.resumen_comercio(*PERIODO)["stock_bajo_minimo"] == 0


def test_un_producto_inactivo_no_entra(repo):
    """Un producto dado de baja no es una alerta de reposicion."""
    _producto(repo, "Descontinuado", min_stock="10", activo=False)

    assert repo.resumen_comercio(*PERIODO)["stock_bajo_minimo"] == 0


def test_el_stock_se_suma_entre_depositos(repo):
    """El minimo es del producto, no de cada deposito: 6 + 6 supera un minimo
    de 10 aunque ninguno de los dos lo alcance solo."""
    item = _producto(repo, "Repartido", min_stock="10")
    uno = repo.save_location(Location(None, "Salon"))
    otro = repo.save_location(Location(None, "Deposito"))
    _cargar(repo, item, uno, 6)
    _cargar(repo, item, otro, 6)

    assert repo.resumen_comercio(*PERIODO)["stock_bajo_minimo"] == 0
