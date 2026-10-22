# Arquitectura — LibraCommerce

## Propósito y límites

LibraCommerce es el **motor comercial reutilizable** de la familia Libra:
contratos y lógica de dominio para entidades comerciales, catálogo de productos y
servicios, ubicaciones e inventario, listas de precio, compras y ventas. No es un
producto ni expone una app: cada vertical que lo consuma arma su HTTP y sus
reglas propias, y usa LibraCommerce para el núcleo comercial que de otro modo
duplicaría.

El límite es deliberado: LibraCommerce tiene el **dominio comercial genérico**,
no el negocio específico de un vertical (no sabe de recetas de cocina, historia
clínica ni ARCA). La separación se expresa en su arquitectura hexagonal — el
dominio no depende de la persistencia ni del transporte.

## Arquitectura hexagonal

El paquete está organizado en capas explícitas (puertos y adaptadores), que es lo
que distingue a este motor del acceso a datos más plano de `libracore.db`:

- **`domain/`** — el modelo de negocio puro, sin dependencias de I/O:
  `entities` (`Party`, roles y tipos), `catalog` (`CatalogItem`, `Category`,
  `Unit`, `PriceList`, `ItemPrice`, `ItemVariant`, `ItemCode`), `inventory`
  (`Location`, `StockMovement`, y las reglas del depósito: `exigir_default_activo` y `exigir_deposito_activo`, ADR-051), `sales` (`Sale`, `SaleItem`, `SalePayment`, y la cuenta pura del reintegro prorrateado de una devolución, `reintegro_prorrateado`, ADR-040/043),
  `purchasing` (`PurchaseOrder`, `PurchaseReceipt`), `scale` (lectura de códigos
  de balanza, `parse_scale_barcode`) y `presets` (rubros comerciales y sus ejes
  de variante).
- **`hora.py`** — el reloj del motor (ADR-052, ADR-065): `ahora_utc` (el gancho; lee de `libracore.fechas`, el reloj único de la familia, y sin LibraCore cae a la misma regla), `ahora_local` y `hoy_local` en hora de Argentina; y el **día de Argentina** de un `occurred_at`/`received_at` guardado, con zona o sin ella (ADR-066): `dia_local` (Python), `dia_local_sql` y `filtro_de_dias_sql` (SQLite y PostgreSQL), que usan los reportes por día. Un test (`test_sin_reloj_del_sistema`) impide leer el reloj por otro lado.
- **`ports/`** — los contratos que el dominio necesita del exterior:
  `persistence.CommerceRepository` (la interfaz de almacenamiento) e
  `integrations.CommerceEventPublisher` / `SaleConfirmedEvent` (publicación de
  eventos de dominio).
- **`usecases/`** — la capa de aplicación que orquesta dominio + puertos:
  `sales` (`confirm_sale`, `cancel_sale`), `inventory`
  (`verificar_deposito`, `verificar_disponibilidad`, `transfer_stock`, error `StockInsuficienteError`),
  `purchasing` (`confirm_purchase_receipt`) y `presets`.
- **`db/`** — un adaptador de persistencia concreto: `repository`
  (`SqliteCommerceRepository`), `schema` (`init_schema`), `migrations` (la cadena
  numerada `_migration_0001…0011`, **congelada** desde P9-M0: ver "Migraciones") y
  `auditoria` (`RepositorioAuditado`, que le pasa un `Cambio` a un registrador —el de
  `libraauth`, ADR-050—, y `ActividadRepository`).
- **`migrations/`** — la cadena de Alembic del motor, adentro del paquete para
  viajar en el wheel; se aplica con `libracommerce-migrar` (`migrar.py`).
