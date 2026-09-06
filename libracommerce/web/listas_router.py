"""Las factories de router de listas de precio y del autocompletado del punto
de venta (P9-M2, 2026-09-06).

Reemplazan a `web/api/listas_precio.py`, `web/api/mayorista_listas.py` (sólo
Contalibra) y `web/routers/productos.py` de los dos productos. Tres factories y
no una, porque el producto las gatea distinto: la de listas va con el módulo
`listas_precio`, la de quiebres con el add-on `mayorista` (Contalibra) y el
autocompletado con la sesión a secas (lo usan Ventas, Facturas, Presupuestos y
Remitos, que tienen cada uno su gate).

- `build_listas_precio_router`: CRUD de listas, ítems, ajuste porcentual e
  importación. Idéntico en los dos productos salvo docstrings.
- `build_quiebres_router`: quiebres por cantidad y precio efectivo por
  cantidad. Comparte el prefijo `/api/listas-precio`; sus rutas no chocan con
  las de arriba. Restolibra puede montarlo el día que venda por volumen.
- `build_buscar_productos_router`: `GET /productos/buscar` (sin `/api`, es
  la ruta histórica). Lo que variaba: Restolibra devuelve sólo vendibles y no
  filtra por `tipo`; Contalibra filtra por `tipo` para Facturas. Acá `tipo` se
  acepta siempre y `solo_vendibles` es opción.
"""

from collections.abc import Callable
from typing import Any, Literal

from ..erp import listas_precio as lp
from . import fastapi as _fastapi
from .catalogo_router import Conexion, _deps

_fastapi()
from fastapi import APIRouter, HTTPException  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402


class ListaPrecioPayload(BaseModel):
    nombre: str
    descripcion: str = ""


class ListaPrecioUpdatePayload(BaseModel):
    nombre: str
    descripcion: str = ""
    activa: bool = True


class ItemsPayload(BaseModel):
    precios: dict[int, float]


class AjustePorcentualPayload(BaseModel):
    porcentaje: float
    base: str = "lista"
    categoria: str = ""


class ImportarPayload(BaseModel):
    fuente: str
    fuente_lista_id: int | None = None


class QuiebrePayload(BaseModel):
    min_quantity: float
    amount: float


class QuiebresPayload(BaseModel):
    quiebres: list[QuiebrePayload]


def build_listas_precio_router(
    *,
    conexion: Conexion | None = None,
    prefix: str = "/api/listas-precio",
):
    abrir, _ = _deps(None, conexion)
    router = APIRouter(prefix=prefix, tags=["listas_precio"])

    def _exigir(conn, lista_id: int) -> dict:
        lista = lp.get_lista_precio(conn, lista_id)
        if not lista:
            raise HTTPException(404, "Lista de precio no encontrada")
        return lista

    @router.get("")
    def listar():
        with abrir() as conn:
            return lp.get_all_listas_precio(conn)

    @router.post("")
    def crear(payload: ListaPrecioPayload):
        nombre = payload.nombre.strip()
        if not nombre:
            raise HTTPException(422, "El nombre es obligatorio.")
        with abrir() as conn:
            lista_id = lp.create_lista_precio(conn, nombre, payload.descripcion.strip())
            return lp.get_lista_precio(conn, lista_id)

    @router.put("/{lista_id}")
    def actualizar(lista_id: int, payload: ListaPrecioUpdatePayload):
        nombre = payload.nombre.strip()
        with abrir() as conn:
            _exigir(conn, lista_id)
            if not nombre:
                raise HTTPException(422, "El nombre es obligatorio.")
            lp.update_lista_precio(conn, lista_id, nombre, payload.descripcion.strip(), 1 if payload.activa else 0)
            return lp.get_lista_precio(conn, lista_id)

    @router.delete("/{lista_id}")
    def eliminar(lista_id: int):
        with abrir() as conn:
            _exigir(conn, lista_id)
            lp.delete_lista_precio(conn, lista_id)
        return {"ok": True}

    @router.get("/{lista_id}/items")
    def items(lista_id: int, categoria: str = ""):
        with abrir() as conn:
            _exigir(conn, lista_id)
            return lp.get_lista_precio_items(conn, lista_id, categoria)

    @router.put("/{lista_id}/items")
    def guardar_items(lista_id: int, payload: ItemsPayload):
        with abrir() as conn:
            _exigir(conn, lista_id)
            lp.save_lista_precio_items(conn, lista_id, payload.precios)
            return lp.get_lista_precio_items(conn, lista_id)

    @router.post("/{lista_id}/ajuste-porcentual")
    def ajuste_porcentual(lista_id: int, payload: AjustePorcentualPayload):
        with abrir() as conn:
            _exigir(conn, lista_id)
            actualizados = lp.apply_porcentaje_lista(conn, lista_id, payload.porcentaje, payload.base, payload.categoria)
        return {"actualizados": actualizados}

    @router.post("/{lista_id}/importar")
    def importar(lista_id: int, payload: ImportarPayload):
        with abrir() as conn:
            _exigir(conn, lista_id)
            lp.importar_precios_lista(conn, lista_id, payload.fuente, payload.fuente_lista_id)
            return lp.get_lista_precio_items(conn, lista_id)

    return router


