"""Las factories de router de catálogo, depósitos y stock (P9-M1, 2026-09-06).

Reemplazan a `app/web/api/productos.py`, `depositos.py` y `stock.py` de
Contalibra y Restolibra, que eran la misma API con variaciones de producto.
Mismo patrón que `libracore.facturas_router`: el producto llama a la factory
con lo que decide —cómo abre la conexión, quién es el usuario, sus opciones y
sus ganchos— y monta el `APIRouter` que vuelve con `include_router`, poniéndole
**él** el gate de sesión y de módulo (`dependencies=[...]`), como hoy.

## Lo que cada producto decide, y por qué es una opción y no un `if`

Medido en los dos routers el 2026-09-06:

- **Código autogenerado al crear** (`generar_codigo_si_falta`): Restolibra
  genera `BEB-0001` cuando el alta viene sin código; Contalibra lo deja vacío.
- **Modos de ajuste**: los tres de siempre (fijar, entrada, salida) más
  `merma` con un motivo de una lista cerrada, que sólo Restolibra tiene. La
  merma existe si el producto declara `motivos_merma`; sin motivos, mandarla
  es un 422, igual que cualquier modo inválido.
- **Conversión de unidad de compra** en el modo entrada (`unidad_compra` +
  `factor`): campos opcionales del payload que Contalibra no manda. No es un
  campo del producto: se ingresa a mano por movimiento y el detalle queda en
  la referencia, como en el router de Restolibra.
- **El payload de producto es la unión**: `tipo` (producto/servicio, de
  Contalibra) y `estacion`/`vendible` (de Restolibra) con los defaults que
  cada uno asumía. Un producto que no manda un campo no cambia de
  comportamiento.
- **La respuesta de `GET /stock/{pid}` es la unión**: `{producto, stock_actual}`.
  Contalibra devolvía sólo `stock_actual`; su frontend ignora la clave extra.

Lo que es arista queda en el producto y se monta al lado, con el mismo
prefijo: las recetas y el reporte de costos de Restolibra
(`/api/productos/{pid}/receta`, `/api/productos/reportes-costos`), y el
autocompletado `/productos/buscar` de los dos, que depende de listas de precio
y se mueve en M2.

## F1 de VentaLibra a LibraCommerce (2026-09-14): variantes y balanza

Tres agregados, todos aditivos -- Contalibra y Restolibra no los llaman y no
cambian de comportamiento:

- **`GET /escanear?code=`**: balanza o código de barras común (o el SKU de una
  variante), sobre `catalogo.escanear`. Replica
  `ventalibra/app/services/scale.py::ScaleService.scan` y su endpoint
  `GET /catalog/items/scan`.
- **Variantes** (`GET`/`POST /{pid}/variantes`, `PUT /{pid}/variantes/{vid}`):
  el CRUD que ya tiene VentaLibra por su cuenta
  (`ventalibra/app/services/catalog.py::CatalogService`), sobre
  `item_variants`.
- **`incluir_variantes`** en `GET /api/productos`: opcional, default `False`.
  Sin él, la respuesta es exactamente la de siempre.

## Carga de vencimientos (2026-09-30, ADR-018): la marca `vence` en el producto

**Opt-in** con `OpcionesCatalogo.con_vencimientos` (default `False`): apagada, el router es **byte a byte** el de
siempre (Contalibra y Restolibra no cambian; hay un test que compara el JSON). Prendida:

- el listado (`GET /api/productos`), el escaneo (`GET /escanear`, dentro de `producto`) y la respuesta del alta y la
  edición devuelven `vence: bool` (`catalog_items.tracks_expiry`) en el producto. **Una sola consulta auxiliar por
  pedido** para todo el listado, no una por producto, y **tolera una base sin la revisión `0002`** (`vence` es `false`
  y nada se rompe: se sondea por metadatos, `erp.vencimientos.tiene_revision`).
- el alta y la edición aceptan `vence` (`true`/`false`, estricto). **Si no viene, la marca no se toca** (editar otros
  campos nunca la pierde: `save_catalog_item` no la escribe). Si viene y cambió, se marca o desmarca con
  `erp.vencimientos.marcar_vence`. `OpcionesCatalogo.autorizar_marcar_vence(usuario) -> bool` decide quién puede
  cambiarla: si devuelve `False`, **403** y no se guarda nada del resto de la edición (la autorización y las reglas se
  resuelven **antes** de escribir); sin gancho, cualquiera que pueda editar el producto puede marcar. Sin la revisión
  `0002` (y `vence` distinto de lo que hay), **409** con el comando que falta; la combinación resultante de la edición no puede ser un servicio marcado (409, cambie o no la marca; desmarcarlo
  con `vence: false` en la misma edición vale).
  Con el gancho puesto el router resuelve `usuario_actual` en el alta y la edición.
- 🔵 `save_catalog_item` commitea por su cuenta (es del repositorio, que no se toca acá), así que no hay una única
  transacción SQL con el guardado: en su lugar, **todo lo que puede rechazar el cambio de marca se resuelve antes de
  escribir** (autorización, revisión, servicio) y la marca se escribe **después** del guardado, en la misma conexión y
  sin commit propio (la confirma el cierre del pedido). Un guardado que falla (422) nunca deja la marca cambiada.
"""

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from ..erp import catalogo, stock, vencimientos
from . import fastapi as _fastapi