- **`erp/`** (extra `[erp]`) — los casos de uso comerciales: `catalogo` (productos,
  categorías, depósitos, transferencias), `stock` (ledger de movimientos, ajustes,
  descuento por venta con el gancho de recetas) y `listas_precio` (listas, ítems,
  quiebres por cantidad, ajuste porcentual, importación y el autocompletado del punto
  de venta) y `ventas` (la venta que cruza los dos motores: `sales`/`sale_items`/
  `stock_movements` de acá y `ventas_pagos`/`caja_movimientos`/`turnos_caja` de
  LibraCore en una transacción; la acreditación del QR, la anulación simétrica, los
  vínculos a factura/remito/MercadoPago y el arqueo del turno), `reportes` (ventas por
  período, medios de pago, productos más vendidos, stock bajo y resumen sobre
  `sales`/`sale_items`/`catalog_items`: la implementación del `PuertoDeReportes` de
  LibraCore para un producto que vende con este motor) y `actividad` (las partes
  de ventas y stock de la línea de tiempo de `libracore.db.logs`) y `vencimientos` (lotes y
  vencimientos como dimensión del ledger, opt-in por producto, sin tabla `lots`; ADR-018; `erp/lotes.py` es su piso, sin
  depender de `stock`: saldos por bucket, FEFO de la venta para los productos marcados, lote de origen en la anulación y
  los avisos de la venta; con A-4 PR-3 la devolución de un perecedero va a merma en un par por lote, la transferencia
  mueve el lote por FEFO con un par por tramo y el ajuste y las salidas manuales cuentan un lote o bajan por FEFO: A-4 completo para esos caminos, ver ADR-018), todos con la conexión
  como primer parámetro —salvo `crear_venta_directa`, que recibe la fábrica porque el
  reintento por número repetido necesita una transacción nueva—; y `erp.hooks`, los
  puntos de extensión tipados que un producto engancha (`al_confirmar_venta` /
  `al_anular_venta`, y desde ADR-067 `al_registrar_venta`, `al_acreditar_pago` y
  `al_devolver_items`, corren dentro de esa transacción); y `erp.offline` (ADR-067), las
  ventas en un nodo offline de LibraEdge: `con_outbox(hooks, Outbox(...))` suma a los
  ganchos del producto el encolado de `venta.registrada`, `venta.pago_acreditado`,
  `venta.anulada` y `venta.devolucion` (en la misma transacción) y el numerador con el
  prefijo de la sucursal (`S02-POS-000123`); `registrar_en(Despachador(), hooks)` (libraedge
  v0.7.0) registra los `(conn, operacion)` que las materializan en el central, idempotentes y con la
  colisión de número a revisión manual; y `erp.offline_caja` (ADR-069), la caja y los turnos del
  nodo: `caja_con_outbox(ganchos, Outbox(...))` encola `turno.abierto`, `turno.cerrado`, `caja.movimiento`,
  `caja.movimiento_anulado` y `dia.cerrado` con los `GanchosDeCaja` de libracore (ADR-076), y los mismos
  `registrar_en` los aplican en el central con `origen`/`origen_id`, el turno traducido y el arqueo del nodo
  marcado si no coincide; y `erp.offline_clientes` (ADR-070), los clientes que se dan de alta en el nodo:
  `altas_en_nodo(Outbox(...))` es la `AltasEnNodo` de libracore (ADR-077) que encola `cliente.alta` con el id
  provisorio negativo, y el central la aplica (usa al cliente del mismo CUIT/DNI o lo crea con su id), anota el mapeo de
  libraedge (ADR-021) y traduce el `cliente_id` de `venta.registrada`; y `erp.offline_stock` (ADR-071), el stock de la
  sucursal del nodo: `configurar(Outbox(...))` registra dos ganchos de `db.ganchos_de_movimientos` que llaman los dos escritores
  del ledger (`add_movimiento_stock` y `append_stock_movement`) — en el nodo encola `stock.movimiento` por fila (menos las de una
  venta, una recepción o la carga inicial), en el central se niega (`DepositoDeSucursalConNodo`, a 409 por el producto) a mover un
  depósito de una sucursal con nodo—, `compra.recibida` sube lo que se recibe en el nodo contra las órdenes de la web, y la carga
  inicial (`carga_inicial_del_central` / `aplicar_carga_inicial`) trae las existencias al vincular y guarda la sucursal del nodo en
  `nodo_local` (revisión `0009`), con lo que un tercer gancho (`configurar_deposito_por_defecto`) hace que el depósito por defecto de
  un nodo sea el de su sucursal y no el de la instancia. Con la revisión `0008` (`stock_movements.origen`/`origen_id`).
