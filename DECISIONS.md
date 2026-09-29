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