# La guarda traduce la ausencia del extra `[web]` a un error que lo nombra;
# despues de eso los nombres se importan normal, para que ruff y FastAPI los
# reconozcan (con `fastapi.Depends` como atributo ruff no sabe que es inmutable,
# y FastAPI no resuelve un modelo definido adentro de una closure).
_fastapi()
from fastapi import APIRouter, Depends, HTTPException  # noqa: E402
from pydantic import BaseModel, StrictBool  # noqa: E402

#: La lista cerrada de motivos de merma de Restolibra (`web/templates/stock/ajuste.html`).
MOTIVOS_MERMA_GASTRONOMICOS = (
    "Quemado",
    "Caída al piso",
    "Vencimiento",
    "Rotura",
    "Degustación",
    "Consumo del personal",
    "Otro",
)

Conexion = Callable[[], AbstractContextManager[Any]]


def _conexion_default() -> Conexion:
    from libracore.db.core import get_connection

    return get_connection


def _sin_usuario() -> dict:
    return {}


@dataclass(frozen=True)
class OpcionesCatalogo:
    #: Si un alta sin código recibe uno generado (`BEB-0001`). Restolibra: sí.
    generar_codigo_si_falta: bool = False
    #: Las unidades que ofrece el alta; se exponen en `GET /unidades`.
    unidades: tuple[str, ...] = catalogo.UNIDADES
    #: Un producto que administra sus propias unidades (VentaLibra: código, nombre, si admite fracciones): `GET
    #: /unidades` lista los códigos de `units` en vez de la tupla de arriba.
    unidades_de_la_base: bool = False
    #: Quién puede crear y borrar categorías (una `Depends(...)`). Un producto con categorías jerárquicas que las
    #: administra por su cuenta la usa para cerrar esa vía.
    autorizar_categorias: Any = None
    #: Reglas de un producto, antes de guardar: `(payload, actual)` con `actual` = el producto como está hoy (`None` al
    #: crear). Decide con una `HTTPException` (VentaLibra: el tipo no se cambia, la unidad se bloquea con movimientos).
    validar_producto: Callable[["ProductoPayload", dict | None], None] | None = None
    #: Antes de borrar un producto (`actual`): un producto con historial no se borra, se desactiva.
    validar_eliminacion: Callable[[dict], None] | None = None
    #: Carga de vencimientos (ADR-018, 2026-09-30): el producto lleva la marca `vence` (`catalog_items.tracks_expiry`).
    #: Apagada (el default) el router es byte a byte el de siempre. Prendida: listado, escaneo, alta y edición devuelven
    #: `vence: bool`, y el alta y la edición aceptan `vence` para marcar o desmarcar. Hace falta la revisión `0002`
    #: para marcar; sin ella `vence` es `false` y leer no se rompe.
    con_vencimientos: bool = False
    #: Quién puede marcar o desmarcar (con `con_vencimientos`): recibe el usuario de la sesión (`usuario_actual`) y
    #: devuelve si puede. `False` es un 403 y no se guarda nada de la edición. `None` = cualquiera que pueda editar el
    #: producto.
    autorizar_marcar_vence: Callable[[dict], bool] | None = None


@dataclass(frozen=True)
class OpcionesStock:
    #: Los motivos de merma. Vacío = el producto no tiene mermas (Contalibra).
    motivos_merma: tuple[str, ...] = ()
    #: Tope por defecto del historial de movimientos.
    limite_movimientos: int = 200
    etiquetas_de_tipo: dict[str, str] = field(default_factory=lambda: dict(stock.TIPO_LABELS))
    #: Un producto con varias sucursales/depósitos: `GET /api/stock` trae además `depositos` y el stock de cada
    #: producto en cada uno (`por_deposito`), y el ajuste puede ir a un depósito (`deposito_id`). Sin esto la
    #: respuesta es la de siempre (Contalibra, Restolibra).
    por_deposito: bool = False
    #: Lote y vencimiento en el ajuste (ADR-018, A-4 PR-3, opt-in): el cuerpo de `POST /{pid}/ajuste` acepta `lot_code`
    #: y `expires_at`, sólo con `modo="absoluto"` (el conteo de ESE lote, ver `stock.ajustar_stock`) o `modo="entrada"` (la
    #: entrada va a ESE bucket, ver `stock.entrada_manual_con_lote`) y sólo para un producto marcado (`tracks_expiry`);
    #: un producto sin marcar, o otro modo, con esos campos es 422. Apagada (el default) el cuerpo, el esquema OpenAPI y
    #: las respuestas son los de siempre (Contalibra y Restolibra no cambian). **Sin esta opción, y con ella sin los
    #: campos**, un producto marcado igual sigue el lote: el ajuste que baja, la `salida` y la `merma` salen por FEFO
    #: (`stock.salida_manual`) y lo que sube o entra sin lote entra «sin lote».
    con_lotes: bool = False


@dataclass(frozen=True)
class OpcionesDepositos:
    """Lo que un producto le agrega a los depósitos. Sin nada, el router hace lo que hacían Contalibra y
    Restolibra. Cada gancho decide con una `HTTPException`: el motor no sabe qué código le toca a la regla de cada
    producto.

    - `autorizar_escritura`: quién crea, edita, predetermina y borra (una `Depends(...)`, como
      `OpcionesCajas.autorizar_escritura` de LibraCore). La transferencia y la lectura quedan con la protección con la
      que el producto monte el router.
    - `validar_alta(payload)` / `validar_edicion(payload, actual)` / `validar_eliminacion(actual)`: antes de escribir.
    - `al_guardar(deposito)`: después de crear o editar (VentaLibra: una sucursal que vende necesita su caja).
    """

    autorizar_escritura: Any = None
    validar_alta: Callable[["DepositoCreatePayload"], None] | None = None
    validar_edicion: Callable[["DepositoUpdatePayload", dict], None] | None = None
    validar_eliminacion: Callable[[dict], None] | None = None
    al_guardar: Callable[[dict], None] | None = None


