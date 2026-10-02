# Decisiones arquitectónicas — LibraCommerce

Registro ADR. Las decisiones no se borran; si dejan de aplicar, se marcan como
reemplazadas. Fechas y motivos salen del código y de la historia registrada en el
wiki (entidad `libracommerce`).

## ADR-001 — Motor comercial genérico, separado del negocio de cada vertical

- Estado: aceptada
- Fecha: 2026-07-25
- Contexto: varios verticales necesitan el mismo núcleo comercial (entidades,
  catálogo, inventario, ventas, compras) sin compartir el negocio específico.
- Decisión: un motor con el dominio comercial genérico; lo específico de un
  vertical (recetas, historia clínica, ARCA) queda afuera.
- Consecuencias: reutilización sin contaminar el paquete común; el vertical arma
  su HTTP y sus reglas.

## ADR-002 — Arquitectura hexagonal explícita (domain / ports / usecases / adapters)

- Estado: aceptada
- Fecha: 2026-07-25
- Contexto: el motor debe poder cambiar de persistencia o de transporte sin
  reescribir el negocio, a diferencia del acceso a datos más plano de
  `libracore.db`.
- Decisión: separar `domain/` (modelo puro, sin I/O), `ports/` (contratos:
  `CommerceRepository`, `CommerceEventPublisher`), `usecases/` (aplicación) y
  `adapters/`/`db/`/`integrations/` (implementaciones concretas).
- Consecuencias: el dominio no depende de la base ni del framework; se paga con
  más capas y ceremonia que un CRUD directo.

## ADR-003 — Adaptador SQLite de referencia, contrato en `ports` para otros motores

- Estado: aceptada
- Fecha: 2026-07-25
- Contexto: se necesita una persistencia concreta ya, pero sin cerrar la puerta a
  PostgreSQL.
- Decisión: `db.repository.SqliteCommerceRepository` como adaptador de referencia,
  con su propia cadena de migraciones incrementales; `CommerceRepository` en
  `ports/` para que exista un adaptador PostgreSQL sin tocar dominio ni usecases.
- Consecuencias: la regla PostgreSQL-only vive en el arranque del **producto** que
  consume el motor, no en el motor.

## ADR-004 — Una migración de datos no se da por buena sin verificarla

- Estado: aceptada
- Fecha: 2026-07
- Contexto: adoptar el motor implica mover datos reales desde el schema legado de
  Contalibra/Restolibra; un conteo verde no prueba que los datos cuadren.
- Decisión: cada `migrate_from_*` tiene su `verify_*_migration` que compara
  conteos, stock por ítem/ubicación, totales de venta y listas de precio, y
  reporta discrepancias (`VerificationReport`, `Discrepancy`).
- Consecuencias: la migración se contrasta contra el origen; una discrepancia se
  ve como dato, no como sorpresa en producción.

## ADR-005 — Integración con LibraEdge como traducción de venta a operación de sync

- Estado: aceptada
- Fecha: 2026-08
- Contexto: LibraCommerce puede correr en el nodo edge de una sucursal offline;
  el nodo sincroniza operaciones, no conoce el dominio comercial.
- Decisión: `integrations/libraedge` traduce una venta confirmada a una operación
  de sincronización (`sale_to_edge_operation`, `apply_confirmed_sale_operation`),
  del lado del motor comercial.
- Consecuencias: LibraEdge queda agnóstico del negocio; el mapeo dominio→sync vive
  en el motor que sí lo conoce.

## ADR-006 — Presets de rubro para el arranque de un comercio

- Estado: aceptada
- Fecha: 2026-08
- Contexto: distintos rubros comerciales tienen ejes de variante distintos
  (talle/color, sabor, etc.) y conviene no obligar a configurarlos desde cero.
- Decisión: `domain/presets` + `usecases/presets` ofrecen rubros con sus ejes
  visibles (`listar_rubros`, `preset_de`, `fijar_rubro`).
- Consecuencias: un comercio nuevo arranca con una configuración razonable de su
  rubro; sigue siendo editable.

## ADR-007 — La cadena de schema es de Alembic y viaja en el wheel (P9-M0, 2026-09-06)

**Contexto.** El motor tenía una cadena numerada propia (`db/migrations.py`,
tabla `schema_migrations`) que corría adentro de `init_schema()` en cada
arranque. Era el único mecanismo de schema de la familia que no era Alembic
(F6.2 del plan de septiembre), y nadie podía invocarlo *antes* de levantar la
app nueva: el `panel_admin.py actualizar` de los consumidores declara
`migraciones=(...)` como comandos, y este motor no aparecía.

**Decisión.** `libracommerce/migrations/` con Alembic, adentro del paquete
(espejo de LibraCore `v1.53.0`), console script `libracommerce-migrar`, tabla de
versión `alembic_version_libracommerce`. La baseline llama a `init_schema()`;
la cadena numerada queda congelada y sostenida por el gate de
`test_schema_congelado.py`. El destino se resuelve a la base **del dominio** del
producto, y un prefijo que no resuelve falla en vez de caer a `DATABASE_URL`.

**Consecuencias.** Un mecanismo de schema por base en vez de tres. Los
consumidores agregan `("libracommerce-migrar", "upgrade", "--prefijo", "<p>")`
a `migraciones` y al `command:` de su compose de dev, y pinean el extra
`[migrations]`. `init_schema()` sigue corriendo en el arranque (es idempotente),
así que una instancia sin migrar no se rompe: sólo se queda sin la versión
registrada hasta el primer deploy que corra el comando.

## ADR-008 — Capas `erp` y `web` con extras, y variación por ganchos (P9-M0, 2026-09-06)

**Contexto.** Contalibra y Restolibra escriben en las tablas de este motor
desde P7/P8, pero cada uno con su propia orquestación (raw SQL), sus routers y
sus pantallas: unas 4.900 líneas de backend por producto, divergidas por deriva
y no por dominio. El humano decidió cerrar el fork consolidando acá.

**Decisión.** Dos subpaquetes detrás de extras, para que el núcleo siga con
`dependencies = []`: `erp/` (depende de LibraCore; casos de uso que cruzan
motores en la misma transacción) y `web/` (FastAPI; factories de router con el
patrón de `libracore.facturas_router`). La variación entre productos entra por
`erp.hooks.Hooks`, tipada y con defaults = Contalibra. Prohibido `if producto`.
Verificado que LibraCore no importa este motor en runtime, así que el extra
`[erp]` no crea un ciclo.

**Consecuencias.** El motor va a triplicar su tamaño durante P9 y hay que
tratarlo como el producto principal mientras dure. Cada módulo (M1..M4) entra
con tres PR —motor, libra-ui, adopción— y el gate es la suite de cada producto
sin tocar.

## ADR-009 — Actualización masiva de precios: recalcular el margen, nunca aceptar el precio ya calculado (2026-09-28)

**Contexto.** Primer ítem del roadmap de producto de VentaLibra (no una
adopción de Contalibra/Restolibra: no existía en ningún producto de la
familia, ver `wiki/analyses/ventalibra-gaps-despensa.md`). Un proveedor manda
una planilla con costos nuevos; decisión del humano (2026-09-28): el precio de
venta se recalcula solo, manteniendo el margen que cada producto ya tenía —no
todos los proveedores mandan un precio de venta sugerido, y lo que hay que
conservar es el margen que el dueño ya venía aplicando.

**Decisión.** `erp.actualizacion_masiva.calcular(conn, filas)` es la única
fuente de verdad del cálculo (no escribe nada); `aplicar(conn,
actualizaciones)` escribe exactamente lo que `calcular` devolvió, reutilizando
`catalogo.update_producto` fila por fila —una actualización masiva es, para el
motor, una `PUT` por producto, no un camino paralelo. El router
(`web/planillas_router.build_actualizacion_precios_router`) **no acepta del
cliente una lista de precios ya resueltos**: los dos endpoints (`/preview` y
`/aplicar`) reciben la MISMA planilla y recalculan desde cero, así que
`aplicar` nunca puede escribir un número que no salga de recalcular el margen,
y una edición manual hecha entre la vista previa y el clic de aplicar se lee
fresca. `openpyxl` entra por un extra nuevo, `[planillas]`, separado de
`[web]`: sólo este router lo necesita, y sumarlo a `[web]` se lo impondría a
cualquier producto que monte cualquier otra factory de esa capa.

**Consecuencias.** Un producto con `precio_costo=0` (recién creado, sin costo
cargado todavía) no tiene margen del que partir: la actualización masiva le
cambia el costo pero no toca el precio de venta, y lo marca (`margen_calculado
= False`) para que la pantalla lo distinga. El matcheo es por cualquier código
del producto (`item_codes`, no sólo el principal): una planilla con el código
de barra de una presentación secundaria también encuentra el producto.

## ADR-010 — Lista de precios de un cliente: el enganche entra al motor, no sólo la lista (2026-09-28)

**Contexto.** ADR-008/P9-M2 movió `price_lists`/`item_prices` a este motor,
pero dejó afuera el enganche cliente→lista del add-on mayorista de Contalibra
(`app/db_mayorista.py` + `app/web/api/mayorista.py`): una tabla
`cliente_lista_precio` y su router quedaron como código propio de Contalibra,
sin que VentaLibra tuviera dónde montar lo mismo pese a tener listas de precio
propias desde F1 (2026-09-14). El pedido del humano fue explícito: no quiere
dos formas de resolver esto, una por producto — si se corrige algo, tiene que
impactar en el motor, sin importar que cada producto muestre una pantalla más
o menos.

**Decisión.** `erp.schema.crear_cliente_lista_precio` (mismo criterio que
`crear_venta_links`, ADR-008: la tabla tiene FK a `clients` de LibraCore y a
`price_lists` de este motor, así que vive en `erp/` y la crea el
`init_schema_propio()` del producto, no una migración de este repo).
`erp.listas_precio.get/set/quitar_lista_de_cliente` son la extracción literal
de `db_mayorista.py`. `web.listas_router.build_cliente_lista_router` expone
`GET`/`PUT /api/clientes/{id}/lista-precio`, aparte de
`build_listas_precio_router` por el mismo motivo que los quiebres: cada
producto lo gatea distinto. La existencia del cliente se resuelve contra
`libracore.db.clients.get_client` directo (mismo patrón que
`ventas_router._nombre_de_cliente_default`), no con un gancho nuevo: los dos
productos que lo montan ya usan el `clients.id` de LibraCore sin traducir.

**Consecuencias.** Contalibra retira `db_mayorista.py`/`mayorista.py` y monta
esto con el mismo gate por add-on que ya tenía; su tabla `cliente_lista_precio`
existente no se toca (mismo nombre, mismas columnas, mismas FK). VentaLibra lo
monta sin gate (módulo siempre libre) y gana la card "Lista de precios
(mayorista)" que la ficha del kit (`libra-ui/comercio/ClienteDetalle`) ya tenía
lista con la prop `conListaDePrecio`, apagada por falta de este endpoint. Si
Restolibra alguna vez vende por volumen, monta el mismo router sin escribir
nada nuevo.

## ADR-011 — Listas de precio: marcar una como predeterminada, cerrando una capacidad muerta desde P9-M2 (2026-09-28)

**Contexto.** `repository.resolve_price` ya sabía resolver sin `price_list_id`
explícito cayendo a la lista `is_default=1 AND active=1` (probado desde
`test_repository.py`), pero **nunca hubo forma de marcar una lista como
default**: `create_lista_precio` no la setea y `update_lista_precio` sólo la
preserva. Hallazgo hecho investigando el roadmap de producto de VentaLibra
(promociones y combos): ningún producto de la familia usa `resolve_price` sin
pasar el `lista_id` a mano, así que esa rama de código estaba escrita, probada
al nivel del repositorio, y completamente inalcanzable desde la capa HTTP.

**Decisión.** `erp.listas_precio.set_lista_precio_default(conn, lista_id)` y
`POST /{lista_id}/set-default` en `build_listas_precio_router`, mismo patrón
exacto que `catalogo.set_default_deposito`/`POST /{did}/set-default` de este
mismo repo: limpia el default anterior antes de marcar el nuevo (el índice
único parcial de `price_lists` no admite dos a la vez), y no deja marcar como
default una lista inactiva (dejaría a `resolve_price` sin ninguna lista que
resolver, no cae en silencio a la inactiva).

**Consecuencias.** Recién con esto un producto puede, por primera vez,
resolver "el precio de este producto ahora" sin conocer de antemano el id de
una lista — la pieza que faltaba para que un carrito (POS) consulte precio por
cantidad y vigencia en vivo. VentaLibra es quien lo va a consumir primero (ver
su propio `DECISIONS.md`); Contalibra y Restolibra no se tocan.