- **`web/`** (extra `[web]`) — factories de router FastAPI que el producto monta con
  `include_router` y gatea él: `catalogo_router` (`build_productos_router`,
  `build_depositos_router`, `build_stock_router`, con `OpcionesCatalogo`/`OpcionesStock`
  para lo que varía por producto) y `listas_router` (`build_listas_precio_router`,
  `build_quiebres_router` —lo monta quien tenga el add-on mayorista— y
  `build_buscar_productos_router`, el `GET /productos/buscar` histórico) y
  `ventas_router` (`build_ventas_router` con `OpcionesVentas`: stock, nombre del
  cliente y los ganchos; el cobro por QR y la factura desde la venta son de LibraCore) y
  `margen_router` (`build_margen_router`: margen y rotación por producto y por período, sólo
  lectura sobre `erp.margen`; ADR-015) y
  `mermas_router` (`build_mermas_router`: mermas por período con su importe, `GET /api/reportes/mermas`, sólo lectura sobre
  `erp.mermas`; la merma guarda el costo vigente en `stock_movements.unit_cost`, opt-in por `guardar_costo` / `OpcionesStock.con_costo_de_merma`, sin migración;
  los campos de importe llevan `costo` en el nombre para que el producto los filtre por rol; ADR-059) y
  `reposicion_router` (`build_reposicion_router`: qué pedir por producto y por sucursal, sólo lectura sobre
  `erp.reposicion`; ADR-017; los tres exports CSV —margen, reposición y vencimientos— pasan por un único `_csv` que
  neutraliza las celdas de texto que una planilla leería como fórmula, `web/csv_seguro.py`; `build_reposicion_minimos_router`: el stock
  mínimo de un producto por sucursal, `GET`/`PUT /{producto_id}/reposicion/minimos`, revisión `0005`; ADR-024; los parámetros opt-in `estacionalidad`, ADR-023, y
  `descontar_por_vencer`, ADR-025, viajan por `GET`, el export y la generación de órdenes en borrador; los cuerpos de `PUT`/`POST` rechazan `true`/`false` en los campos numéricos, ADR-026; el candado
  del producto de `erp.reposicion` también lo toma `catalogo.update_producto`, ADR-026, y `actualizacion_masiva.aplicar`, que relee el producto después de tomarlo, ADR-027) y
  `vencimientos_router` (`build_vencimientos_router`, de lectura, y `build_vencimientos_escritura_router`, con
  gates por operación: próximos a vencer, lotes de un producto, marcar «vence», asignar vencimiento a saldo sin
  lote, cargar una entrada de stock nuevo con lote (`POST /entrada`), dar de baja un lote —con el costo de la merma, ADR-059— y conciliar el saldo «sin lote»
  negativo heredado de antes de A-4 (`GET /conciliacion`, en el router de lectura, y `POST /conciliar`, ADR-060), sobre `erp.vencimientos`;
  ADR-018; y, opt-in con `OpcionesCatalogo.con_vencimientos`, la marca `vence` en el producto: listado, escaneo, alta y
  edición la devuelven y el alta y la edición la cambian bajo `autorizar_marcar_vence`, y apagada las respuestas de
  productos no cambian). La de escritura **falla al construirse** sin
  `usuario_actual` o sin listas no vacías `dependencias_marcar` y `dependencias_movimientos`, y `asignar`, `entrada`, `merma` y `conciliar`
  exigen `clave_operacion` (idempotencia de los reintentos; única por producto: el cliente genera una clave por intento
  del usuario y por producto). Una `variante_id` que no es del producto es 422.
- **`adapters/`** — puentes hacia afuera: `adapters/contalibra` lee datos del schema
  legado de Contalibra. (`integrations/libraedge`, la traducción de la capa vieja `Sale`
  a LibraEdge, se retiró con ADR-067: ningún producto la usaba; la de hoy es
  `erp.offline`.)
- **`scripts/`** — migración y verificación de datos reales:
  `migrate_from_contalibra` / `migrate_from_restolibra` mueven un producto legado
  al schema del motor, y `verify_*_migration` comparan conteos, stock por
  ítem/ubicación, totales de venta y listas de precio (`VerificationReport`,
  `Discrepancy`) — el motor no da una migración por buena sin contrastarla.

## Puertos que consumen los productos

Medido sobre los consumidores, el uso real entra por la persistencia y el
dominio, no por los usecases: `db.repository` (16 sitios), `domain.catalog` (14),
`db.schema` (9), `domain.inventory` (7), `usecases.inventory` (5),
`domain.sales` (5). Es coherente con el estado del motor: los productos hoy usan
sobre todo el modelo y el repositorio; la capa de casos de uso está lista para
cuando un vertical mueva su lógica de ventas al motor.

## Motor de persistencia