@dataclass(frozen=True)
class OpcionesSucursales:
    """Lo que un producto le agrega a las sucursales. Sin `build_sucursales_router` montado, un producto no
    tiene sucursales (Contalibra) — mismo criterio que `OpcionesDepositos`.

    La baja de una sucursal ya trae su propia guarda (`catalogo._verificar_baja_de_sucursal`, portada de
    LibraDesk): esto es sólo lo que un producto le agrega ENCIMA de esa guarda. `al_guardar` recibe la sucursal
    con `deposito_predeterminado_id`: al crearla, el depósito que el motor ya le creó."""

    autorizar_escritura: Any = None
    validar_alta: Callable[["SucursalCreatePayload"], None] | None = None
    validar_edicion: Callable[["SucursalUpdatePayload", dict], None] | None = None
    al_guardar: Callable[[dict], None] | None = None


# ── Payloads (a nivel de módulo: FastAPI los resuelve por anotación) ─────


class ProductoPayload(BaseModel):
    """La unión de los dos productos: `tipo` (Contalibra) y `estacion`/`vendible`
    (Restolibra), con los defaults que cada uno asumía."""

    nombre: str
    codigo: str = ""
    descripcion: str = ""
    precio_venta: float = 0
    precio_costo: float = 0
    unidad: str = "u"
    categoria: str = ""
    stock_minimo: float = 0
    activo: bool = True
    tipo: Literal["producto", "servicio"] = "producto"
    estacion: str = ""
    vendible: bool = True
    #: `None` (ausente) CONSERVA lo que ya tenga la unidad -- no la resetea.
    #: Contalibra y Restolibra nunca lo mandan, así que sus unidades (en
    #: `False` desde siempre) quedan exactamente igual. Ver
    #: `catalogo._resolver_permite_fraccion`.
    permite_fraccion: bool | None = None


class ProductoConVencePayload(ProductoPayload):
    """`ProductoPayload` más la marca `vence`, sólo con `OpcionesCatalogo.con_vencimientos` (apagada, el payload es el
    de siempre). `None` (ausente) NO toca la marca; estricto: `"si"` o `1` no son un booleano."""

    vence: StrictBool | None = None


class CategoriaPayload(BaseModel):
    nombre: str


class DepositoCreatePayload(BaseModel):
    nombre: str
    descripcion: str = ""
    #: `locations.location_type`, sólo al crear (el tipo no se cambia después). `None` = el default del dominio.
    tipo: str | None = None
    #: `locations.branch_id`: la sucursal a la que pertenece este depósito. `None` = sin sucursal (Contalibra,
    #: la mayoría de las instancias de un solo local).
    branch_id: int | None = None


class DepositoUpdatePayload(BaseModel):
    nombre: str
    descripcion: str = ""
    activo: bool = True


class SucursalCreatePayload(BaseModel):
    nombre: str
    codigo: str = ""
    direccion: str = ""
    #: El primer depósito de la sucursal (toda sucursal tiene uno). Vacío = «Depósito <nombre>».
    deposito: str = ""
    #: `location_type` del depósito que se crea con la sucursal. `None` = el default del dominio.
    deposito_tipo: str | None = None


class DepositoPredeterminadoPayload(BaseModel):
    deposito_id: int


class SucursalUpdatePayload(BaseModel):
    nombre: str
    codigo: str = ""
    direccion: str = ""
    activa: bool = True


class TransferenciaPayload(BaseModel):
    producto_id: int
    origen_id: int
    destino_id: int
    cantidad: float
    fecha: str = ""
    observaciones: str = ""
    variant_id: int | None = None


class CodigoPayload(BaseModel):
    #: `internal`, `barcode`, `sku`, `scale` u `other` (`catalogo.TIPOS_DE_CODIGO`).
    tipo: str = "barcode"
    codigo: str
    es_principal: bool = False


class VariantePayload(BaseModel):
    sku: str
    nombre: str
    atributos: dict[str, str] = {}


class VarianteUpdatePayload(BaseModel):
    sku: str
    nombre: str
    atributos: dict[str, str] = {}
    activa: bool = True


class AjustePayload(BaseModel):
    modo: Literal["absoluto", "entrada", "salida", "merma"] = "absoluto"
    cantidad: float
    referencia: str = ""
    fecha: str = ""
    # Sólo con modo="entrada": conversión de unidad de compra, por movimiento.
    unidad_compra: str = ""
    factor: float = 1
    # Sólo con modo="merma".
    motivo: str = "Otro"
    # Con `OpcionesStock.por_deposito`: en qué depósito (y de qué variante) va el ajuste. `None` = el de siempre.
    deposito_id: int | None = None
    variant_id: int | None = None


class AjusteConLotePayload(AjustePayload):
    """`AjustePayload` más el lote, sólo con `OpcionesStock.con_lotes` (apagada, el payload es el de siempre)."""

    lot_code: str | None = None
    expires_at: str | None = None


def _deps(usuario_actual, conexion):
    return (conexion or _conexion_default()), (usuario_actual or _sin_usuario)