De paso, mismo hallazgo de "capacidad escrita y nunca alcanzable": `set_precio_
vigente` sólo sabía insertar (nunca reemplazaba una fila de `item_prices`
existente), así que no había forma de cancelar una promoción con vigencia
antes de que venciera sola. Se agrega `erp.listas_precio.delete_precio_
vigente(conn, producto_id, vigencia_id)` y `DELETE /items/{producto_id}/
vigencias/{vigencia_id}`, con `item_id` en el `WHERE` (no alcanza con acertar
el id: tiene que ser del producto que dice la URL).

## ADR-012 — Sucursal es una tabla propia del motor (`branches`), no una columna de producto (2026-09-28)

**Contexto.** El humano encontró, revisando VentaLibra, dos modelos
incompatibles conviviendo en la familia: VentaLibra (PR `ventalibra#314`,
2026-09-26) modela sucursal y depósito como pares planos del mismo
`locations.location_type` (`store`/`warehouse`), sin jerarquía — cualquiera
puede tener stock. [[libradesk]] (capa de producto sobre Contalibra,
2026-08-14) ya tenía construida la jerarquía real: una tabla `sucursales`
propia del producto, con `locations.branch_id` apuntando a ella (columna
suelta del motor desde Fase 4, sin FK — Contalibra la deja en `NULL`
siempre). Decisión del humano: el modelo de LibraDesk queda como estándar,
y sube al motor para que los cuatro consumidores lo reciban en vez de que
cada uno lo porte por su cuenta — mismo criterio que la convergencia de
`verificar_disponibilidad()`/`transfer_stock()` (ver
`wiki/analyses/donde-vive-el-stock-familia-libra.md`). Detalle completo y
las fuentes cruzadas: `wiki/analyses/jerarquia-sucursal-deposito-libracommerce.md`.

Se evaluaron dos caminos: (A) auto-referencial sobre `locations` (una fila
`location_type='store'` ES la sucursal, sin tabla nueva) o (B) una tabla
`branches` propia, con `locations.branch_id` apuntando a ella. El humano
eligió (B): sucursal y depósito son tipos de entidad distintos, no la misma
tabla con un flag, y es el modelo que LibraDesk ya tiene probado en
producción.

**Decisión.** Tabla `branches` nueva (`id`, `name`, `code`, `address`,
`active`, `is_default`, `created_at`), puramente aditiva — no migra ninguna
tabla existente. `locations.branch_id` sigue **sin FK real** contra
`branches.id`: es la misma columna suelta que ya existía desde Fase 4 (ahora
con contenido del otro lado), y agregarle la FK es una migración de datos
sobre las bases ya desplegadas de Contalibra/VentaLibra/LibraDesk, fuera de
este alcance. `erp.catalogo` gana la sección "Sucursales"
(`create_sucursal`/`update_sucursal`/`validar_sucursal`/...), con
`_verificar_baja_de_sucursal` portada **literal** de
`libradesk/app/services/comercial.py::_verificar_baja_de_sucursal` —incluida
su corrección del 2026-08-16 (mira existencias `<> 0`, no sólo depósitos
`active=1`)—, y `web/catalogo_router.build_sucursales_router` con
`OpcionesSucursales`, mismo patrón de extensión que `OpcionesDepositos`.
`create_deposito` suma un `branch_id` opcional, validado contra `branches`.
Un producto que no monta el router (Contalibra) sigue exactamente igual que
antes.

**Consecuencias.** Esto es sólo el motor (fase 0 de la migración). Quedan
afuera, cada uno su propia tanda con datos reales: (1) VentaLibra tiene que
migrar del modelo plano de `location_type` al jerárquico sin perder el
historial de stock de dev/demo; (2) LibraDesk tiene que migrar su tabla
`sucursales` de producto a la del motor, sin duplicar el concepto; (3)
Contalibra base tiene que prender el sustrato, hoy apagado a propósito
(`branch_id=None` hardcodeado, 9 consultas de listas de precio con
`AND branch_id IS NULL` como invariante — prenderlo no es aditivo).
Restolibra queda afuera mientras no adopte este motor.

## ADR-013 — Toda sucursal activa tiene al menos un depósito activo, y uno es el de venta (2026-09-28)

**Contexto.** La regla que originó ADR-012 —«toda sucursal declara como mínimo un
depósito, y el stock vive sólo en depósitos»— quedó sin imponer en la Fase 0:
`create_sucursal` no creaba depósito y nada protegía al último depósito de una
sucursal; sólo estaba guardada la baja de la sucursal. Además, con varios
depósitos por sucursal, «de cuál descuenta una venta» no tenía respuesta: la
única marca (`locations.is_default`) es global a la instancia. Decisiones del
humano (2026-09-28): el motor impone la invariante, y el depósito de venta es un
**predeterminado por sucursal**, no «el de menor id».

**Decisión.** `create_sucursal` crea la sucursal **y su primer depósito**
(parámetros `deposito`/`deposito_tipo`; sin nombre, «Depósito <sucursal>»), que
queda como predeterminado. `branches.default_location_id` guarda cuál es (sin FK,
mismo criterio que `locations.branch_id`; la tabla es nueva en ADR-012 y no había
salido en ningún tag, así que la columna entra en su `CREATE TABLE` y no en una
migración). `get_deposito_de_venta(conn, sucursal_id)` lo resuelve y repara solo
un predeterminado ausente o inactivo (el activo de menor id);
`set_deposito_predeterminado` y `POST /api/sucursales/{id}/deposito-predeterminado`
lo cambian. No se puede desactivar ni eliminar el **último depósito activo de una
sucursal activa** (`update_deposito`/`delete_deposito`); si se quita el
predeterminado, pasa a otro activo.

**Diferencia deliberada con LibraDesk.** Esa invariante hace imposible su guarda
«no se da de baja una sucursal con depósitos activos»: no se podría desactivar el
último depósito antes que la sucursal, ni la sucursal antes que sus depósitos. Lo
que esa guarda protegía —que el stock no quede invisible— lo cubre el chequeo de
**existencias** (`<> 0`, incluidos los depósitos ya inactivos), que se conserva.
Un depósito activo pero vacío ya no bloquea: **la baja de la sucursal da de baja
sus depósitos**, y **reactivarla reactiva su predeterminado** (o el de menor id,
o crea uno si no tuviera ninguno). La baja también se planta si la sucursal
contiene el depósito por defecto de la instancia (`is_default`), que `update_deposito` no deja
desactivar.

**Consecuencias.** Las sucursales que ya existan sin depósito (datos de LibraDesk
o de una migración) no se reparan solas: `get_deposito_de_venta` devuelve `None`
para una sin ningún depósito activo y quien llama decide, y las pantallas ven
`depositos = 0` en el listado. Al migrar VentaLibra y LibraDesk (fases siguientes)
esas sucursales tienen que recibir su depósito en la propia migración. Un depósito
sin sucursal (Contalibra, Restolibra) conserva exactamente las guardas de antes.
`OpcionesSucursales.al_guardar` recibe la sucursal ya con su depósito
(`deposito_predeterminado_id`).

## ADR-014 — Promociones: "llevá N pagá M" y combos como pieza del motor (2026-09-28)

**Contexto.** Roadmap de producto de VentaLibra, segundo ítem: "promociones y
combos". A diferencia de las listas de precio o el enganche cliente↔lista, no
hay nada que extraer: ningún producto de la familia tenía promociones por regla,
sólo precio por cantidad y por vigencia (`erp.listas_precio`). Se construye en el
motor, no en el producto, para que Contalibra y Restolibra puedan montarlas.

**Decisión.**
- Dos tipos, un solo modelo (`promotions` + `promotion_items`): `nxm` (un producto,
  `cantidad` = las que se llevan, `paga` = las que se pagan; 2x1 es `2/1`) y `combo`
  (dos o más productos distintos a un `precio` cerrado del paquete).
  `erp.promociones` (CRUD, `calcular`, `registrar_aplicadas`) y las factories
  `build_promociones_router` (CRUD, de admin) y `build_promociones_calculo_router`
  (`POST /calcular`, sólo lee, para el cajero). El DDL sale por
  `erp.schema.crear_promociones`, mismo criterio que `crear_cliente_lista_precio`.
- **La promoción no toca las líneas.** Sigue habiendo una línea por producto a su
  precio de lista; el ahorro viaja en el `descuento` de la venta, y `sale_promotions`
  anota qué promoción se aplicó, cuántas veces y cuánto ahorró (con el nombre y el
  monto de ese momento: borrar o editar la promoción después no reescribe lo
  vendido). Así el stock, el costo por línea y las devoluciones siguen veraces por
  producto, que es lo que repartir el ahorro en el precio de cada línea rompía.
- **El servidor es la autoridad.** `OpcionesVentas.promociones` (default `False`:
  Contalibra y Restolibra no cambian) hace que `POST /api/ventas` calcule las
  promociones con las líneas que llegan y las sume al descuento (con tope en el
  subtotal), en la misma transacción. `POST /calcular` es la vista previa del
  cajero y usa la misma función. `registrar_venta`/`crear_venta_directa` reciben
  `promociones=` (aditivo, `None` no escribe nada).
- **Cuando compiten, gana la de mayor ahorro por paquete** (empate: la más vieja),
  consume las unidades y las demás se calculan con lo que sobra. Predecible, no
  óptimo global.

**Consecuencias.** Hay que llamar a `crear_promociones(conn)` desde el schema del
producto (VentaLibra: migración `0006`). Una promoción con horario se compara en
hora local, igual que las vigencias de precio.

**Arreglo que viaja con esto.** `erp.listas_precio.precio_vigente` recibía `en`
tal cual: un instante en UTC (`...Z`, lo que manda `new Date().toISOString()`) se
comparaba como texto contra vigencias guardadas en hora local, así que una
promoción de 18 a 20 hs se activaba a las 15 en Argentina. Ahora un instante con
zona se pasa a hora de Argentina antes de comparar (`a_hora_local`, desfase fijo
de −3: no hay horario de verano y no se depende de la base de zonas de la
imagen). Lo introdujo el cableado del POS de la tanda anterior (ADR-011).


## ADR-015 — Margen y rotación: una lectura del motor, con el costo que haya y avisando cuál (2026-09-29, v0.26.0)

**Contexto.** Roadmap de producto de VentaLibra, tanda 1: "reportes de margen y rotación".
`erp.reportes.reporte_productos_top` suma cantidad y total por producto; nadie restaba el costo.
Decisión vigente: el motor es el origen, así que la agregación vive acá y el producto sólo monta
la factory (y la pantalla es del kit, `libra-ui`).

**Decisión.**
- `erp.margen.reporte_margen` (sólo lee, sin migraciones) y `web.margen_router.build_margen_router`:
  `GET /api/reportes/margen` (resumen, productos y períodos), y los CSV en `/export/productos` y
  `/export/periodos` bajo el mismo prefijo. Ingreso, costo, margen ($ y %) y unidades (la rotación:
  del rango, por día y por período) por producto y por período; orden por `orden`/`sentido`; `producto_id`
  deja las tres partes sobre un producto. El gate (admin) lo pone el producto al montarlo.
- **Qué es una venta.** `sales.status` en `confirmed`, `partially_returned` y `returned`; una anulada
  (`cancelled`) y una pendiente de cobro (`draft`) no cuentan, igual que `puerto_de_reportes(solo_confirmadas=True)`.
- **Las devoluciones se restan del ledger.** `devolver_items` no toca `sale_items` ni `sales.status`
  (la venta sigue `confirmed`; sólo cambia `status_detail`, el stock y la caja), así que lo devuelto se lee de
  `stock_movements` por las dos formas de anotarlo (`reason_code='devolucion'` y `source_type='sale_return'`) y se
  descuenta de unidades, ingreso y costo en la misma proporción. Un producto devuelto entero no aparece.
- **El costo.** `sale_items.unit_cost_snapshot` si existe; si no, el `default_cost` de hoy **marcado como
  estimado** (`costo_estimado`); si tampoco hay, la línea cuenta con costo 0 y queda marcada (`sin_costo`), porque un
  margen de 100% que no lo es no debe leerse como real. 🔴 **`erp.ventas.crear_venta` —el camino de `POST /api/ventas`—
  no escribe `unit_cost_snapshot`**: sólo lo llena `db.repository.save_sale` con un `SaleItem` del dominio. Hoy, para las
  ventas de mostrador, todo costo sale como estimado. Guardar el snapshot al vender es un cambio de escritura de la
  venta (una columna que ya existe, sin migración) que **no se hizo acá**: queda como decisión aparte.