def build_quiebres_router(
    *,
    conexion: Conexion | None = None,
    prefix: str = "/api/listas-precio",
):
    """Quiebres por cantidad y el precio efectivo por cantidad (lo consume el
    presupuesto para re-cotizar el renglón cuando cambia la cantidad)."""
    abrir, _ = _deps(None, conexion)
    router = APIRouter(prefix=prefix, tags=["mayorista"])

    @router.get("/{lista_id}/items/{producto_id}/quiebres")
    def ver_quiebres(lista_id: int, producto_id: int):
        with abrir() as conn:
            if lp.get_lista_precio(conn, lista_id) is None:
                raise HTTPException(404, "lista de precios no encontrada")
            return lp.get_quiebres(conn, lista_id, producto_id)

    @router.put("/{lista_id}/items/{producto_id}/quiebres")
    def guardar_quiebres(lista_id: int, producto_id: int, payload: QuiebresPayload):
        vistos: set[float] = set()
        for q in payload.quiebres:
            # La cantidad 1 es el precio base (fila `min_quantity IS NULL`); un
            # quiebre es para MÁS de una unidad.
            if q.min_quantity < 2:
                raise HTTPException(422, "la cantidad mínima de un quiebre tiene que ser 2 o más")
            if q.amount <= 0:
                raise HTTPException(422, "el precio de un quiebre tiene que ser mayor a 0")
            if q.min_quantity in vistos:
                raise HTTPException(422, f"hay dos quiebres con la misma cantidad mínima ({q.min_quantity:g})")
            vistos.add(q.min_quantity)
        with abrir() as conn:
            if lp.get_lista_precio(conn, lista_id) is None:
                raise HTTPException(404, "lista de precios no encontrada")
            lp.set_quiebres(conn, lista_id, producto_id,
                            [{"min_quantity": q.min_quantity, "amount": q.amount} for q in payload.quiebres])
            return lp.get_quiebres(conn, lista_id, producto_id)

    @router.get("/{lista_id}/precio")
    def precio_por_cantidad(lista_id: int, producto_id: int, cantidad: float = 1):
        """`precio: null` si el producto no tiene precio en la lista."""
        with abrir() as conn:
            return {"precio": lp.resolver_precio_por_cantidad(conn, lista_id, producto_id, cantidad)}

    return router


def build_buscar_productos_router(
    *,
    conexion: Conexion | None = None,
    usuario_actual: Callable[..., Any] | None = None,
    prefix: str = "/productos",
    solo_vendibles: bool = False,
    tope: int = 20,
):
    """`GET {prefix}/buscar?q=&lista_id=&tipo=`: el autocompletado del punto de
    venta y los comprobantes. `usuario_actual` es la dependencia de sesión del
    producto (el router histórico la ponía por endpoint, no por `include_router`)."""
    from fastapi import Depends

    abrir, usuario = _deps(usuario_actual, conexion)
    router = APIRouter(prefix=prefix, tags=["productos"])

    @router.get("/buscar")
    def productos_buscar(q: str = "", lista_id: int = 0, tipo: Literal["", "producto", "servicio"] = "",
                         user=Depends(usuario)):
        with abrir() as conn:
            return JSONResponse(lp.buscar_productos(conn, q=q, lista_id=lista_id, tipo=tipo,
                                                    solo_vendibles=solo_vendibles, tope=tope))

    return router