def _nadie() -> None:
    """Sin opción de vencimientos (o sin gancho) el router no resuelve la sesión: una dependencia vacía."""
    return None


# ── Productos y categorías ───────────────────────────────────────────────


def build_productos_router(
    *,
    conexion: Conexion | None = None,
    usuario_actual: Callable[..., Any] | None = None,
    prefix: str = "/api/productos",
    opciones: OpcionesCatalogo | None = None,
):
    """`GET`/`POST` de productos, `PUT`/`DELETE /{pid}`, y las categorías
    (`GET`/`POST /categorias`, `DELETE /categorias/{cid}`). Sin gate propio: lo
    pone el producto al montarlo."""
    abrir, usuario = _deps(usuario_actual, conexion)
    opciones = opciones or OpcionesCatalogo()
    con_vence = opciones.con_vencimientos
    # Sin la opción (Contalibra, Restolibra) el payload, las respuestas y las dependencias son los de siempre.
    Payload = ProductoConVencePayload if con_vence else ProductoPayload
    sesion = Depends(usuario) if con_vence and opciones.autorizar_marcar_vence is not None else Depends(_nadie)
    categorias_escriben = [opciones.autorizar_categorias] if opciones.autorizar_categorias is not None else []

    router = APIRouter(prefix=prefix, tags=["productos"])

    @router.get("")
    def listar(q: str = "", incluir_variantes: bool = False, solo_activos: bool = False,
               solo_vendibles: bool = False):
        """`q` busca cada palabra (en cualquier orden) en el nombre, el código o la categoría, sin distinguir
        mayúsculas ni acentos. `solo_activos`/`solo_vendibles` (default `False`: todos, como siempre) son lo que el
        punto de venta ofrece."""
        with abrir() as conn:
            productos = catalogo.get_all_productos(conn, q=q, solo_activos=solo_activos, solo_vendibles=solo_vendibles)
            if incluir_variantes:
                for p in productos:
                    p["variantes"] = catalogo.get_variantes_producto(conn, p["id"])
            if con_vence:
                marcados = vencimientos.ids_que_vencen(conn)  # una consulta para todo el listado
                for p in productos:
                    p["vence"] = p["id"] in marcados
            return productos

    @router.get("/unidades")
    def unidades():
        if opciones.unidades_de_la_base:
            with abrir() as conn:
                return [r[0] for r in conn.execute("SELECT code FROM units ORDER BY code").fetchall()]
        return list(opciones.unidades)

    @router.get("/escanear")
    def escanear(code: str):
        """Balanza (`item_codes.code_type='scale'`) o código común (código de
        barras del producto, o SKU de una variante). 404 si el código no
        corresponde a nada; 422 si SÍ es de balanza pero apunta a algo que no
        se puede vender así (ver `catalogo.EtiquetaBalanzaError`)."""
        with abrir() as conn:
            try:
                resultado = catalogo.escanear(conn, code)
            except catalogo.EtiquetaBalanzaError as e:
                raise HTTPException(422, str(e)) from e
            if resultado is None:
                raise HTTPException(404, "No hay ningún ítem con ese código.")
            if con_vence and resultado.get("producto"):
                resultado["producto"]["vence"] = bool(vencimientos.ids_que_vencen(conn, resultado["producto"]["id"]))
            return resultado

    @router.get("/categorias")
    def listar_categorias():
        with abrir() as conn:
            return catalogo.get_categorias_producto(conn)

    @router.post("/categorias", dependencies=categorias_escriben)
    def crear_categoria(payload: CategoriaPayload):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        with abrir() as conn:
            catalogo.create_categoria_producto(conn, nombre)
            return catalogo.get_categorias_producto(conn)

    @router.delete("/categorias/{cid}", dependencies=categorias_escriben)
    def eliminar_categoria(cid: int):
        with abrir() as conn:
            catalogo.delete_categoria_producto(conn, cid)
            return catalogo.get_categorias_producto(conn)

    def _cambio_de_marca(conn, payload, user: dict | None, vence_actual: bool) -> bool | None:
        """El valor al que hay que llevar la marca `vence`, o `None` si no hay que tocarla (no vino o no cambió). Todo
        lo que puede rechazar el cambio se resuelve **antes** de escribir: autorización (403), la revisión `0002` que
        falta (409) y un servicio, que no tiene inventario (409)."""
        # La combinación resultante, cambie o no la marca: un servicio no puede quedar marcado (un producto marcado que
        # se edita a servicio con `vence` omitido o `true`). Desmarcarlo en la misma edición sí vale.
        efectiva = vence_actual if payload.vence is None else payload.vence
        if payload.tipo == "servicio" and efectiva:
            raise HTTPException(409, "Un servicio no puede tener vencimiento: desmarcalo (vence: false) al cambiar el tipo.")
        if payload.vence is None or payload.vence == vence_actual:
            return None
        if opciones.autorizar_marcar_vence is not None and not opciones.autorizar_marcar_vence(user or {}):
            raise HTTPException(403, "No tenés permiso para marcar o desmarcar productos que vencen.")
        try:
            vencimientos._exigir_revision(conn)
        except vencimientos.SinRevision as e:
            raise HTTPException(409, str(e)) from e
        return payload.vence

    def _marcar(conn, pid: int, vence: bool) -> None:
        try:
            vencimientos.marcar_vence(conn, pid, vence)
        except vencimientos.ProductoNoEncontrado as e:
            raise HTTPException(404, str(e)) from e
        except vencimientos.VencimientosError as e:
            raise HTTPException(409, str(e)) from e

    @router.post("")
    def crear(payload: Payload, user: dict | None = sesion):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        codigo = payload.codigo.strip()
        categoria = payload.categoria.strip()
        if opciones.validar_producto:
            opciones.validar_producto(payload, None)
        with abrir() as conn:
            marca = _cambio_de_marca(conn, payload, user, False) if con_vence else None
            if not codigo and opciones.generar_codigo_si_falta:
                codigo = catalogo.generar_codigo_producto(conn, categoria)
            try:
                pid = catalogo.create_producto(
                    conn, nombre=nombre, codigo=codigo,
                    descripcion=payload.descripcion.strip(),
                    precio_venta=payload.precio_venta, precio_costo=payload.precio_costo,
                    unidad=payload.unidad, categoria=categoria,
                    stock_minimo=payload.stock_minimo, tipo=payload.tipo,
                    estacion=payload.estacion.strip(),
                    vendible=1 if payload.vendible else 0,
                    permite_fraccion=payload.permite_fraccion,
                )
            except Exception as e:
                raise HTTPException(422, str(e)) from e
            producto = catalogo.get_producto(conn, pid)
            if con_vence:
                if marca is not None:
                    _marcar(conn, pid, marca)
                producto["vence"] = bool(marca)
            return producto

    @router.put("/{pid}")
    def actualizar(pid: int, payload: Payload, user: dict | None = sesion):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        with abrir() as conn:
            actual = catalogo.get_producto(conn, pid)
            if not actual:
                raise HTTPException(404, "Producto no encontrado")
            marca = None
            if con_vence:
                actual["vence"] = pid in vencimientos.ids_que_vencen(conn, pid)
            if opciones.validar_producto:
                opciones.validar_producto(payload, actual)
            if con_vence:
                marca = _cambio_de_marca(conn, payload, user, actual["vence"])   # todo lo rechazable, antes de escribir
            try:
                catalogo.update_producto(
                    conn, pid=pid, nombre=nombre, codigo=payload.codigo.strip(),
                    descripcion=payload.descripcion.strip(),
                    precio_venta=payload.precio_venta, precio_costo=payload.precio_costo,
                    unidad=payload.unidad, categoria=payload.categoria.strip(),
                    activo=1 if payload.activo else 0, stock_minimo=payload.stock_minimo,
                    tipo=payload.tipo, estacion=payload.estacion.strip(),
                    vendible=1 if payload.vendible else 0,
                    permite_fraccion=payload.permite_fraccion,
                )
            except Exception as e:
                raise HTTPException(422, str(e)) from e
            if marca is not None:
                _marcar(conn, pid, marca)   # después del guardado: un guardado que falla no deja la marca cambiada
            producto = catalogo.get_producto(conn, pid)
            if con_vence:
                producto["vence"] = actual["vence"] if marca is None else marca
            return producto

    @router.delete("/{pid}")
    def eliminar(pid: int):
        with abrir() as conn:
            actual = catalogo.get_producto(conn, pid)
            if not actual:
                raise HTTPException(404, "Producto no encontrado")
            if opciones.validar_eliminacion:
                opciones.validar_eliminacion(actual)
            catalogo.delete_producto(conn, pid)
        return {"ok": True}

    # ── Códigos (varios por producto: barras, SKU, balanza…) ─────────────

    @router.get("/{pid}/codigos")
    def listar_codigos(pid: int):
        with abrir() as conn:
            if not catalogo.get_producto(conn, pid):
                raise HTTPException(404, "Producto no encontrado")
            return catalogo.get_codigos(conn, pid)

    @router.post("/{pid}/codigos")
    def crear_codigo(pid: int, payload: CodigoPayload):
        codigo = payload.codigo.strip()
        if not codigo:
            raise HTTPException(422, "El código es obligatorio.")
        with abrir() as conn:
            if not catalogo.get_producto(conn, pid):
                raise HTTPException(404, "Producto no encontrado")
            try:
                return catalogo.add_codigo(conn, pid, payload.tipo, codigo, payload.es_principal)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            except sqlite3.IntegrityError as e:
                # UNIQUE(tipo, código) o un segundo principal: datos del pedido, no un error del servidor.
                raise HTTPException(409, "Ese código ya existe, o el producto ya tiene un código principal.") from e

    # ── Variantes (talle/color, presentaciones) ──────────────────────────

    @router.get("/{pid}/variantes")
    def listar_variantes(pid: int):
        with abrir() as conn:
            if not catalogo.get_producto(conn, pid):
                raise HTTPException(404, "Producto no encontrado")
            return catalogo.get_variantes_producto(conn, pid)

    @router.post("/{pid}/variantes")
    def crear_variante(pid: int, payload: VariantePayload):
        sku = payload.sku.strip()
        nombre = payload.nombre.strip()
        if not sku or not nombre:
            raise HTTPException(422, "El SKU y el nombre son obligatorios.")
        with abrir() as conn:
            if not catalogo.get_producto(conn, pid):
                raise HTTPException(404, "Producto no encontrado")
            try:
                return catalogo.create_variante(conn, pid, sku, nombre, payload.atributos)
            except sqlite3.IntegrityError as e:
                # UNIQUE(sku): error de datos del cliente, no del server. Todo
                # lo demás (una base caída, por ejemplo) tiene que seguir
                # siendo un 500 -- no se atrapa acá.
                raise HTTPException(409, str(e)) from e

    @router.put("/{pid}/variantes/{vid}")
    def actualizar_variante(pid: int, vid: int, payload: VarianteUpdatePayload):
        sku = payload.sku.strip()
        nombre = payload.nombre.strip()
        if not sku or not nombre:
            raise HTTPException(422, "El SKU y el nombre son obligatorios.")
        with abrir() as conn:
            existente = catalogo.get_variante(conn, vid)
            if not existente or existente["producto_id"] != pid:
                raise HTTPException(404, "Variante no encontrada")
            try:
                return catalogo.update_variante(conn, vid, sku, nombre, payload.atributos, payload.activa)
            except sqlite3.IntegrityError as e:
                raise HTTPException(409, str(e)) from e

    return router