- **El descuento.** El de línea se resta de la línea; el de la venta (`sales.discount_total`, donde viajan el
  descuento manual y el ahorro de las promociones) se reparte entre las líneas en proporción a lo que valen, sin volver
  a restar lo que ya explican los descuentos de línea. No se descuenta IVA (`crear_venta` guarda `tax_amount` en 0).
- Se agrega en Python (Decimal) y no en SQL, para que el reparto y el neto de devoluciones no dependan de SQL que
  corra distinto en SQLite y PostgreSQL. Costo: recorre las líneas del rango; para el volumen de un comercio alcanza.

**Consecuencias.** Un producto lo monta con `app.include_router(build_margen_router(conexion=...), dependencies=admin_only)`.
Agrupa por producto, no por variante. No incluye stock ni cobertura en días (rotación = unidades vendidas).

**Nota (2026-09-29, revisión de Codex sobre v0.26.0).** Tres ajustes a `erp.margen`, sin cambiar lo decidido arriba:
(1) el rango se compara **por día**: `sales.occurred_on` es texto libre y `POST /api/ventas` acepta `fecha` con hora, y
`occurred_on <= 'AAAA-MM-DD'` dejaba afuera las ventas de ese último día que traían hora; ahora `desde` va por su
fecha y `hasta` como cota exclusiva del día siguiente (mismo SQL en SQLite y PostgreSQL; también rige para el ledger de
devoluciones, que usa el mismo filtro). (2) Una línea devuelta entera se saltea antes de acumular, así que ya no arrastra
`costo_estimado` ni `sin_costo` al producto, al período ni al resumen. (3) **Límite conocido, no arreglado:** la
devolución se prorratea por cantidad entre las líneas del mismo (ítem, variante). El ledger de `devolver_items` no trae
la línea (ni `sale_item_id` ni la posición: `source_id` es la venta y `reason_code='devolucion'`), y el motor trata esas
líneas como un pozo común; con dos líneas del mismo producto a distinto precio o snapshot, devolver la de $100 de una
venta de $100 + $200 da $150 de ingreso en vez de $200. Atribuirla por línea exige que el ledger guarde la línea al
devolver (un cambio de escritura de `devolver_items`), y queda como decisión aparte. `listar_ventas` y `erp.reportes`
tienen el mismo `<=` sobre `occurred_on`; no se tocaron acá.

## ADR-016 — La venta de mostrador puede guardar el costo de cada línea, y sólo si se lo pide (2026-09-29)

**Contexto.** ADR-015 (margen y rotación) dejó dicho que `erp.ventas.crear_venta` —el
camino de `POST /api/ventas`— no escribe `sale_items.unit_cost_snapshot`, así que todo
costo de una venta de mostrador sale como estimado (el `default_cost` de HOY, no el de
aquella venta), y anotó "guardar el snapshot al vender" como decisión aparte. Es esa
decisión. La columna ya existe (sin migración): la llena `db.repository.save_sale` con un
`SaleItem` del dominio, pero no la venta del POS.

**Decisión.**
- **Opt-in, aditivo.** `crear_venta`, `registrar_venta` y `crear_venta_directa` reciben
  `guardar_costo: bool = False`, y `OpcionesVentas.guardar_costo` (default `False`) lo
  propaga desde `POST /api/ventas`: mismo patrón que `caja_con_turno` y `promociones`.
  Con `False` el `INSERT` de `sale_items` es carácter por carácter el de siempre —
  Contalibra y Restolibra, que no lo mandan, no ven ninguna diferencia—.
- **Qué se guarda.** Por cada línea con `producto_id`, el `catalog_items.default_cost`
  vigente en el momento de la venta, leído en la misma transacción (una consulta por
  venta, no una por línea). El costo **no** vive en la variante (`item_variants` no tiene
  columna de costo): una línea con `variante_id` toma el del producto.
- **Qué NO se guarda.** Las líneas de servicio o ad-hoc (sin `producto_id`) quedan en NULL.
  Un producto sin costo cargado también: `default_cost` es `NOT NULL DEFAULT 0`, y ese 0
  significa "nadie lo cargó", no "costó cero"; guardarlo daría un margen de 100% con cara
  de dato real. NULL deja que `erp.margen` lo trate como lo que es (`sin_costo`).
- **El margen no cambia de semántica.** `erp.margen._lineas_netas` ya prefería
  `unit_cost_snapshot` cuando no es NULL y marcaba `costo_estimado` sólo si caía al
  `default_cost`: con el snapshot guardado la línea deja de estar estimada y no se mueve
  si el costo cambia después. Sin opt-in, todo sigue igual.

**Consecuencias.** Un producto que quiera márgenes reales en las ventas nuevas monta
`OpcionesVentas(guardar_costo=True)`. **No hay backfill**: las ventas ya hechas siguen con
NULL (y con costo estimado en el margen) porque el costo de aquel día no se puede
reconstruir; un backfill con el costo de hoy sería exactamente la estimación de siempre,
pero sin avisarlo. Un producto con receta se costea con su propio `default_cost`, no con la suma de
sus insumos: es lo que hoy guarda el catálogo.

## ADR-017 — Reposición sugerida: «qué pedir» por rotación, con el mínimo como piso, por sucursal y sin generar la orden (2026-09-30)

**Contexto.** Roadmap de producto de VentaLibra, B-1 (decisión del humano, 2026-09-29: empezar por reposición
sugerida). El motor ya tenía las piezas sueltas —rotación (`erp.margen`, ADR-015), stock por depósito
(`erp.stock.get_stock_por_deposito`), órdenes de compra (`erp.compras`), `catalog_items.min_stock`— pero nadie las
cruzaba: `alertas_de_stock` sólo compara el stock con el mínimo. Decisiones ya tomadas: rotación de los últimos N días
con `min_stock` como piso; se calcula por **sucursal** (con la opción de la instancia entera) y se recibe por
depósito; **sólo sugiere**, no genera la orden de compra. Sin migración ni schema nuevo, y libre de planes.

**Decisión.**
- `erp.reposicion.sugerencia_reposicion` (sólo lee) y `web.reposicion_router.build_reposicion_router`:
  `GET /api/reportes/reposicion` y `GET .../export` (CSV). Parámetros: `dias_rotacion` (30), `dias_cobertura` (15),
  `plazo_entrega_dias` (3), `sucursal_id`, `categoria`, `producto_id`, `solo_a_pedir` (`true`). Enteros de 1 hasta su
  tope (365, 365 y 180); uno inválido, o una sucursal que no existe, es 422. El gate lo pone el producto al montarlo.
- **La fórmula.** `necesidad = unidades_netas_vendidas × (dias_cobertura + plazo_entrega_dias) / dias_de_muestra` y
  `sugerido = max(0, ceil(necesidad − stock − en_camino))`; si `stock + en_camino < min_stock`, al menos lo que falta
  para llegar: `max(sugerido, ceil(min_stock − stock − en_camino))`. Sale también `motivo` (`bajo_minimo`,
  `por_rotacion` o `ambos`), la rotación diaria, la cobertura actual en días (`None` sin rotación) y las banderas
  `sin_ventas`, `posible_quiebre` y `variantes` (cuántas activas: se agrupa por producto, no por variante). Se
  redondea hacia arriba a la unidad (`units.allows_fraction`: entero, o `decimal_scale` decimales, 3 si no lo trae).
  Ordena por urgencia: menor cobertura primero, luego mayor `sugerido`; los que no rotan, al final.
- **La rotación es la del margen, no otra.** `erp.margen.unidades_netas` reusa `_lineas_netas` sin cambiarle la semántica:
  una venta es `confirmed`, `partially_returned` o `returned`; la anulada y la pendiente de cobro no cuentan; las
  devoluciones se restan del ledger. Sólo se agregó `venta_id` a la línea que ya devolvía y el reparto por sucursal.
- **Cómo se cuentan las ventas por sucursal.** 🔴 `sales.branch_id` **no alcanza**: `erp.ventas.crear_venta` (el camino
  de `POST /api/ventas`) no lo escribe —sólo `db.repository.save_sale` con un `Sale` del dominio—, así que toda venta
  de mostrador lo trae en NULL. La sucursal de una venta es `sales.branch_id` si viene y, si no, la de su depósito
  (`locations.branch_id` de los movimientos `movement_type='sale'` con `source_type` `venta` o `sale`). Una venta sin
  ninguna de las dos no entra en ninguna sucursal, pero sí en el total. El stock de una sucursal es la suma de sus
  depósitos **activos**; el de la instancia, la de todos los depósitos activos (con o sin sucursal). Un depósito
  desactivado deja de contar.
- **En camino.** Cantidad pedida menos recibida por línea, sin bajar de 0, sobre las órdenes que no son `received` ni
  `cancelled`. **`draft` cuenta**: el motor no tiene ninguna operación que pase una orden a `sent` (nace `draft`, y
  `confirmar_recepcion` la deja `partial` o `received`), así que la que ya se le pidió al proveedor está en `draft`.
  Con `sucursal_id` entran las de esa sucursal **y las sin sucursal** (`purchase_orders.branch_id` es opcional; no
  saber a dónde va no es motivo para pedirlo dos veces), y esa parte se informa aparte (`en_camino_sin_sucursal`).
- **El sesgo por quiebres.** Un producto sin stock no vende y dividir por N subestima su rotación. Del ledger de los
  depósitos que se miran se arma el saldo día por día; un día en que el saldo **nunca fue positivo y no hubo ninguna
  venta** no cuenta (`dias_con_stock = N − esos días`, la muestra nunca baja de 7 días, o de N si es menor). Un día con
  ventas nunca se excluye: si vendió, había —un producto cuyo inventario nunca se cargó no se sobrestima—. Cubre de
  paso al producto nuevo. `posible_quiebre` es «stock ≤ 0 o algún día sin stock en la ventana». El stock negativo se
  toma como 0 en la cuenta (se muestra el real): pedir para «tapar» inventario que nunca se cargó es peor que pedir de
  menos.
- Se agrega en Python (Decimal) y las consultas son las mismas en SQLite y PostgreSQL, como el margen. Sólo productos
  activos, `item_type='product'` y `purchasable`.

**Se difiere, a propósito.** `lead_time_days` y `max_stock` por producto (hoy el plazo y la cobertura son del pedido, y el
piso es `min_stock`); proveedor por producto (no hay a quién dirigir la sugerencia); generar la orden de compra en
borrador; estacionalidad (una ventana de N días no distingue un diciembre); el reparto entre sucursales de un pedido
único; y reposición por depósito.

**Límites conocidos.** (1) **El sesgo por quiebres se corrige a medias**: es por día (dos movimientos del mismo día que
suben y bajan el saldo no se distinguen), depende de que el ledger esté bien cargado, y un producto que estuvo sin
stock casi toda la ventana rota sobre pocos días (piso de 7). La bandera avisa; no reemplaza el criterio de quien
pide. (2) `min_stock` es global al producto, no por sucursal: con varias sucursales el piso se aplica igual a cada una.
(3) Una orden abandonada en `draft` cuenta como pedida. (4) Un comercio que no lleva stock en el ledger no tiene qué
sugerir (todo sale con stock 0), y sus ventas sin depósito con sucursal no entran en ninguna sucursal. (5) La rotación
usa el «hoy» del servidor, no el de Argentina. (6) Un producto con receta (Restolibra) se sugiere por sus ventas, no
por el consumo de sus insumos.

**Consecuencias.** Un producto lo monta con `app.include_router(build_reposicion_router(conexion=...), dependencies=...)`;
la pantalla es del kit (`libra-ui`) y la capacidad que la gatea, del producto. No escribe nada, así que no hay nada que
deshacer.

## ADR-018 — Vencimientos y lotes, la parte informativa: el lote es una dimensión del ledger y se activa por producto (2026-09-30)

**Contexto.** Roadmap de producto de VentaLibra, A-1 (decisión del humano, 2026-09-30: arrancar con los defaults
recomendados). Contalibra, Restolibra y VentaLibra comparten este motor, así que lo que entra tiene que ser **aditivo y
opt-in**: un producto que nadie marca no cambia en nada. El motor ya tenía `stock_movements.lot_code` y `expires_at`
(desde la baseline) y la recepción de compras ya los escribía (`usecases.purchasing.confirm_purchase_receipt` →
`repository.append_stock_movement`, con `expires_at` como `datetime.isoformat()`), pero `erp.stock.add_movimiento_stock`
no los aceptaba y nada los leía. Esta decisión es sólo la parte **informativa**: aviso de lo que vence, lotes visibles,
asignar un vencimiento al stock que no lo tiene y dar de baja un lote. **No toca el camino de ventas** (eso es A-4).

