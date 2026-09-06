"""La capa ERP de LibraCommerce: los casos de uso comerciales que CRUZAN motores.

`usecases/` sigue siendo puro —dominio y repositorio, sin dependencias—. Lo que
va acá es lo que Contalibra y Restolibra orquestan hoy por su cuenta y toca
tablas de los dos motores en la misma transacción: una venta que además de
`sales`/`sale_items`/`stock_movements` escribe pagos, caja, turno y cuenta
corriente de LibraCore. Por eso este subpaquete depende de LibraCore y vive
detrás del extra `[erp]`; el resto del motor sigue con `dependencies = []`.

Reglas de la capa (P9, `wiki/analyses/migracion-p9-capa-comercial-libracommerce.md`):

- Un caso de uso recibe la conexión ya abierta y **nunca** abre una propia ni
  decide el commit: la atomicidad venta + pagos + caja + stock + turno es del
  llamador, como en P7.
- La variación entre productos entra por `hooks.Hooks`, nunca por `if producto`.
- LibraCore se importa **adentro** de cada función, no acá arriba: así
  `libracommerce.erp.hooks` se puede importar sin el extra para tipar un gancho
  del lado del producto.

M0 deja los contratos; M1..M4 traen los casos de uso.
"""

from .hooks import SIN_GANCHOS, GanchoDeVenta, Hooks, Insumo, ListaDePrecioPara, ResolverReceta

__all__ = [
    "SIN_GANCHOS",
    "GanchoDeVenta",
    "Hooks",
    "Insumo",
    "ListaDePrecioPara",
    "ResolverReceta",
]