# ── Depósitos ────────────────────────────────────────────────────────────


def build_depositos_router(
    *,
    conexion: Conexion | None = None,
    usuario_actual: Callable[..., Any] | None = None,
    prefix: str = "/api/depositos",
    opciones: OpcionesDepositos | None = None,
):
    """Depósitos, stock por depósito y transferencias. Idéntico en los dos productos (6 líneas de diferencia,
    todas docstrings); las variantes de un producto con sucursales entran por `opciones`."""
    abrir, usuario = _deps(usuario_actual, conexion)
    opt = opciones or OpcionesDepositos()
    # Ya viene envuelta en `Depends(...)`: se pasa tal cual.
    escribe = [opt.autorizar_escritura] if opt.autorizar_escritura is not None else []

    router = APIRouter(prefix=prefix, tags=["depositos"])

    @router.get("")
    def listar():
        with abrir() as conn:
            depositos = catalogo.get_all_depositos(conn)
            for d in depositos:
                d["total_productos"] = len(catalogo.get_stock_por_deposito(conn, d["id"]))
            return depositos

    @router.post("", dependencies=escribe)
    def crear(payload: DepositoCreatePayload):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        if opt.validar_alta:
            opt.validar_alta(payload)
        with abrir() as conn:
            try:
                did = catalogo.create_deposito(
                    conn, nombre, payload.descripcion.strip(), payload.tipo, payload.branch_id
                )
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            creado = catalogo.get_deposito(conn, did)
            if opt.al_guardar:
                opt.al_guardar(creado)
            return creado

    @router.put("/{did}", dependencies=escribe)
    def actualizar(did: int, payload: DepositoUpdatePayload):
        nombre = payload.nombre.strip()
        with abrir() as conn:
            actual = catalogo.get_deposito(conn, did)
            if not actual:
                raise HTTPException(404, "Depósito no encontrado")
            if not nombre:
                raise HTTPException(422, "El nombre es obligatorio.")
            if opt.validar_edicion:
                opt.validar_edicion(payload, actual)
            try:
                catalogo.update_deposito(conn, did, nombre, payload.descripcion.strip(), 1 if payload.activo else 0)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            guardado = catalogo.get_deposito(conn, did)
            if opt.al_guardar:
                opt.al_guardar(guardado)
            return guardado

    @router.post("/{did}/set-default", dependencies=escribe)
    def set_default(did: int):
        with abrir() as conn:
            if not catalogo.get_deposito(conn, did):
                raise HTTPException(404, "Depósito no encontrado")
            try:
                catalogo.set_default_deposito(conn, did)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            return catalogo.get_deposito(conn, did)

    @router.delete("/{did}", dependencies=escribe)
    def eliminar(did: int):
        with abrir() as conn:
            actual = catalogo.get_deposito(conn, did)
            if not actual:
                raise HTTPException(404, "Depósito no encontrado")
            if opt.validar_eliminacion:
                opt.validar_eliminacion(actual)
            try:
                catalogo.delete_deposito(conn, did)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
        return {"ok": True}

    @router.get("/transferencias")
    def transferencias(deposito_id: int | None = None, limite: int = 200):
        """El historial de transferencias (las que tocan `deposito_id`, de los dos lados, o todas)."""
        with abrir() as conn:
            return catalogo.get_transferencias(conn, deposito_id, limite)

    @router.get("/{did}/stock")
    def stock_del_deposito(did: int):
        with abrir() as conn:
            if not catalogo.get_deposito(conn, did):
                raise HTTPException(404, "Depósito no encontrado")
            return catalogo.get_stock_por_deposito(conn, did)

    @router.get("/stock-producto/{pid}")
    def stock_producto(pid: int):
        with abrir() as conn:
            return catalogo.get_stock_producto_todos_depositos(conn, pid)

    @router.post("/transferir")
    def transferir(payload: TransferenciaPayload, user: dict = Depends(usuario)):
        if payload.origen_id == payload.destino_id:
            raise HTTPException(422, "El depósito origen y destino deben ser distintos.")
        if payload.cantidad <= 0:
            raise HTTPException(422, "La cantidad debe ser mayor a 0.")
        with abrir() as conn:
            try:
                catalogo.transferir_stock(
                    conn, producto_id=payload.producto_id, origen_id=payload.origen_id,
                    destino_id=payload.destino_id, cantidad=payload.cantidad,
                    usuario_id=user.get("id"), fecha=payload.fecha,
                    observaciones=payload.observaciones.strip(), variant_id=payload.variant_id,
                )
            except catalogo.DepositoInexistente as e:
                # Explícito y no sólo cubierto por el `except ValueError` de
                # abajo (que también lo atraparía, por herencia): así queda
                # dicho con su nombre, igual que en `ventas_router.py`.
                raise HTTPException(422, str(e)) from e
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            # Cómo quedó cada lado: la pantalla lo pinta sin volver a preguntar (campos de más: quien no
            # los lee no cambia).
            return {
                "ok": True,
                "cantidad": payload.cantidad,
                "origen": _lado(conn, payload.origen_id, payload.producto_id, payload.variant_id),
                "destino": _lado(conn, payload.destino_id, payload.producto_id, payload.variant_id),
            }

    return router