**Decisión.**
- **Sin tabla `lots`: el lote es una dimensión del ledger.** Las existencias por lote son `SUM(quantity_delta)`
  agrupado por producto, depósito, variante, `lot_code` y `expires_at`. El bucket con lote y vencimiento en NULL es el
  stock **«sin lote»**. Una tabla aparte sería una segunda fuente de verdad que se desincroniza del ledger (el mismo
  argumento de ADR-007 contra re-expresar el schema), y partir el ledger en más grupos no cambia el stock total de un
  producto/depósito: `get_stock_actual` y `get_stock_por_deposito` siguen sumando todo (hay un test).
- **Revisión Alembic `0002_vencimientos_lotes`** (la primera revisión real de la cadena; `init_schema()` sigue congelado y
  por eso las fixtures del gate `test_schema_congelado` **no cambian**): `ALTER TABLE catalog_items ADD COLUMN
  tracks_expiry INTEGER NOT NULL DEFAULT 0` y `CREATE INDEX IF NOT EXISTS idx_stock_item_location_lot ON
  stock_movements(item_id, location_id, lot_code)`. El mismo texto en SQLite y PostgreSQL (sin índice parcial).
  Idempotente por introspección (la columna sólo se agrega si falta), con `downgrade` (baja el índice y la columna;
  el ledger, que es aditivo, no se toca; perder la columna borra la marca «vence», no los lotes). **Compatibilidad con
  los tres productos:** no reescribe ninguna fila (todos los productos quedan en 0), y un producto que no marque nada
  sigue escribiendo el mismo `INSERT` de siempre. Verificado con `upgrade head` sobre una base con datos de la revisión
  anterior en los dos motores, ledger idéntico antes y después. 🔴 **Como `init_schema()` no la crea, una base que sólo
  corrió el arranque no tiene la columna:** el producto tiene que declarar `libracommerce-migrar upgrade --prefijo ...`
  en su deploy **antes** de montar los routers (ADR-007). Sin ella `erp.vencimientos` levanta `SinRevision` y el router
  responde 503 con el comando; nada del resto del motor lee la columna.
- **`erp.stock.add_movimiento_stock`** recibe `lot_code` y `expires_at` opcionales. Sin ellos el `INSERT` es carácter por
  carácter el de hoy (hay un test que lo compara). `lot_code` se recorta, no puede quedar vacío ni pasar de 64
  caracteres; `expires_at` acepta un `date`, un `datetime` o un texto ISO 8601 y se guarda como `'AAAA-MM-DD'` (de un
  `datetime` se toma la fecha tal como viene, sin convertir de zona); cualquier otra cosa es un `ValueError` **antes** de
  escribir. La recepción de compra **no se tocó**: ya llegaba con lote y vencimiento al ledger (hay un test que lo
  prueba en los dos motores); como esa vía guarda `AAAA-MM-DDTHH:MM:SS`, el lector normaliza a fecha al agrupar.
- **`erp.vencimientos`:** `marcar_vence` (la marca `tracks_expiry`; no escribe movimientos; un servicio no se marca),
  `lotes_de` (existencias por lote, incluido el bucket sin lote y los saldos negativos), `proximos_a_vencer` (por lote
  con saldo > 0 de los productos marcados y activos: producto, código, depósito, sucursal, lote, vencimiento, días para
  vencer —negativo = vencido—, saldo y estado `vencido` | `por_vencer`, por vencimiento; más un resumen y la lista
  `sin_lote`), `asignar_vencimiento_a_saldo` y `dar_de_baja_lote`. **`sin_lote` se clasifica por depósito y variante,
  nunca por producto**: un −5 sin lote en un depósito y un +5 en otro no se cancelan (el negativo son salidas que no
  bajaron ningún lote de ese depósito, y sus lotes están sobreestimados); cada saldo ≠ 0 sale como una fila
  (`deposito_id`, `variante_id`, `saldo`, `situacion`) y el resumen los cuenta (`productos_sin_lote`,
  `productos_con_salidas_sin_lote`, `saldos_sin_fecha`, `saldos_con_salidas_sin_lote`).
- **Asignar un vencimiento** al saldo sin lote es un **par aditivo** de ajustes: una fila negativa sin lote y una positiva
  con lote y vencimiento, con la misma referencia (la `nota` más un identificador `[asignación xxxxxxxx]` que las une),
  la misma fecha, depósito y usuario; **nunca un `UPDATE`** de una fila ya escrita. Valida que el producto esté marcado y
  que el saldo sin lote de ese depósito y variante alcance, todo antes de escribir la primera fila. No usa `source_id`
  (en este motor es el id de la venta o de la recepción de origen; reusarlo para otra cosa lo volvería ambiguo).
- **Dar de baja un lote** es una `merma` (`movement_type='waste'`) sobre el bucket exacto (lote y vencimiento tal como los
  devuelve `lotes_de`), sin dejar su saldo negativo. 🔵 **No hay un `reason_code` propio `vencimiento`**: `add_movimiento_stock`
  guarda el tipo en `reason_code` (`merma`) y `vencimiento` no es un tipo de `TIPOS`; agregarlo cambiaría la lista que
  muestran las pantallas. El motivo va en la referencia (`Merma: Vencimiento — lote L1, vence ...`), que es la convención
  que ya usa la merma de Restolibra (`Merma: <motivo>`).
- **Idempotencia: `clave_operacion` obligatoria y única por producto** en `asignar_vencimiento_a_saldo`,
  `dar_de_baja_lote` y en los cuerpos de `POST /asignar` y `POST /merma` (un texto no vacío de hasta 64 caracteres, sin
  corchetes). Sin ella un reintento tras un commit con la respuesta perdida duplicaría el par o descontaría dos veces. El
  ledger no tiene columna: la clave viaja **al final de la nota** de cada movimiento como `[op:<clave>]` (el mismo recurso
  que `[asignación xxxxxxxx]`), en la **misma transacción**. 🔑 **La unicidad es `(item_id, clave)`, no global:** el
  bloqueo es por producto, así que sólo serializa a quienes compiten por el mismo producto; se busca la marca entre los
  movimientos de ese `item_id` con el producto ya tomado (una búsqueda global dejaba a dos productos con la misma clave
  tomando bloqueos distintos, sin verse, y un reintento posterior encontraba dos operaciones). La misma clave sobre otro
  producto es simplemente otra operación (no 409 ni `repetida`). Un reintento con la misma clave, el mismo producto y los
  mismos parámetros **no escribe nada** y devuelve el resultado de la primera vez (con el saldo de entonces) más
  `repetida: true` (200); con otros parámetros u otra operación sobre ese producto, 409 (`ClaveDeOperacionReusada`). Una
  operación que falla no gasta la clave. Sólo cuenta la marca al final de la nota (un texto libre no la puede imitar) y se
  compara exacta, mayúsculas incluidas (`LIKE` sólo preselecciona). **Contrato para el cliente: una clave por intento del
  usuario y por producto** (un UUID por intento; reenviar la misma sólo al reintentar). Límite: no hay expiración de claves.
- **La variante tiene que ser del producto.** `variante_id` llega del cliente: en `asignar`, `dar_de_baja` y `lotes_de` (que
  ahora acepta `variante_id` como filtro, y `GET /productos/{id}/lotes` también) se exige que exista y sea de `item_id`
  (`item_variants.item_id`), **antes** de leer saldos o de escribir; si no, `ValueError` (422 en el router; producto
  inexistente, 404). Sin esto se podía mover saldo del producto A a una variante del B. No se exige que esté activa: el
  motor no lo exige para mover stock y la merma de una variante dada de baja es legítima.
- **Concurrencia de las escrituras.** `asignar` y `dar_de_baja` **toman el producto** antes de leer el saldo, con un
  `UPDATE catalog_items SET tracks_expiry = tracks_expiry WHERE id = ?`: en PostgreSQL bloquea la fila hasta el commit, así
  que dos escrituras simultáneas sobre el mismo producto se serializan y no gastan dos veces el mismo saldo, y **dos
  reintentos simultáneos con la misma clave y el mismo producto escriben una sola vez** (hay tests con dos conexiones en
  PostgreSQL, también con la misma clave en dos productos distintos: cada uno escribe una vez); en SQLite
  toda escritura ya se serializa.
- **«Hoy» es la fecha de Argentina** (UTC-3 fijo, como `erp.listas_precio`; no depende de la base de zonas horarias de la
  imagen), no la del servidor: un vencimiento no puede correrse de día porque el contenedor esté en UTC. La ventana es
  cerrada: entra lo que vence hasta `hoy + dias` inclusive; **vencido es `vence < hoy`** (lo que vence hoy es `por_vencer`
  con 0 días). ADR-017 (reposición) sigue con la fecha del servidor: no se tocó.
- **Los routers son dos**, para que el producto ponga capacidades distintas a leer y a escribir:
  `build_vencimientos_router` (`GET /api/vencimientos`, `/export` CSV y `/productos/{id}/lotes`) y
  `build_vencimientos_escritura_router` (`PUT /productos/{id}`, `POST /asignar`, `POST /merma`), este último con gates
  por operación (`dependencias_marcar`, `dependencias_movimientos`) además de los que el producto ponga al montarlo.
  🔴 **La factory de escritura FALLA al construirse (`ValueError`) si falta `usuario_actual` o si alguna de las dos
  listas de dependencias está vacía o ausente**: no hay sustituto (`{}`) ni forma de exponer escrituras del ledger sin
  autorización y sin usuario en `created_by`. Firma: `build_vencimientos_escritura_router(*, conexion=None,
  usuario_actual, dependencias_marcar, dependencias_movimientos, prefix="/api/vencimientos")`; las dos listas son de
  `Depends(...)` y no pueden ser vacías. Errores: parámetro o cuerpo inválido y sucursal o depósito inexistentes, 422; producto inexistente, 404; regla de negocio
  (saldo insuficiente, producto sin marcar, servicio, `clave_operacion` ya usada en ese producto con otros datos), 409; base sin la revisión, 503. Quién puede qué es del producto:
  la propuesta es que lea encargado y depósito, marque el encargado, y asignen y den de baja encargado y depósito.

**Defaults de producto decididos (2026-09-30).** Los usa A-4; A-1 sólo usa el último.
1. Un producto vencido **se vende con aviso, no se bloquea**.
2. El stock **«sin lote» sale último** en FEFO (primero vence, primero sale).
3. La devolución de un perecedero **va a merma**, no vuelve al lote.
4. Aviso de vencimiento con **15 días** de anticipación por defecto (parámetro `dias`, tope 365).

**Lo que A-1 NO hace, a propósito.** No cambia la venta, la anulación, la devolución, la transferencia ni `ajustar_stock`, ni
la entrada manual de `POST /api/stock/{pid}/ajuste`: todos siguen escribiendo movimientos **sin lote**. Es A-4 (FEFO), y
es el paso más delicado. 🔴 **Consecuencia que hay que tener presente:** en un producto marcado, hasta A-4, lo que sale
resta del bucket «sin lote» (que queda negativo) y **no** del lote del que salió la mercadería, así que el saldo de los
lotes está **sobreestimado**. El reporte no lo esconde: `sin_lote` lista, por depósito y variante, los saldos sin lote
negativos (`situacion='salidas_sin_lote'`, sin compensarlos con los positivos de otro depósito), y `lotes_de` muestra el negativo. Lo mismo `ajustar_stock`: lleva el stock **total** de un
producto al valor pedido y su fila va sin lote, así que en un producto con lotes su efecto por lote es ambiguo hasta A-4.
Mientras tanto, la forma segura de usarlo es marcar el producto, asignar vencimiento a lo que hay y dar de baja lo que vence.

**Riesgos.** (1) **Concurrencia en PostgreSQL al elegir lote en A-4**: la venta tendrá que elegir el lote dentro de su
transacción (`SELECT ... FOR UPDATE` o aceptar un lote negativo); el bloqueo del producto de A-1 no cubre ese camino.
(2) El camino viejo `usecases.sales` (venta por el dominio) queda **fuera de alcance** y seguirá escribiendo sin lote.
(3) `erp.ventas.crear_venta` no valida stock: un lote puede quedar negativo por una venta (A-4 decidirá). (4) El saldo
por depósito: la jerarquía sucursal/depósito ya cambió cómo se piensa el stock; las existencias por lote se miden por
depósito y `sucursal_id` sólo filtra por los depósitos de esa sucursal. (5) Un `expires_at` que no se puede leer, escrito
por otro camino, cuenta como sin vencimiento (un reporte no debe caerse por una fila). (6) `proximos_a_vencer` suma
«unidades» de productos con unidades distintas (kg y u): es un total de cantidades, no de pesos.