El adaptador de referencia es SQLite (`SqliteCommerceRepository`) con su propia
cadena de migraciones. El contrato `CommerceRepository` está en `ports/`
justamente para que un adaptador PostgreSQL —o cualquier otro— pueda existir sin
tocar dominio ni usecases. La regla de familia PostgreSQL-only vive en el
arranque de cada **producto** que consume el motor, no en el motor.

## Distribución

Paquete `libracommerce` (build `hatchling`), versión pineada al tag por cada
producto (`v0.9.1` al 2026-09). Librería, sin CLI de runtime; los `scripts/` de
migración se corren puntualmente durante una adopción.

## Referencias

- `README.md`, `docs/` — alcance y documentación del paquete.
- Wiki: entidad `libracommerce`, `concepts/estandares-desarrollo`, y la auditoría
  `auditoria-estructural-familia-libra-2026-09`.

## Migraciones

Desde P9-M0 (2026-09-06) el schema evoluciona con **Alembic**, como los otros
motores y los ocho productos. La cadena vive en `libracommerce/migrations/` y
viaja en el wheel; un consumidor la aplica con:

    libracommerce-migrar upgrade --prefijo <producto>

El destino es la base **del dominio** del producto (`<PREFIJO>_DATABASE_URL` y
sus nombres históricos vía `url_de_instancia`), que es donde viven las tablas
de este motor en los cuatro consumidores; nunca la base separada de LibraCore.
La tabla de versión es **`alembic_version_libracommerce`**, porque en
Contalibra y Restolibra `alembic_version` a secas ya es de LibraCore en la
misma base.

La revisión `0001_baseline_commerce` llama a `init_schema()`, que es idempotente
y corre por dentro la cadena numerada vieja (`db/migrations.py`,
`schema_migrations`). Esa cadena queda **congelada**: todo cambio de schema
posterior es una revisión de Alembic nueva, escrita a mano (no hay
`target_metadata`) y ejecutada con `conexion_libracore(op.get_bind())` para que
hable los dos motores. Las instancias vivas **se migran, no se estampan**: el
`upgrade` reaplica lo que ya está, agrega lo que falte y registra la versión.

La primera revisión real es `0002_vencimientos_lotes` (ADR-018): `catalog_items.tracks_expiry` (opt-in por
producto, `DEFAULT 0`) y un índice `stock_movements(item_id, location_id, lot_code)`. Como `init_schema()` está
congelado, **esa columna no existe en una base que sólo corrió el arranque**: `erp.vencimientos` lo dice
(`SinRevision`, 503 en el router) en vez de fallar con un error de SQL.

Las revisiones siguientes siguen el mismo criterio (aditivas, idempotentes, y el código sondea por metadatos si existen): `0003` y `0004` (parámetros y proveedor
por producto de la reposición), `0005` (`item_branch_min_stock`), `0006` (`purchase_receipts.clave_operacion`, ADR-046), `0007_devoluciones_de_servicio`
(`sale_service_returns`, ADR-054: lo devuelto de un servicio, que no tiene stock en el ledger), `0008` (`stock_movements.origen`/`origen_id`, ADR-071) y `0009_sucursal_del_nodo`
(`nodo_local`: la sucursal del nodo, que deduce de la carga inicial que ya tenga; enmienda al ADR-071).

**El DDL se corre de a un proceso por vez (ADR-062).** Con `uvicorn --workers N` cada worker corre en su arranque `init_schema()` y, en el
producto, las tablas de cruce y `repuntar_fk_ventas_pagos`; en PostgreSQL ese «si no existe, crear» no es atómico y dos procesos sobre una
base sin las tablas chocan en el catálogo (`duplicate key … pg_type_typname_nsp_index`). Cada una de esas funciones empieza con
`db.candado.serializar_esquema(conn)`, que es `libracore.candados.esperar_candado_de_transaccion` (ADR-053 del motor) con el nombre
`libracommerce.esquema`: el segundo proceso espera al `commit` del primero y corre sobre lo que dejó. Sobre SQLite no hace nada ni
importa libracore. No cambia ninguna tabla (el esquema sigue congelado). Lo que **no** cubre es el DDL propio de cada producto.

## Capas ERP y web (P9)

El plan P9 (`wiki/analyses/migracion-p9-capa-comercial-libracommerce.md`) mueve
a este motor la orquestación comercial que Contalibra y Restolibra tenían
duplicada. `usecases/` sigue puro; lo que cruza a LibraCore va en `erp/` (extra
`[erp]`), y la API en `web/` (extra `[web]`) como factories de router. Reglas:

- Un caso de uso de `erp/` recibe la conexión abierta y nunca abre una propia
  ni decide el commit: la atomicidad es del llamador.
- `repo.transaction()` **anida** (ADR-031): la profundidad es de la conexión, la transacción exterior es dueña del commit y del rollback, y cada interior es un `SAVEPOINT`
  (una interior que falla y se atrapa revierte sólo lo suyo; `update_producto` y `create_producto` se pueden llamar dentro de la `transaction()` de un producto).
- La variación entre productos entra por `erp.hooks.Hooks` (`resolver_receta`,
  `al_confirmar_venta`, `al_anular_venta`, `al_registrar_venta`, `al_acreditar_pago`,
  `al_devolver_items`, `lista_de_precio_para`, `canales`, `numerador`, ...),
  con defaults que son el comportamiento de Contalibra. **Sin `if producto`.**
- **Un cuerpo con campos `int`/`float` rechaza `true`/`false`** donde un `1` o un `0` cambian algo del negocio (ids,
  cantidades, precios, porcentajes): pydantic los convertiría en `1`/`0` antes de que el motor los vea. Se declara con
  `web/_validacion.sin_booleanos(*campos)` dentro del modelo (todos los routers, ADR-026 y ADR-027; los `Decimal`, los
  `bool` y los `Literal` no lo necesitan). **La guardia que lo comprueba (ADR-028)**: `libracommerce.testing.campos_numericos_que_aceptan_booleano(app)`
  recorre las rutas de una `FastAPI` ya armada, instancia el modelo real con `True`/`False` en cada hoja numérica (también en `list`, `dict` y modelos anidados;
  en query y path, con el texto `"true"`) y devuelve `[(método y ruta, campo, tipo), …]` de lo que **todavía** lo acepta. Cada producto la corre en su suite sobre
  su `create_app()` completa, para ver routers propios y payloads heredados sin `sin_booleanos`:

  ```python
  from libracommerce.testing import campos_numericos_que_aceptan_booleano

  def test_ningun_campo_numerico_acepta_un_booleano():
      assert campos_numericos_que_aceptan_booleano(create_app()) == []
  ```

  Si informa algo: `sin_booleanos` en ese campo; y sólo si un `1` o un `0` no cambian nada del negocio, `ignorar={("POST /api/x", "campo")}` con un comentario
  que lo justifique. No ve un `model_validator(mode="after")` con campos de relleno inarmables ni lo que no es pydantic (la celda de una planilla). Necesita el
  extra `[web]`. **Desde ADR-030 `sin_booleanos` (con `rechazar_booleanos`) y la guardia viven en `libracore` (`libracore.validacion`, `libracore.testing`,
  v1.125.0) y `web/_validacion.py` y `libracommerce/testing.py` sólo las reexportan**: el extra `[web]` pide `libracore>=1.125` (`ventas_router` ya lo importaba sin que
  el extra lo declarara; el núcleo sin `web` sigue con `dependencies = []`).
- Lo financiero (caja, cuenta corriente, tesorería, clientes, logs) sigue en
  LibraCore; este motor lo llama, no lo reemplaza. Donde LibraCore necesita saber
  de dónde salen las ventas lo pide como puerto y este motor lo implementa:
  `erp.reportes.puerto_de_reportes` (M4) para `libracore.reportes_router`, y
  `erp.actividad` para las dos partes del log que leen `sales`/`stock_movements`.
- **`db/schema.py` corre sobre una base vacía, sin LibraCore.** Por eso el DDL de
  `venta_links` —la única tabla con FKs a los dos motores— vive en `erp/schema.py`
  (M5) y lo llama el producto después de los dos `init_*_schema`, no `init_schema()`.

## Dónde se arregla (regla de la familia, 2026-10-03)

**Este repo es un motor: el arreglo de fondo de lo que le toca vive siempre acá**, nunca en cada
producto que lo consume. Lo que otro producto comparte o podría compartir se escribe y se arregla
acá y llega a los productos subiendo el pin; el producto aporta sólo costuras (hooks) y lo suyo
(pantallas, textos, modelo de datos propio). Si falta una costura, se agrega acá. Si un producto
necesita el arreglo antes, se hace acá igual (PR y tag): no se deja «provisorio» en el producto.
Detalle y motivo: `reglas/producto.md` del wiki.