def _lado(conn, deposito_id: int, producto_id: int, variant_id: int | None) -> dict:
    deposito = catalogo.get_deposito(conn, deposito_id)
    return {
        "id": deposito_id, "nombre": deposito["nombre"] if deposito else f"#{deposito_id}",
        "stock": stock.get_stock_actual(conn, producto_id, deposito_id, variant_id),
    }


# ── Sucursales ───────────────────────────────────────────────────────────


def build_sucursales_router(
    *,
    conexion: Conexion | None = None,
    usuario_actual: Callable[..., Any] | None = None,
    prefix: str = "/api/sucursales",
    opciones: OpcionesSucursales | None = None,
):
    """Sucursales, portado de LibraDesk (`app/services/comercial.py`) al motor. Un producto sin sucursales
    (Contalibra) no lo monta y `locations.branch_id` sigue en `None` siempre, como hasta ahora.

    Baja lógica únicamente (nunca `DELETE`): ver el encabezado de la sección "Sucursales" en `erp/catalogo.py`
    sobre por qué no hay FK contra `locations.branch_id`."""
    abrir, _ = _deps(usuario_actual, conexion)
    opt = opciones or OpcionesSucursales()
    escribe = [opt.autorizar_escritura] if opt.autorizar_escritura is not None else []

    router = APIRouter(prefix=prefix, tags=["sucursales"])

    @router.get("")
    def listar(solo_activas: bool = True):
        with abrir() as conn:
            return catalogo.get_all_sucursales(conn, solo_activas)

    @router.get("/{sid}")
    def obtener(sid: int):
        with abrir() as conn:
            sucursal = catalogo.get_sucursal(conn, sid)
            if not sucursal:
                raise HTTPException(404, "Sucursal no encontrada")
            return sucursal

    @router.post("", dependencies=escribe)
    def crear(payload: SucursalCreatePayload):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        if opt.validar_alta:
            opt.validar_alta(payload)
        with abrir() as conn:
            sid = catalogo.create_sucursal(
                conn, nombre, payload.codigo, payload.direccion, payload.deposito, payload.deposito_tipo
            )
            creada = catalogo.get_sucursal(conn, sid)
            if opt.al_guardar:
                opt.al_guardar(creada)
            return creada

    @router.put("/{sid}", dependencies=escribe)
    def actualizar(sid: int, payload: SucursalUpdatePayload):
        nombre = payload.nombre.strip()
        with abrir() as conn:
            actual = catalogo.get_sucursal(conn, sid)
            if not actual:
                raise HTTPException(404, "Sucursal no encontrada")
            if not nombre:
                raise HTTPException(422, "El nombre es obligatorio.")
            if opt.validar_edicion:
                opt.validar_edicion(payload, actual)
            try:
                catalogo.update_sucursal(conn, sid, nombre, payload.codigo, payload.direccion, payload.activa)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            guardada = catalogo.get_sucursal(conn, sid)
            if opt.al_guardar:
                opt.al_guardar(guardada)
            return guardada

    @router.post("/{sid}/set-default", dependencies=escribe)
    def set_default(sid: int):
        with abrir() as conn:
            if not catalogo.get_sucursal(conn, sid):
                raise HTTPException(404, "Sucursal no encontrada")
            try:
                catalogo.set_default_sucursal(conn, sid)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            return catalogo.get_sucursal(conn, sid)

    @router.post("/{sid}/deposito-predeterminado", dependencies=escribe)
    def deposito_predeterminado(sid: int, payload: DepositoPredeterminadoPayload):
        with abrir() as conn:
            if not catalogo.get_sucursal(conn, sid):
                raise HTTPException(404, "Sucursal no encontrada")
            try:
                catalogo.set_deposito_predeterminado(conn, sid, payload.deposito_id)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            return catalogo.get_sucursal(conn, sid)

    return router