**Se difiere, a propósito.** Costo por lote (la recepción actualiza el `default_cost` del producto, último costo, no el de un lote); bloqueo configurable
de la venta de vencidos; alertas por correo o WhatsApp; trazabilidad hacia atrás (de qué lote salió cada venta); fraccionar un
lote; vencimiento por defecto por días de vida útil del producto; lote y vencimiento en la entrada manual de stock (A-2 en la
pantalla, con lo que decida el producto); y exponer la marca «vence» en el catálogo (hoy sale de
`GET /api/vencimientos/productos/{id}/lotes`, para no cambiar la respuesta de `GET /api/productos` en los tres productos).

**Consecuencias.** Un producto la monta con
`app.include_router(build_vencimientos_router(conexion=...), dependencies=...)` y
`build_vencimientos_escritura_router(conexion=..., usuario_actual=..., dependencias_marcar=[Depends(...)],
dependencias_movimientos=[Depends(...)])` (las tres últimas obligatorias), después de correr `libracommerce-migrar upgrade`. Un producto que no los monte, o que no marque ninguno, no ve
ninguna diferencia.

**Nota (2026-09-30, revisión de Codex sobre el montaje en VentaLibra) — parche hasta A-4: la merma de un lote mira también el
stock total y las salidas sin conciliar.** `dar_de_baja_lote` validaba sólo el saldo **del lote**. Como hasta A-4 las ventas
descuentan del bucket «sin lote» y no del lote, el caso asignar 10 a un lote → vender 10 (queda −10 sin lote y el lote conserva
10) → dar de baja el lote pasaba y dejaba el stock total en −10 (descontaba dos veces lo vendido). Ahora, con el producto
ya tomado y **después** de buscar la `clave_operacion` (un reintento de una baja ya hecha sigue devolviendo lo anterior con
`repetida: true` aunque el estado haya cambiado), se exige además: (a) que el **stock total** del producto en ese depósito y
variante (todos los buckets, lotes y sin lote) alcance (`SaldoInsuficiente`, 409), y (b) que **no haya saldo «sin lote»
negativo** en ese depósito y variante (`ReglaDeNegocio`, 409: «hay salidas sin lote sin conciliar en este depósito: el saldo
del lote puede estar sobreestimado; conciliá con el conteo físico antes de dar de baja»). **Es un parche, no el modelo:** lo
resuelve A-4 (FEFO), que hará que la venta baje el lote del que sale la mercadería; ahí (b) deja de tener sentido y se
revisa. Costo aceptado: mientras haya salidas sin conciliar en un depósito/variante no se puede mermar ninguno de sus lotes
hasta conciliar con el conteo físico (un ajuste que lleve el bucket sin lote a cero). Una salida sin lote de **otro**
depósito o variante no bloquea.

**Nota (2026-09-30) — los CSV del motor no dejan pasar fórmulas.** Los exports de margen, reposición y vencimientos salen todos
por `web.margen_router._csv`; ahora cada celda de **texto** que empieza con `=`, `+`, `-`, `@`, tab o retorno de carro se
prefija con `'` (`web/csv_seguro.celda_segura`), porque un nombre de producto, un código, un lote, un depósito o una nota
cargados por el personal podían ser una fórmula al abrir la planilla. Los números (también los negativos) y `None` no se
tocan. Un test barre el paquete y falla si aparece otro export con su propio `csv.writer`.

> **Nota 2026-09-30 (tercera revisión de Codex sobre el montaje en VentaLibra): la guarda de la merma es de mejor esfuerzo, no una garantía.** Mientras las ventas descuenten del bucket «sin lote», el saldo de un lote sobreestima lo que hay en cuanto ocurre CUALQUIER salida sin lote posterior a su entrada o asignación, aunque el saldo neto «sin lote» siga siendo positivo (asignar 6 de 10, vender 3 sin indicar lote y mermar las 6 vuelve a descontar unidades vendidas). `dar_de_baja_lote` bloquea los casos detectables (stock total insuficiente y saldo «sin lote» negativo), pero **no puede probar de qué bucket salió físicamente una venta**. Cerrarlo de verdad exige que la salida registre su lote (A-4: FEFO en ventas, anulaciones, devoluciones y transferencias). **Decisión:** hasta A-4, el producto que monte este router **no debe exponer la baja de un lote**; la asignación de vencimiento (que conserva el total) y el reporte informativo sí. La regla de salidas sin conciliar se revisa cuando A-4 esté hecho.
> **Nota 2026-09-30 (CSV):** `csv_seguro` también neutraliza el salto de línea inicial (`\n`), que OWASP cuenta como posible disparador de fórmula.

**Nota (2026-09-30) — carga de vencimientos: la entrada con lote y la marca en el producto.** Decisión del humano: hoy no hay
dónde cargar vencimientos de forma natural (se podía marcar, asignar al saldo sin lote y dar de baja, pero no **cargar** un
lote de mercadería nueva ni marcar desde el alta del producto). Es el **motor** de la etapa; el kit de pantallas y el
producto vienen después y se apoyan en estos dos contratos. Aditivo, sin tocar el camino de ventas.
- **`erp.vencimientos.registrar_entrada_con_lote(conn, item_id, deposito_id, lote, vence, cantidad, *, clave_operacion,
  variante_id=None, usuario_id=None, nota='', fecha='')`** y **`POST /api/vencimientos/entrada`** (router de escritura, con
  `dependencias_movimientos` y `usuario_actual` como asignar y merma; la firma obligatoria de la factory no cambia): una
  **entrada manual de stock nuevo** con lote y vencimiento. Se distingue de asignar: **asignar** le pone fecha a saldo que
  ya está contado sin lote (un par de ajustes, el total no cambia); la **entrada** suma stock real (`+cantidad`, **una sola
  fila**) y no toca el «sin lote». Devuelve `{producto_id, deposito_id, variante_id, lote, vence, cantidad, referencia,
  saldo_lote, repetida}` (`saldo_lote` = saldo de ese lote tras la entrada).
- **Tipo de movimiento: `entrada`** (`reason_code='entrada'`, `movement_type='adjustment'`), el mismo que escribe la entrada
  manual de `POST /api/stock/{id}/ajuste` y con etiqueta propia en las pantallas; `TIPOS` de `erp.stock` no se modificó. (Asignar
  sigue escribiendo `ajuste`: es una reclasificación, no una entrada.)
- **Un lote es el par (código, vencimiento), como ya lo agrupa `lotes_de`:** el mismo código con la **misma fecha suma al
  mismo bucket**; el mismo código con **otra fecha es otro bucket** (otra fila en `lotes_de` y en el reporte). No se
  «corrige» solo: un lote cargado con la fecha equivocada se da de baja y se vuelve a cargar. Unir buckets por código sería
  reescribir el ledger o adivinar cuál fecha vale; el costo aceptado es que un error de tipeo en la fecha se ve como dos filas.
- **Misma idempotencia que asignar y merma:** `clave_operacion` obligatoria, única por `(producto, clave)`, marca `[op:<clave>]`
  al final de la nota, producto tomado antes de buscarla. Reintento con los mismos datos → `repetida: true` con el resultado
  de la primera vez; otros datos u otra operación sobre ese producto → 409; **la búsqueda de la clave va primero**, así que un
  reintento de una carga hecha no falla porque el producto se haya desmarcado o la unidad haya cambiado. Un lote, un producto
  sin marcar y un servicio: 409. Validaciones: variante del producto (422), cantidad > 0, **la escala de la unidad** (entera
  si `units.allows_fraction = 0`; si la admite, hasta `decimal_scale` decimales, 3 si no lo declara, como `erp.reposicion`) y
  un tope de mil millones por entrada (sin tope `1e999999` pasaba por «número finito» y llegaba a un `float` infinito).
  🔵 Hallazgo: **ni `ajustar_stock` ni `asignar_vencimiento_a_saldo` validan la escala de la unidad** (el ajuste manual
  tampoco); acá sí porque es stock nuevo. No se tocaron.
- **La marca `vence` en el producto, opt-in por producto del motor:** `OpcionesCatalogo.con_vencimientos: bool = False` y
  `autorizar_marcar_vence: Callable[[dict], bool] | None = None` en `web/catalogo_router.py`. **Apagada (el default), las
  respuestas y los cuerpos de productos son byte a byte los de hoy** (Contalibra y Restolibra no cambian: un test compara el
  JSON contra el de una app sin la opción, y que la sesión ni se resuelve). **Prendida:** el listado, el escaneo (dentro de
  `producto`) y la respuesta del alta y la edición devuelven `vence: bool`; el alta y la edición aceptan `vence` (estricto:
  un `"si"` o un `1` son 422), y **si no viene no se toca la marca** (`save_catalog_item` no escribe la columna: editar otros
  campos nunca la pierde, hay test). Si viene y **cambió**, se marca o desmarca con `marcar_vence`; el gancho recibe el usuario
  de la sesión y, si devuelve `False`, **403** sin guardar nada del resto de la edición; sin gancho, cualquiera que pueda editar
  el producto puede marcar. Sin la revisión `0002` y con un cambio de marca: **409** con el comando; un servicio: 409.
- **Por qué opt-in y no siempre prendida:** los tres productos comparten `build_productos_router`, y una clave nueva en cada
  producto del listado, o un campo nuevo en el cuerpo, es un cambio de contrato para dos productos que no usan vencimientos
  (y `vence` en una base sin `0002` sería una mentira). Por qué **un gancho** `autorizar_marcar_vence` y no una dependencia:
  editar el producto y marcarlo que venza son capacidades distintas (en VentaLibra marca sólo el encargado y edita más gente)
  y la decisión depende de **si la marca cambia**, que el router sabe y el producto no.
- **Una sola consulta auxiliar por pedido en el listado** (`erp.vencimientos.ids_que_vencen`: los ids marcados, más el
  sondeo de la columna por metadatos, que también hace el resto del módulo), no una por producto. **Tolera una base sin la
  `0002`** (`vence` es `false`, nada se rompe): el sondeo es por `PRAGMA table_info` y no con un `SELECT` de la columna, porque
  en PostgreSQL un `SELECT` fallido **aborta la transacción** entera.
- 🔵 **Transacción de la edición:** `save_catalog_item` commitea por su cuenta (es del repositorio), así que no hay una
  única transacción SQL con el guardado. En cambio **todo lo que puede rechazar el cambio de marca se resuelve antes de
  escribir** (autorización, revisión, servicio) y la marca se escribe **después** del guardado, en la misma conexión y sin
  commit propio: un guardado que falla (p. ej. un código repetido, 422) no deja la marca cambiada (hay test). Lo único que
  queda fuera es una falla de la base entre el guardado y la marca.
- **Revisión de Codex (2026-09-30), tres ajustes.** (1) **Un servicio no puede quedar marcado.** El alta y la edición validan la
  combinación **resultante** (tipo pedido + marca efectiva), cambie o no la marca: un producto marcado que se edita a
  `servicio` con `vence` omitido o `true` es **409** («un servicio no puede tener vencimiento…») sin guardar nada y sin consultar
  el gancho; con `vence: false` en la misma edición vale. Para eso `marcar_vence(False)` ya no rechaza a un servicio (limpiar la
  marca siempre puede hacerse; sólo marcar es error). Además `registrar_entrada_con_lote` y `asignar_vencimiento_a_saldo`
  rechazan (409) un producto que no sea `item_type='product'`, marcado o no. La merma no: dar de baja existencias históricas de
  algo que hoy es servicio es una limpieza legítima. (2) **La entrada exige depósito activo** (`catalogo.validar_deposito`,
  `DepositoInexistente`, un `ValueError`: **422**, el mismo código que ventas y transferencias), porque es stock nuevo; va
  **después** de buscar la clave (un reintento de una carga hecha no falla porque el depósito se haya dado de baja). Lectura,
  asignar y merma sobre un depósito inactivo **no cambian** (sus existencias siguen existiendo). (3) **Limitación preexistente,
  fuera de alcance y pendiente:** la edición de un producto **no es atómica respecto de un código duplicado**:
  `update_producto` (`save_catalog_item`) commitea los campos y **después** reemplaza el código, así que un código repetido falla
  (422) con los demás campos ya guardados, **con o sin `vence`**. No lo introdujo esta etapa y arreglarlo toca `db/repository.py` y
  el camino de edición de los tres productos. Lo que sí se garantiza: **la marca `vence` sólo se escribe si el guardado completo tuvo
  éxito**. Un test (`test_limitacion_preexistente_la_edicion_no_es_atomica_respecto_del_codigo_duplicado`) fija el comportamiento
  actual.
- **Límites.** No hay `GET /api/productos/{pid}` en el motor: la ficha suelta con `vence` es
  `GET /api/vencimientos/productos/{id}/lotes` (`producto.vence`); `GET /api/stock/{pid}` y la respuesta del ajuste siguen con el
  `producto` sin `vence` (prender eso sería una opción más en `OpcionesStock`). La entrada con lote no avisa si el mismo
  código ya existe con otra fecha.
