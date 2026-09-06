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
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from ..erp import catalogo, stock
from . import fastapi as _fastapi

# La guarda traduce la ausencia del extra `[web]` a un error que lo nombra;
# despues de eso los nombres se importan normal, para que ruff y FastAPI los
# reconozcan (con `fastapi.Depends` como atributo ruff no sabe que es inmutable,
# y FastAPI no resuelve un modelo definido adentro de una closure).
_fastapi()
from fastapi import APIRouter, Depends, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

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


@dataclass(frozen=True)
class OpcionesStock:
    #: Los motivos de merma. Vacío = el producto no tiene mermas (Contalibra).
    motivos_merma: tuple[str, ...] = ()
    #: Tope por defecto del historial de movimientos.
    limite_movimientos: int = 200
    etiquetas_de_tipo: dict[str, str] = field(default_factory=lambda: dict(stock.TIPO_LABELS))


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


class CategoriaPayload(BaseModel):
    nombre: str


class DepositoCreatePayload(BaseModel):
    nombre: str
    descripcion: str = ""


class DepositoUpdatePayload(BaseModel):
    nombre: str
    descripcion: str = ""
    activo: bool = True


class TransferenciaPayload(BaseModel):
    producto_id: int
    origen_id: int
    destino_id: int
    cantidad: float
    fecha: str = ""
    observaciones: str = ""


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


def _deps(usuario_actual, conexion):
    return (conexion or _conexion_default()), (usuario_actual or _sin_usuario)


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
    abrir, _ = _deps(usuario_actual, conexion)
    opciones = opciones or OpcionesCatalogo()

    router = APIRouter(prefix=prefix, tags=["productos"])

    @router.get("")
    def listar(q: str = ""):
        with abrir() as conn:
            return catalogo.get_all_productos(conn, q=q)

    @router.get("/unidades")
    def unidades():
        return list(opciones.unidades)

    @router.get("/categorias")
    def listar_categorias():
        with abrir() as conn:
            return catalogo.get_categorias_producto(conn)

    @router.post("/categorias")
    def crear_categoria(payload: CategoriaPayload):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        with abrir() as conn:
            catalogo.create_categoria_producto(conn, nombre)
            return catalogo.get_categorias_producto(conn)

    @router.delete("/categorias/{cid}")
    def eliminar_categoria(cid: int):
        with abrir() as conn:
            catalogo.delete_categoria_producto(conn, cid)
            return catalogo.get_categorias_producto(conn)

    @router.post("")
    def crear(payload: ProductoPayload):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        codigo = payload.codigo.strip()
        categoria = payload.categoria.strip()
        with abrir() as conn:
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
                )
            except Exception as e:
                raise HTTPException(422, str(e)) from e
            return catalogo.get_producto(conn, pid)

    @router.put("/{pid}")
    def actualizar(pid: int, payload: ProductoPayload):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        with abrir() as conn:
            if not catalogo.get_producto(conn, pid):
                raise HTTPException(404, "Producto no encontrado")
            try:
                catalogo.update_producto(
                    conn, pid=pid, nombre=nombre, codigo=payload.codigo.strip(),
                    descripcion=payload.descripcion.strip(),
                    precio_venta=payload.precio_venta, precio_costo=payload.precio_costo,
                    unidad=payload.unidad, categoria=payload.categoria.strip(),
                    activo=1 if payload.activo else 0, stock_minimo=payload.stock_minimo,
                    tipo=payload.tipo, estacion=payload.estacion.strip(),
                    vendible=1 if payload.vendible else 0,
                )
            except Exception as e:
                raise HTTPException(422, str(e)) from e
            return catalogo.get_producto(conn, pid)

    @router.delete("/{pid}")
    def eliminar(pid: int):
        with abrir() as conn:
            if not catalogo.get_producto(conn, pid):
                raise HTTPException(404, "Producto no encontrado")
            catalogo.delete_producto(conn, pid)
        return {"ok": True}

    return router


# ── Depósitos ────────────────────────────────────────────────────────────