# ── Stock ────────────────────────────────────────────────────────────────


def build_stock_router(
    *,
    conexion: Conexion | None = None,
    usuario_actual: Callable[..., Any] | None = None,
    prefix: str = "/api/stock",
    opciones: OpcionesStock | None = None,
):
    """Listado con alertas, historial de movimientos, motivos de merma, stock de
    un producto y el ajuste (fijar / entrada / salida / merma)."""
    abrir, usuario = _deps(usuario_actual, conexion)
    opciones = opciones or OpcionesStock()
    con_merma = bool(opciones.motivos_merma)
    # Sin `con_lotes` el payload (y por eso el OpenAPI) es el de siempre.
    Payload = AjusteConLotePayload if opciones.con_lotes else AjustePayload

    router = APIRouter(prefix=prefix, tags=["stock"])

    @router.get("")
    def listar():
        with abrir() as conn:
            productos = stock.get_stock_todos(conn)
            depositos = catalogo.get_all_depositos(conn) if opciones.por_deposito else []
            por_deposito = stock.get_stock_por_deposito(conn) if opciones.por_deposito else {}
        respuesta = {"productos": productos, "alertas": stock.alertas_de_stock(productos)}
        if opciones.por_deposito:
            # Los inactivos no son columna, pero su stock sigue en el total (la mercadería existe aunque el
            # depósito ya no se use).
            respuesta["depositos"] = [
                {"id": d["id"], "nombre": d["nombre"], "tipo": d["tipo"], "es_default": d["es_default"]}
                for d in depositos if d["activo"]
            ]
            for p in productos:
                p["por_deposito"] = {str(k): v for k, v in por_deposito.get(p["id"], {}).items()}
        return respuesta

    @router.get("/movimientos")
    def movimientos(producto_id: int = 0, desde: str = "", hasta: str = "", limit: int | None = None):
        with abrir() as conn:
            return stock.get_movimientos_stock(
                conn, producto_id=producto_id or None, desde=desde, hasta=hasta,
                limit=limit or opciones.limite_movimientos,
            )

    @router.get("/motivos-merma")
    def motivos_merma():
        return list(opciones.motivos_merma)

    @router.get("/tipos")
    def tipos():
        return opciones.etiquetas_de_tipo

    @router.get("/{pid}")
    def detalle(pid: int, deposito_id: int | None = None, variant_id: int | None = None):
        """`stock_actual` es el total; con `OpcionesStock.por_deposito`, `deposito_id` y/o `variant_id` piden el de ese
        depósito y esa variante, que vuelve en `stock_deposito`."""
        with abrir() as conn:
            producto = catalogo.get_producto(conn, pid)
            if not producto:
                raise HTTPException(404, "Producto no encontrado")
            respuesta = {"producto": producto, "stock_actual": stock.get_stock_actual(conn, pid)}
            if opciones.por_deposito and (deposito_id is not None or variant_id is not None):
                respuesta["stock_deposito"] = stock.get_stock_actual(conn, pid, deposito_id, variant_id)
            return respuesta

    @router.post("/{pid}/ajuste")
    def ajuste(pid: int, payload: Payload, user: dict = Depends(usuario)):
        fecha = payload.fecha or date.today().isoformat()
        referencia = payload.referencia.strip() or "Ajuste manual"
        usuario_id = user.get("id")
        # Sin `por_deposito` el ajuste es el de siempre, aunque el payload traiga el campo.
        deposito_id = payload.deposito_id if opciones.por_deposito else None
        variant_id = payload.variant_id if opciones.por_deposito else None
        with abrir() as conn:
            producto = catalogo.get_producto(conn, pid)
            if not producto:
                raise HTTPException(404, "Producto no encontrado")
            try:
                catalogo.validar_deposito(conn, deposito_id)
            except catalogo.DepositoInexistente as e:
                raise HTTPException(422, str(e)) from e
            destino = {"deposito_id": deposito_id, "variant_id": variant_id}
            lote = {}
            if opciones.con_lotes and (payload.lot_code is not None or payload.expires_at is not None):
                if payload.modo not in ("absoluto", "entrada"):
                    raise HTTPException(
                        422, "El lote y el vencimiento sólo se pueden usar con el modo absoluto o entrada.")
                lote = {"lot_code": payload.lot_code, "expires_at": payload.expires_at}
            if payload.modo == "absoluto":
                if payload.cantidad < 0:
                    raise HTTPException(422, "El stock no puede fijarse en un valor negativo.")
                try:
                    stock.ajustar_stock(conn, pid, payload.cantidad, referencia, usuario_id=usuario_id, fecha=fecha,
                                        **destino, **lote)
                except ValueError as e:
                    if not lote:
                        raise     # sin lote el ajuste no levantaba ValueError: no se cambia lo de siempre
                    raise HTTPException(422, str(e)) from e
            elif payload.modo == "entrada":
                factor = payload.factor or 1
                if factor <= 0:
                    raise HTTPException(422, "El factor de conversión debe ser mayor a 0.")
                unidad_compra = payload.unidad_compra.strip()
                cantidad_base = abs(payload.cantidad) * factor
                ref = referencia
                if unidad_compra and factor != 1:
                    ref = f"{referencia} ({payload.cantidad:g} {unidad_compra} × {factor:g})"
                if lote:
                    # Con lote (sólo un producto marcado): la entrada va a ESE bucket.
                    try:
                        stock.entrada_manual_con_lote(conn, pid, cantidad_base, ref, usuario_id=usuario_id,
                                                      fecha=fecha, **destino, **lote)
                    except ValueError as e:
                        raise HTTPException(422, str(e)) from e
                else:
                    stock.add_movimiento_stock(conn, pid, "entrada", cantidad_base, ref, usuario_id=usuario_id,
                                               fecha=fecha, **destino)
            elif payload.modo == "salida":
                # Un producto marcado sale por FEFO (ADR-018, A-4 PR-3); uno sin marcar, la fila de siempre.
                stock.salida_manual(conn, pid, "salida", payload.cantidad, referencia, usuario_id=usuario_id,
                                    fecha=fecha, **destino)
            elif payload.modo == "merma" and con_merma:
                motivo = (payload.motivo or "Otro").strip() or "Otro"
                stock.salida_manual(conn, pid, "merma", payload.cantidad, f"Merma: {motivo}", usuario_id=usuario_id,
                                    fecha=fecha, **destino)
            else:
                raise HTTPException(422, "Modo inválido.")
            respuesta = {"producto": producto, "stock_actual": stock.get_stock_actual(conn, pid)}
            if deposito_id is not None:
                respuesta["stock_deposito"] = stock.get_stock_actual(conn, pid, deposito_id, variant_id)
            return respuesta

    return router