> **Nota 2026-09-30 (límite conocido, severidad media):** la validación «un servicio no puede tener vencimiento» en la edición de un producto lee la marca antes de guardar y no bloquea la fila; una edición a `servicio` concurrente con otra petición que marca el mismo producto puede dejar un servicio marcado. No daña datos: `registrar_entrada_con_lote` y `asignar_vencimiento_a_saldo` rechazan todo lo que no sea `item_type='product'` (409), así que un servicio marcado no recibe stock con lote, y se puede desmarcar con `vence: false`. Cerrarlo de verdad exige bloquear la fila durante el guardado de la edición (camino compartido con los tres productos, fuera de alcance) o una restricción en la base.

**Nota (2026-09-30) — A-4 PR-2: FEFO en la venta y lote en la anulación.** La venta y la anulación de un producto marcado
dejan de pasar por el bucket «sin lote». Lo que se decidió y se hizo:

- **Reglas (`erp/lotes.py`, nuevo).** Un producto, o un insumo de receta, con `catalog_items.tracks_expiry = 1` vende por FEFO,
  **una fila `sale` por lote consumido**, con el `lot_code` y el `expires_at` del bucket: (1) los lotes con fecha por
  vencimiento ascendente (los **vencidos incluidos**, salen primero y se venden con aviso), desempate por código; (2) los
  lotes con código y **sin fecha**; (3) el bucket «sin lote» **último**. Sólo cuentan los buckets con saldo > 0 **del
  depósito y de la variante de la línea** (una variante no consume los lotes de otra ni de otro depósito). Cantidades con
  `Decimal`: los lotes se reparten con la cantidad limpiada a 10 decimales (el ruido de `float` de `0.1 + 0.2` no deja restos
  ni filas de faltante), pero **una cantidad positiva nunca se pierde**: la suma de los tramos es siempre la cantidad exacta
  (una diferencia real, menor a 1e-10, se suma al último tramo; el ruido de `float`, menor a 1e-12 relativo, no) y, si la limpieza dejaría el plan vacío (`qty=4e-11`), el único tramo
  es el «sin lote» con la cantidad original, como en un producto sin marcar (revisión de Codex).
- **Faltante.** Si los lotes no alcanzan, **todo el resto va a UNA fila del bucket «sin lote»**, que queda negativo como hoy:
  ni se bloquea la venta ni se inventa un lote. Esa fila también lleva lo que el «sin lote» sí tenía, así que **un marcado
  sin lotes escribe la misma fila que un producto sin marcar** (`Tramo.faltante` dice cuánto no tenía respaldo).
- **Bloqueo.** Para los marcados, `lotes.tomar_productos` hace el mismo `UPDATE catalog_items SET tracks_expiry =
  tracks_expiry WHERE id = ?` de `asignar_vencimiento_a_saldo` y `dar_de_baja_lote`, **en orden ascendente de id y antes de
  leer los saldos** (que se releen después): dos ventas del mismo producto se serializan y también contra asignar, dar de
  baja y cargar, y dos ventas con varios productos en orden cruzado no hacen deadlock. Cierra el riesgo (1) de la sección
  «Riesgos» de esta decisión. Un producto sin marcar no se bloquea. 🔵 En el camino real (`crear_venta_directa`) el
  `INSERT` de `sales.number` ya serializa las ventas simultáneas antes de llegar al stock; el bloqueo es el que vale para
  el resto (merma, asignar, otros numeradores) y por eso los tests de concurrencia llaman a `descontar_stock_venta` directo.
- **Sonda de opt-in.** `descontar_stock_venta` hace **una** consulta (`SELECT id FROM catalog_items WHERE id IN (...) AND
  tracks_expiry = 1`) por venta, precedida de un `PRAGMA table_info(catalog_items)` (metadatos: en PostgreSQL un `SELECT`
  fallido aborta la transacción, así que una base **sin la revisión `0002`** se detecta por el catálogo y no revienta la
  venta). Sin marcados el resto es el código de siempre. **No se cachea el «sí»**: sin un identificador de base confiable
  (`sqlite3.Connection` no admite atributos ni `weakref`) un caché de proceso quedaría mal parado cuando una misma URL
  vuelve a tener un schema sin la columna (los tests lo hacen; un producto que restaura un respaldo, también). Costo medido
  en `descontar_stock_venta` de un producto sin marcar: SQLite ≈ +0,03 ms, PostgreSQL ≈ +0,7 ms por venta. Para poder
  sondear antes de escribir, `descontar_stock_venta` resuelve primero servicio y receta de todas las líneas (una sola vez
  cada una, como antes) y después escribe: la única diferencia observable es que un `resolver_receta` corre antes de la
  primera fila y no entre filas.
- **Anulación (`erp.ventas.anular_venta`).** `_movimientos_de_venta` suma `lot_code` y `expires_at` y un `ORDER BY id`
  (antes el orden dependía del físico); cada reposición copia el lote de su fila de venta. Como la venta escribe una fila por
  lote, la anulación **vuelve exacta al lote de origen**, receta y faltante incluidos. **No consulta la marca** (manda lo que
  dice el ledger) y no bloquea (suma stock). Una fila sin lote repone con el `INSERT` de siempre. Repone **aunque el lote se
  haya dado de baja (merma) después**: el lote reaparece con esa cantidad. Anular dos veces no escribe la segunda.
- **Avisos (sólo funciones; HTTP es del PR-3).** `lotes.avisos_de_venta(conn, venta_id, *, hoy=None, dias=15)` lee las filas
  `sale` de la venta y devuelve `{tipo, producto_id, nombre, lote, vence, dias_para_vencer, cantidad, deposito_id,
  variante_id}` con `tipo` = `lote_vencido` (`vence < hoy`, fecha de Argentina), `por_vencer` (hasta `hoy + dias`
  inclusive) o `faltante_sin_lote` (la venta dejó el «sin lote» de un marcado en negativo, medido justo después de esa venta).
  `lotes.planificar_salida(conn, items, deposito_id=None, *, hoy=None, dias=15, hooks=...)` es **lectura pura** (no escribe ni
  bloquea): dice qué lote saldría para cada línea, con las líneas en secuencia, para que el POS confirme antes de cobrar.
- **Movido, sin cambiar de nombre.** `erp.lotes` no importa `erp.stock` (lo importa `stock`, para no crear un ciclo con
  `vencimientos`): `normalizar_lote`, `normalizar_vencimiento`, `MAX_LARGO_LOTE` y la consulta de saldos por bucket viven ahí y
  se reexportan desde `erp.stock` y `erp.vencimientos`.

**Lo que NO cambia.** Un producto sin marcar y un marcado sin lotes escriben exactamente el ledger de siempre (mismas llamadas
a `add_movimiento_stock`, mismos argumentos: lo fijan los 188 tests de `tests/test_ledger_sin_marcar.py`, invertidos sólo los
`test_a4_cambia_*` de venta y anulación, hoy `test_a4_pr2_*`); Contalibra y Restolibra no cambian mientras no marquen
productos (platos e insumos sin marcar incluidos); tampoco margen ni reposición (leen las líneas de la venta y `sale`/`return` por `source_id`; hay un test que
compara un marcado con lotes contra uno sin marcar). **La nota «hasta A-4» de más arriba, en lo que toca a la venta y a la
anulación, ya no aplica** (se conserva como historia): una venta marcada baja el lote del que sale. **Sigue en pie para la
devolución, la transferencia y el ajuste**, que siguen escribiendo sin lote hasta el PR-3, y para las ventas hechas antes de
este cambio; por eso la guarda de `dar_de_baja_lote` (saldo «sin lote» negativo, stock total) y su nota se mantienen y se
revisan en el PR-3.

**PR-2 y PR-3 se promueven JUNTOS** (a `main`/tag): el PR-2 solo deja un estado intermedio que existe únicamente en
`develop`. En ese estado, **una devolución parcial de un marcado con venta FEFO repone en «sin lote»** (no al lote de origen:
`devolver_items` no cambió), así que el saldo de esos lotes queda subestimado y el «sin lote» sobrestimado hasta que el PR-3 la
cambie (par `devolucion` + `merma` con lote de origen).

**Queda para el PR-3:** la devolución de un perecedero en par `devolucion` + `merma` (decisión 3), la transferencia por lote,
el ajuste con lote (`ajustar_stock`), la exposición HTTP de los avisos (`avisos_de_venta` y `planificar_salida`), reactivar la
baja de un lote en el producto y revisar la guarda de la merma.
> **Nota 2026-09-30 (PR-2, segunda revisión de Codex; límites conocidos, no se endurecen):** (1) las cantidades con más de ~15 dígitos significativos (p. ej. lotes de 10^16 unidades) pueden perder una unidad al convertir cada tramo a `float` para el `INSERT` (la columna y la API trabajan con `float`); no es un caso de comercio real y fijarlo exigiría pasar `Decimal` hasta la base en todo el ledger. (2) La marca `tracks_expiry` se lee antes de tomar el bloqueo por producto: si se marca o desmarca un producto mientras se vende ese mismo producto, esa venta puede escribirse sin lote (estado de A-1) o con lote en un producto recién desmarcado (los consumidores del ledger ignoran el lote de un producto sin marcar). No pierde ni duplica stock. Cerrarlo de verdad exige bloquear todos los productos de la venta, con costo para el camino de Contalibra y Restolibra.

**Nota (2026-09-30) — A-4 PR-3: devolución, transferencia y ajuste por lote, y los avisos por HTTP.** Cierra A-4 en el
motor: con esto, **venta, anulación, devolución, transferencia y ajuste** de un producto marcado (`tracks_expiry = 1`)
siguen el lote. Estado intermedio que termina acá: el PR-2 solo existía en `develop`; **PR-2 y PR-3 se promueven JUNTOS**.

- **Devolución (`erp.ventas.devolver_items`): un PAR por lote, y la devolución de un perecedero va a merma.** Por cada tramo
  devuelto de un ítem **cuya venta salió de un lote** (lo dicen las filas `sale` de ESA venta, con `lot_code` o `expires_at`; **no la
  marca actual**: desmarcar el producto después de vender no cambia a dónde vuelve, igual que `anular_venta`) se escriben, **los dos en el `deposito_id` del parámetro** y con el lote y vencimiento de origen:
  un `devolucion +q` (`reason_code='devolucion'`, `source_type='venta'`, `source_id` la venta, con `lot_code` y `expires_at`) y
  una `merma −q` (`movement_type='waste'`, `reason_code='merma'`, `source_id` la venta, nota `Merma: devolución venta ID n —
  lote X`). **Por qué el par:** lo que lee el tope (`_COND_DEVUELTO`), `anular_venta` y `margen._devuelto_por_clave` son las filas
  `devolucion`, y la merma no matchea ninguna de las condiciones de vendido/devuelto ni las consultas de margen; para el stock y
  la reposición el neto es 0 (la mercadería perecedera devuelta no vuelve al estante: es la decisión de producto 3). El tramo
  es el reparto de lo devuelto entre las filas `sale` de la venta para ese ítem y variante **en el orden en que salieron**,
  restando lo ya devuelto por lote (`_acumular` aprendió `con_lote`; `_lotes_a_devolver` y `_repartir_devolucion`); el **tope no
  cambia**: sigue siendo por (ítem, variante) total, sin lote. Lo que ningún lote puede recibir (una venta de antes de A-4, una
  devolución anterior sin lote, el faltante de la venta) cae en «sin lote» **sin par**: una fila `devolucion` suelta, como
  siempre. Un producto sin marcar, y cualquiera cuya venta no salió de ningún lote (marcado o desmarcado), escriben la fila suelta de siempre (idéntica,
  `INSERT` de 11 columnas). Antes de leer lo ya devuelto se toma la unión de los marcados AHORA y los que la venta sacó de un lote
  (`lotes.tomar_productos`, en orden ascendente de id): dos devoluciones simultáneas del mismo producto se serializan y la segunda ve el tope que dejó la primera
  (hay tests con dos conexiones en PostgreSQL, y con líneas en orden cruzado). La atomicidad es la de siempre (no commitea; si
  falla a mitad el caller hace rollback).