def build_depositos_router(
    *,
    conexion: Conexion | None = None,
    usuario_actual: Callable[..., Any] | None = None,
    prefix: str = "/api/depositos",
):
    """Depósitos, stock por depósito y transferencias. Idéntico en los dos
    productos (6 líneas de diferencia, todas docstrings)."""
    abrir, usuario = _deps(usuario_actual, conexion)

    router = APIRouter(prefix=prefix, tags=["depositos"])

    @router.get("")
    def listar():
        with abrir() as conn:
            depositos = catalogo.get_all_depositos(conn)
            for d in depositos:
                d["total_productos"] = len(catalogo.get_stock_por_deposito(conn, d["id"]))
            return depositos

    @router.post("")
    def crear(payload: DepositoCreatePayload):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        with abrir() as conn:
            did = catalogo.create_deposito(conn, nombre, payload.descripcion.strip())
            return catalogo.get_deposito(conn, did)

    @router.put("/{did}")
    def actualizar(did: int, payload: DepositoUpdatePayload):
        nombre = payload.nombre.strip()
        with abrir() as conn:
            if not catalogo.get_deposito(conn, did):
                raise HTTPException(404, "Depósito no encontrado")
            if not nombre:
                raise HTTPException(422, "El nombre es obligatorio.")
            catalogo.update_deposito(conn, did, nombre, payload.descripcion.strip(), 1 if payload.activo else 0)
            return catalogo.get_deposito(conn, did)

    @router.post("/{did}/set-default")
    def set_default(did: int):
        with abrir() as conn:
            if not catalogo.get_deposito(conn, did):
                raise HTTPException(404, "Depósito no encontrado")
            catalogo.set_default_deposito(conn, did)
            return catalogo.get_deposito(conn, did)

    @router.delete("/{did}")
    def eliminar(did: int):
        with abrir() as conn:
            if not catalogo.get_deposito(conn, did):
                raise HTTPException(404, "Depósito no encontrado")
            try:
                catalogo.delete_deposito(conn, did)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
        return {"ok": True}

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
                    observaciones=payload.observaciones.strip(),
                )
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
        return {"ok": True}

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

    router = APIRouter(prefix=prefix, tags=["stock"])

    @router.get("")
    def listar():
        with abrir() as conn:
            productos = stock.get_stock_todos(conn)
        return {"productos": productos, "alertas": stock.alertas_de_stock(productos)}

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
    def detalle(pid: int):
        with abrir() as conn:
            producto = catalogo.get_producto(conn, pid)
            if not producto:
                raise HTTPException(404, "Producto no encontrado")
            return {"producto": producto, "stock_actual": stock.get_stock_actual(conn, pid)}

    @router.post("/{pid}/ajuste")
    def ajuste(pid: int, payload: AjustePayload, user: dict = Depends(usuario)):
        fecha = payload.fecha or date.today().isoformat()
        referencia = payload.referencia.strip() or "Ajuste manual"
        usuario_id = user.get("id")
        with abrir() as conn:
            producto = catalogo.get_producto(conn, pid)
            if not producto:
                raise HTTPException(404, "Producto no encontrado")
            if payload.modo == "absoluto":
                if payload.cantidad < 0:
                    raise HTTPException(422, "El stock no puede fijarse en un valor negativo.")
                stock.ajustar_stock(conn, pid, payload.cantidad, referencia, usuario_id=usuario_id, fecha=fecha)
            elif payload.modo == "entrada":
                factor = payload.factor or 1
                if factor <= 0:
                    raise HTTPException(422, "El factor de conversión debe ser mayor a 0.")
                unidad_compra = payload.unidad_compra.strip()
                cantidad_base = abs(payload.cantidad) * factor
                ref = referencia
                if unidad_compra and factor != 1:
                    ref = f"{referencia} ({payload.cantidad:g} {unidad_compra} × {factor:g})"
                stock.add_movimiento_stock(conn, pid, "entrada", cantidad_base, ref, usuario_id=usuario_id, fecha=fecha)
            elif payload.modo == "salida":
                stock.add_movimiento_stock(
                    conn, pid, "salida", -abs(payload.cantidad), referencia, usuario_id=usuario_id, fecha=fecha,
                )
            elif payload.modo == "merma" and con_merma:
                motivo = (payload.motivo or "Otro").strip() or "Otro"
                stock.add_movimiento_stock(
                    conn, pid, "merma", -abs(payload.cantidad), f"Merma: {motivo}", usuario_id=usuario_id, fecha=fecha,
                )
            else:
                raise HTTPException(422, "Modo inválido.")
            return {"producto": producto, "stock_actual": stock.get_stock_actual(conn, pid)}

    return router