- **Transferencia (`usecases.inventory.transfer_stock(tramos=...)` y `catalogo.transferir_stock`): un par por tramo FEFO.**
  `catalogo.transferir_stock` arma el plan sólo si el producto está marcado (`lotes.plan_fefo` sobre el depósito de **origen** y
  la variante; vencidos incluidos, «sin lote» último; el faltante no aplica porque la guarda de disponibilidad total ya exige
  stock) y lo pasa como `tramos`. Cada tramo escribe su propio par salida/entrada en la misma transacción; la salida lleva el lote
  y el vencimiento (`AAAA-MM-DD`) y la entrada los **copia**; cada entrada apunta a SU salida con `source_id`, así que el par sigue
  siendo 1:1. `tramos=None` (el default) es **el camino de siempre**: `lot_code` y `expires_at` en NULL por
  `repo.append_stock_movement`. Un marcado sin lotes en el origen da un plan todo «sin lote» y entonces también va con
  `tramos=None`. Se toma el producto antes de planificar y **no** se toma para una operación inválida (cantidad no positiva, mismo
  origen y destino: la rechaza `transfer_stock` como hoy). La guarda de disponibilidad no cambia (total del origen). **Efecto
  visible:** `get_transferencias` muestra **una fila por tramo**, cada una con su cantidad y sin clave nueva (mismo contrato
  por fila): una transferencia de 8 unidades de dos lotes son dos filas. Con `tramos`, `transfer_stock` devuelve el par del
  primer tramo. `StockMovement.expires_at` acepta `date` además de `datetime` (un `date` se guarda como `AAAA-MM-DD`, igual que
  las filas de venta; la recepción de compra sigue guardando `datetime`).
- **Ajuste (`erp.stock.ajustar_stock`, `POST /api/stock/{pid}/ajuste` con `modo="absoluto"`).** Recibe `lot_code` y `expires_at`
  opcionales. **Con lote: el conteo de ESE bucket** `(lote, vencimiento)` en el depósito que escribe: el delta es contra el saldo
  del bucket, no contra el total, y la fila `ajuste` lleva el lote; un lote que no existe se crea. **Sin lote, un marcado:**
  `stock_nuevo` sigue siendo el total; un delta **negativo** sale por FEFO (una fila `ajuste` por tramo, el mismo plan que la venta
  pero con el tipo de ajuste y sin `venta_id`), uno **positivo** entra «sin lote». Un producto sin marcar, o un marcado sin lotes,
  escribe la fila de siempre; pasar lote a un producto sin marcar es `ValueError`. Toma el producto antes de leer saldos.
  **HTTP, opt-in:** `OpcionesStock.con_lotes` (default `False`) hace que el cuerpo acepte `lot_code` y `expires_at` (sólo con
  `modo="absoluto"` o `"entrada"`; `salida`/`merma`, un producto sin marcar o un lote inválido, 422). Apagada, el cuerpo, el esquema OpenAPI y las
  respuestas son los de siempre (un test compara el esquema).
- **Avisos por HTTP, opt-in: `OpcionesVentas.con_avisos_de_vencimiento` (default `False`).** Prendida, `POST /api/ventas` y
  `GET /api/ventas/{id}` agregan la clave `avisos` (`erp.lotes.avisos_de_venta`, umbral por vencer de 15 días) **sólo si hay
  alguno**, y existe `POST /api/ventas/plan-salida`: cuerpo `{items: [{producto_id, qty, variante_id?}], deposito_id?}`, respuesta
  `lotes.planificar_salida(...)` (`{hoy, dias, salidas, avisos}`); **lectura pura** (no escribe, no bloquea, no commitea; 422 con
  una lista vacía, una cantidad no positiva o no finita, un producto o depósito inexistente), para que el POS confirme antes de
  cobrar. Apagada, las respuestas son byte a byte las de hoy y la ruta no existe ni figura en `/openapi.json` (un test lo
  compara). 🔵 Apagada, `POST /api/ventas/plan-salida` responde **405 y no 404**: es lo que ya pasaba hoy, porque `GET /{vid}`
  captura ese path. Sin gate de plan ni permisos propios: el producto monta el router con sus dependencias.
- **Efectos visibles.** (1) Una devolución de un marcado suma una fila `merma` por cada tramo en `GET /api/stock/movimientos`
  (con el `venta_id` de la venta). (2) Una transferencia de N lotes son N filas en `get_transferencias`. (3) El ajuste de un marcado
  con lotes escribe filas con lote. (4) La `salida` y la `merma` manuales de un marcado escriben una fila por lote en vez de una. (5) El stock de un perecedero devuelto no vuelve: la reposición sugerida ve `stock` más bajo
  que con un producto sin marcar equivalente, y la rotación (ventas netas) igual.

**Lo que NO cambia.** Un producto sin marcar y un marcado sin lotes escriben **el mismo ledger de siempre** en las cinco
operaciones (mismas llamadas a `add_movimiento_stock` y al repositorio, mismos argumentos): lo fijan los tests de
`tests/test_ledger_sin_marcar.py`, invertidos sólo los `test_a4_cambia_*` de devolución, transferencia y ajuste (hoy
`test_a4_pr3_*`). `test_erp_ventas.py` y `test_web_ventas.py` no se tocaron. Contalibra y Restolibra no cambian mientras no
marquen productos. Margen y reposición leen lo mismo (un test compara un marcado con devolución contra uno sin marcar).

**Límites conocidos (no se arreglan en este PR).**
- 🔴 **`devolver_items` no es receta-aware** (preexistente): una venta con receta descontó insumos (por lote si están marcados) y la
  devolución repone el plato, que nunca tuvo stock; los lotes de los insumos no vuelven ni van a merma. Un test lo fija. Es un
  pendiente de Restolibra, no de VentaLibra.
- 🔴 **`ajustar_stock` sin `deposito_id`** compara el total de TODOS los depósitos y escribe en el depósito por defecto
  (preexistente). Para un marcado con el stock repartido, el FEFO corre sobre ese depósito (donde se escribe), y puede dejar su
  «sin lote» en negativo aunque otro depósito tenga lotes. Quien ajusta un producto con varios depósitos tiene que pasar
  `deposito_id`; no se cambió porque alteraría el camino de los productos sin marcar. Un test lo fija.
- 🔵 `listar_ventas` y `erp.reportes._rango` filtran `sales.occurred_on <= hasta` sobre texto libre: una venta con hora del día `hasta`
  queda afuera (el margen lo evita con `_filtro_de_ventas`). Preexistente, no se tocó.
- 🔵 La edición de un producto no es atómica respecto de un código duplicado (ver la nota de la carga). Preexistente, no se tocó.
- **Variantes en el ajuste y la salida manual (revisión de Codex).** `ajustar_stock` compara el total del depósito, variantes incluidas,
  pero el FEFO sólo planifica la variante del movimiento: sin `variant_id`, la NULL. Un ajuste «a 5» de un producto con 10 en un lote de
  la variante A escribía −5 «sin lote» de la variante NULL y dejaba el lote de A en 10. Ahora, para un producto MARCADO, un
  ajuste sin lote (`ajustar_stock`, sube o baja) o una `salida`/`merma` manual (`salida_manual`) **sin variante** cuando el producto
  tiene saldo ≠ 0 en alguna variante **en ese depósito** (el dado o el por defecto) levanta `lotes.VarianteRequerida`
  (`ValueError`; 422 por HTTP: «este producto tiene stock por variante: indicá la variante a ajustar») antes de escribir. No se
  reparte entre variantes. Con `variant_id` explícito, sin stock en variantes (saldos en 0, o sólo en otro depósito), con lote (el bucket
  ya es de una variante) o sin marcar, todo es idéntico a hoy. La **transferencia** no tiene el patrón: su guarda mira
  `variant_id IS NULL` igual que el FEFO (lo que se compara es lo que se planifica). La **venta** tampoco se tocó: su línea
  sin variante consume la variante NULL por diseño (PR-2), sin comparar totales.
- 🔵 La **transferencia y el ajuste** siguen la marca ACTUAL (son operaciones sobre stock presente, no sobre una venta pasada): un producto
  desmarcado con lotes en el ledger transfiere y ajusta «sin lote» como uno sin marcar; al volver a marcarlo, por FEFO otra vez (hay test).
  La devolución y la anulación, en cambio, siguen lo que dice la venta.
- 🔵 Los avisos del `GET` de una venta usan «hoy» (Argentina), no la fecha de la venta: un lote que estaba vigente al vender
  puede figurar vencido al consultar meses después. Una venta anulada conserva sus avisos (son historia).
- 🔵 Con `tramos`, `transfer_stock` devuelve sólo el par del primer tramo; quien necesite todos los lee del ledger.
- 🔵 El plan de salida es una simulación sobre el estado de ahora: otra venta concurrente puede cambiar el resultado antes de cobrar.
  No valida `hooks.validar_deposito` (necesita turno): sólo que el depósito exista y esté activo.
- 🔵 Las cantidades con más de ~15 dígitos significativos pueden perder una unidad al pasar por `float` (nota del PR-2): vale también acá.

**Salidas manuales (se cerraron en este mismo PR, antes de subirlo).** `erp.stock.salida_manual(conn, pid, tipo, cantidad, ...)`
es la salida manual de un producto que puede estar marcado: un marcado sale por FEFO (`_salida_por_lote(tipo=...)`: una fila por
lote, vencidos primero, «sin lote» último, el faltante a una fila «sin lote», mismo `movement_type` y `reason_code` de cada modo
—`salida` = `adjustment`/`salida`, `merma` = `waste`/`merma`—, misma referencia, bloqueo por producto); un sin marcar y un marcado
sin lotes escriben la llamada de siempre a `add_movimiento_stock` (mismo `INSERT` de 11 columnas: un test compara el SQL, y la
comparación HTTP contra develop 69d4a77 de `salida`, `merma` y `entrada` dio respuestas y ledger idénticos). El endpoint de ajuste
la usa en `modo="salida"` y `modo="merma"`. La `entrada` manual sin lote de un marcado sigue entrando «sin lote» (sumar ahí no
sobreestima ningún lote); con `OpcionesStock.con_lotes` y `lot_code`/`expires_at`, el modo `entrada` escribe UNA fila `entrada`
en ese bucket (`stock.entrada_manual_con_lote`; **no** reusa `registrar_entrada_con_lote` porque esa exige `clave_operacion`, valida
la escala de la unidad y arma su propia referencia, y el modo `entrada` del endpoint no tiene idempotencia ni acepta esos
parámetros; la fila que escribe es la misma). Con lote en un producto sin marcar, o en `salida`/`merma`, 422.

**Caminos de stock del motor (`grep add_movimiento_stock | append_stock_movement`) y si siguen el lote:**

| Camino | Efecto | ¿Por lote? |
|---|---|---|
| `stock.descontar_stock_venta` (venta, insumos de receta) | resta | sí, FEFO (PR-2) |
| `ventas.anular_venta` | suma | sí, al lote de origen (PR-2) |
| `ventas.devolver_items` | suma + merma | sí, par por lote (PR-3) |
| `catalogo.transferir_stock` | resta y suma | sí, par por tramo FEFO (PR-3) |
| `stock.ajustar_stock` | resta o suma | sí: conteo de un lote, FEFO si baja (PR-3) |
| `stock.salida_manual` (endpoint `salida` y `merma`) | resta | sí, FEFO (PR-3, este cierre) |
| endpoint `entrada` sin lote | suma | entra «sin lote» (no sobreestima); con lote, a ese bucket |
| `vencimientos.asignar_vencimiento_a_saldo`, `dar_de_baja_lote`, `registrar_entrada_con_lote` | par / resta / suma | explícitas sobre un bucket, por diseño |
| `usecases.purchasing` (recepción) | suma | con lote (si la línea lo trae) |

**Quedan FUERA, por decisión (sólo se listan):** (a) el camino viejo `usecases.sales` (`confirm_sale`, `cancel_sale`,
`return_sale_items`: escriben `sale`/`return` sin lote, vía el repositorio); (b) `usecases.inventory.transfer_stock` llamado
**directamente** (sin pasar por `catalogo.transferir_stock`): con `tramos=None` mueve «sin lote»; (c) cualquier producto que llame
a `stock.add_movimiento_stock` con una cantidad negativa y sin lote (p. ej. un `produccion` o una merma propia de Restolibra):
ese helper no puede saber si el «sin lote» es intencional (lo necesitan `asignar` y el faltante de la venta), así que un producto
con marcados tiene que usar `salida_manual`, `descontar_stock_venta` o `ajustar_stock`; (d) los scripts de migración
(`scripts/migrate_from_*`), que cargan el ledger de otro sistema tal cual.

**A-4 está completo en el motor para los caminos de la tabla.** Venta, anulación, devolución, transferencia, ajuste y salidas manuales por lote. Consecuencia para el producto:
**la merma de un lote (`dar_de_baja_lote`) puede reactivarse en los productos que la deshabilitaron** (VentaLibra: quitar
`merma_deshabilitada`, `MERMA_DESHABILITADA=False` en sus tests, y `puedeMermar` separado de `puedeMover` en el kit), porque el saldo
por lote ya es fiable **para las salidas POSTERIORES** a este cambio y que pasen por esos caminos. Las ventas, devoluciones y transferencias **anteriores a A-4
siguen «sin lote»**: el saldo «sin lote» negativo heredado mantiene la guarda de `dar_de_baja_lote` (no se toca) hasta que se
concilie con el conteo físico. Si el producto usa alguno de los caminos (a)–(c), el saldo de sus lotes puede volver a sobreestimarse.
La guarda de stock total y de saldo «sin lote» negativo de la nota del 2026-09-30 sigue en pie.
> **Nota 2026-10-01 (hallazgo de Codex sobre el montaje en VentaLibra):** `_agregar_avisos` corre en `POST /api/ventas` DESPUÉS del commit de la venta y del descuento de stock; si el cálculo de los avisos fallaba, la respuesta era un error aunque la venta ya estaba registrada, y el POS reintentaba el cobro (venta y cobro duplicados) o, en el cobro por QR, perdía el id de la venta pendiente. Ahora **falla abierto**: se omite `avisos`, se registra el error y se devuelve la venta confirmada (v0.30.1). Un aviso es un complemento informativo y nunca puede ocultar una venta ya registrada.

## ADR-019 — Reposición v2: lo que está en un lote vencido no cuenta como stock (2026-10-01)

**Contexto.** ADR-017 dejó diferido «descontar lo vencido» hasta que existiera FEFO por lote (ADR-018, A-4). Con A-4
hecho, un producto perecedero puede tener la góndola llena de mercadería que ya no se puede ofrecer, y la v1 lo
daba por cubierto y no sugería pedir.

**Decisión.** Para un producto marcado (`catalog_items.tracks_expiry = 1`), la suma de los saldos positivos de los
lotes con `vence < hoy` en los depósitos que se miran se resta del stock disponible para la cuenta (`sugerido`,
`cobertura_dias`). El campo `stock` sigue siendo el real; `vencido` informa cuánto se descontó. Un lote que vence
hoy todavía es stock. El saldo «sin lote» no tiene fecha y no se descuenta. Se puede apagar con
`descontar_vencido=false` (router y función), y es el default `true`: sólo cambia a los productos marcados, así
que un producto sin marcar, o una base sin la revisión `0002`, devuelve lo mismo que en la v1. Sin migración.

**Consecuencias.** Un producto con todo el stock vencido figura con cobertura 0 y se sugiere pedir su necesidad
entera. «Hoy» sigue siendo la fecha del servidor (límite de ADR-017). Sigue diferido: `lead_time_days` y
`max_stock` por producto, proveedor por producto, orden de compra en borrador, estacionalidad, `min_stock` por
sucursal y descontar lo que vence *dentro* del horizonte de cobertura (hoy sólo lo ya vencido).

## ADR-020 — Reposición v2: plazo de entrega y techo de stock propios del producto (2026-10-02)

**Contexto.** ADR-017 dejó diferidos `lead_time_days` y `max_stock` por producto. El plazo general (3 días) sirve de default pero un
proveedor de importación no entrega en lo mismo que uno local, y un producto de poco movimiento o de mucho volumen no se debe pedir
más allá de lo que entra en el depósito.

**Decisión.** Revisión Alembic **`0003_parametros_reposicion`**: `catalog_items.lead_time_days INTEGER` y `catalog_items.max_stock NUMERIC`,
las dos **NULL** (sin valor por defecto: «no definido» no es 0). Aditiva e idempotente; los productos que existen quedan en NULL y la
reposición les da lo mismo que antes. Una base sin la revisión sigue funcionando (la consulta no lee las columnas) y las operaciones nuevas
responden `SinRevision`/503.

- **Plazo propio:** reemplaza al `plazo_entrega_dias` general en el horizonte de ESE producto (`dias_cobertura + plazo`). Entero de 1 a 180.
- **Techo:** `disponible + sugerido` no pasa de `max_stock` (disponible = stock utilizable + en camino; el techo cuenta lo ya pedido). La
  sugerencia se recorta redondeando **hacia abajo** a la unidad; la fila lleva `limitado_por_maximo`. **El techo manda sobre el piso del
  mínimo**; guardar un techo menor que el mínimo se rechaza (`fijar_parametros`), pero si ya existe gana el techo.
- **Salida:** cada fila trae `plazo_entrega_dias` (el usado), `plazo_propio`, `stock_maximo` y `limitado_por_maximo`; el CSV también.
- **Escritura:** `erp.reposicion.fijar_parametros` (los dos valores completos; `None` borra) y `build_reposicion_parametros_router`
  (`GET`/`PUT /api/productos/{id}/reposicion`), que **no se construye sin `dependencias_escribir`**. Va aparte del payload del producto a
  propósito: no cambia ni el contrato ni el router de productos que ya usan los tres productos.

**Consecuencias.** Pendiente fuera de este ADR: cargarlos desde la pantalla del producto (kit) y montar el router en VentaLibra con su
capacidad; proveedor por producto, orden de compra en borrador, estacionalidad, `min_stock` por sucursal y descontar lo que vence dentro
del horizonte. Un `max_stock` por sucursal tampoco existe: el techo es del producto en la instancia o la sucursal que se mira.

> **Nota 2026-10-02 (hallazgo de Codex sobre el montaje en VentaLibra, v0.32.1):** `update_producto` ahora rechaza (`ValueError`, el router lo contesta 422) subir el
> `stock_minimo` por encima del `max_stock` del producto. Antes sólo lo exigía `fijar_parametros` y una edición del producto (que puede hacer un rol que no ve los
> parámetros) dejaba la invariante rota. El techo manda igual si ya había un mínimo mayor (datos anteriores). Una base sin la revisión `0003` no cambia.

## ADR-021 — Reposición v2: un proveedor habitual por producto (2026-10-02)

**Contexto.** ADR-017 y ADR-020 dejaron diferido el «proveedor por producto». Hoy una orden de compra lleva un proveedor, pero el producto no sabe
a quién se le suele pedir: la reposición sugiere qué pedir y no a quién, y la orden de compra en borrador (el paso que sigue) no tiene de dónde sacarlo.

**Decisión.** Revisión Alembic **`0004_proveedor_por_producto`**: `catalog_items.supplier_party_id INTEGER REFERENCES parties(id)`, **NULL** (sin proveedor
definido). Aditiva e idempotente; los productos que existen quedan en NULL. **Un** proveedor habitual por producto (no una lista de proveedores con precios
ni plazos: eso es otro diseño). El proveedor es un tercero (`parties`) que exista y esté activo; el motor no tiene roles de tercero, así que no se exige
que figure como proveedor en otra parte.

- **Escritura:** `fijar_parametros(..., proveedor_id=)` y el `PUT /api/productos/{id}/reposicion` aceptan `proveedor_id`. **Si no se manda, el proveedor queda
  como estaba** (`SIN_CAMBIO` / clave ausente del cuerpo): los clientes de v0.32.x (el kit 0.100.0, que sólo manda plazo y techo) no lo borran sin querer. `null`
  lo borra. Un id inexistente o de un tercero dado de baja es 422/`ValueError` y **no escribe nada** (tampoco el plazo ni el techo del mismo pedido).
  Sin la revisión `0004` pedir un proveedor responde `SinRevision`/503; sin pedirlo, la `0003` alcanza como siempre.
- **Lectura:** `GET .../reposicion` devuelve `proveedor_id` y `proveedor` (el nombre). La reposición trae los dos en cada fila y en el CSV, y acepta
  `proveedor_id` para listar sólo los productos de ese proveedor (422 si no existe). Una base sin la `0004` devuelve `None` y no filtra nada.
- **No cambia** el cálculo del sugerido ni lo que cuenta como «en camino» (una orden a otro proveedor sigue contando: el habitual es una preferencia, no una
  restricción).

**Consecuencias.** Falta cargarlo desde la pantalla del producto y elegirlo/filtrarlo en la reposición ([[libra-ui]]), montarlo en VentaLibra y la orden de compra
en borrador por proveedor. Sigue diferido: estacionalidad, `min_stock` por sucursal y descontar lo que vence dentro del horizonte.

> **Nota 2026-10-02 (VentaLibra, v0.33.1):** el `proveedor_id` que hablan los routers de reposición no tiene por qué ser el `party_id`. VentaLibra guarda sus proveedores en
> otra tabla y traduce con un offset (`party_de_proveedor`/`proveedor_de_party`), igual que en Compras. `build_reposicion_router` y `build_reposicion_parametros_router`
> aceptan ahora los mismos ganchos que `OpcionesCompras` (`resolver_proveedor(conn, proveedor_id) -> party_id` y `proveedor_de(conn, party_id) -> proveedor_id`): el filtro, el cuerpo
> del `PUT` y cada respuesta hablan en los ids del producto, y el motor sigue guardando y comparando por `party_id`. Sin ganchos, identidad (el comportamiento de v0.33.0). Lo vio el
> primer test de VentaLibra contra el router real (422 «el proveedor 2 no existe»): la prueba del motor sólo usaba `party_id`.

## ADR-022 — Reposición v2: órdenes de compra en borrador, una por proveedor habitual (2026-10-02)

**Contexto.** La reposición sugiere qué pedir (ADR-017) y ahora a quién (el proveedor habitual del producto, ADR-021), pero el circuito terminaba ahí: había que
transcribir la lista a mano en Compras. El paso que sigue es convertir lo sugerido en órdenes de compra.

**Decisión.** `erp.reposicion_ordenes.generar_ordenes_borrador` y `POST /api/reportes/reposicion/ordenes` (`build_reposicion_ordenes_router`) crean **una orden en
`draft` por proveedor habitual**, con una línea por producto: la cantidad es el `sugerido` de la reposición con los mismos parámetros, el costo es el
`catalog_items.default_cost` vigente (la línea trae `costo_cero` si nunca se cargó) y sin IVA. **Nunca envía ni confirma**: es un borrador que una persona revisa; el
motor ni siquiera tiene una operación que pase una orden a `sent`. Un producto sin proveedor habitual no entra en ninguna orden y se informa en `sin_proveedor`; un
`producto_ids` pedido sin nada que pedir se informa en `omitidos`. Sin migración (usa la `0004`; `SinRevision`/503 sin ella).

- **Lo confirmado es un tope:** el cuerpo puede llevar `topes` (`{producto_id: cantidad}`, lo que la persona vio en la vista previa); la cantidad de cada línea es el
  **menor** entre el sugerido de ahora y su tope, así que si entre la vista previa y el pedido el stock bajó y el sugerido subió, la orden no se pasa de lo confirmado (si
  bajó, se pide menos). Un producto sin tope se pide por el sugerido. Hallazgo de Codex sobre el kit.
- **La clave identifica UN pedido:** el marcador en las `notes` lleva también una huella de lo pedido (parámetros, `producto_ids`, `topes`); la misma clave con otros datos es
  `ClaveReusada` (409 en el router) y no devuelve lo anterior, para que quien reusó la clave no crea que se pidió lo nuevo. La clave admite letras, números y `. _ : -` (un
  UUID entra) y se busca por igualdad exacta (un `LIKE` trataría `%` y `_` como comodines). El tope se redondea hacia abajo a la unidad del producto; si no alcanza
  una unidad, la línea se omite. Hallazgos de Codex.
- **No duplica:** las órdenes en borrador ya cuentan como «en camino» (ADR-017), así que generar dos veces seguidas no encuentra nada que pedir la segunda. Además
  `clave_operacion` (obligatoria, un UUID por intento) hace idempotente un reintento exacto: se estampa `[op:<clave>]` en las `notes` y una clave usada devuelve las
  mismas órdenes (`repetida: true`).
- **Todo o nada:** las órdenes se insertan **sin commitear** (`repositorio_de(conn).save_purchase_order` confirma cada vez, y con eso las ya creadas quedarían
  confirmadas si una de las siguientes falla); el router traduce los ids de proveedor y recién después confirma. La numeración sale del `numerador` que pase el producto
  (`OpcionesCompras.numerador`).
- **Escribe órdenes de compra, así que la factory no se construye** sin `dependencias_escribir` (la capacidad que ya protege Compras) ni sin `usuario_actual`.
- **Ids de proveedor:** los mismos ganchos que Compras (`resolver_proveedor`, `proveedor_de`).

**Consecuencias.** Falta el botón en la reposición ([[libra-ui]]) y el montaje en VentaLibra. Sigue diferido: estacionalidad, `min_stock` por sucursal y descontar lo que vence
dentro del horizonte. Una orden generada con el costo en 0 hay que completarla antes de enviarla.
