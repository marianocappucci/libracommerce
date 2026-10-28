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
- **Reemplazada por ADR-067 (2026-10-19):** el mapeo sigue en el motor, pero sobre la capa
  ERP (`erp.offline`); `integrations/libraedge` se retiró.

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
  ✅ **Desde v0.59.0 (ADR-055 y ADR-056)** sí hay `enviar_orden` / `cancelar_orden`, y `contar_borradores=false` deja de restarlos (con `true`, el default, queda como acá).
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
(3) Una orden abandonada en `draft` cuenta como pedida (✅ opcional desde ADR-056: `contar_borradores=false`). (4) Un comercio que no lleva stock en el ledger no tiene qué
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
> **Nota 2026-09-30 (límite conocido, severidad media):** la validación «un servicio no puede tener vencimiento» en la edición de un producto lee la marca antes de guardar y no bloquea la fila; una edición a `servicio` concurrente con otra petición que marca el mismo producto puede dejar un servicio marcado. No daña datos: `registrar_entrada_con_lote` y `asignar_vencimiento_a_saldo` rechazan todo lo que no sea `item_type='product'` (409), así que un servicio marcado no recibe stock con lote, y se puede desmarcar con `vence: false`. Cerrarlo de verdad exige bloquear la fila durante el guardado de la edición (camino compartido con los tres productos, fuera de alcance) o una restricción en la base. ✅ **Cerrado en ADR-057 (v0.59.0):** `update_producto` suelta la marca al pasar a servicio dentro de su transacción y `marcar_vence` lleva la condición en el `UPDATE`.

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
  respuestas son los de siempre (un test compara el esquema). **Prenderla exige la guarda `OpcionesStock.autorizar_lotes` (ADR-047).**
- **Avisos por HTTP, opt-in: `OpcionesVentas.con_avisos_de_vencimiento` (default `False`).** Prendida, `POST /api/ventas` y
  `GET /api/ventas/{id}` agregan la clave `avisos` (`erp.lotes.avisos_de_venta`, umbral por vencer de 15 días) **sólo si hay
  alguno**, y existe `POST /api/ventas/plan-salida`: cuerpo `{items: [{producto_id, qty, variante_id?}], deposito_id?}`, respuesta
  `lotes.planificar_salida(...)` (`{hoy, dias, salidas, avisos}`); **lectura pura** (no escribe, no bloquea, no commitea; 422 con
  una lista vacía, una cantidad no positiva o no finita, un producto o depósito inexistente), para que el POS confirme antes de
  cobrar. Apagada, las respuestas son byte a byte las de hoy y la ruta no existe ni figura en `/openapi.json` (un test lo
  compara). 🔵 Apagada, `POST /api/ventas/plan-salida` responde **405 y no 404**: es lo que ya pasaba hoy, porque `GET /{vid}`
  captura ese path. ✅ **Ahora es 404 (ADR-057, v0.59.0).** Sin gate de plan ni permisos propios: el producto monta el router con sus dependencias.
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
  (preexistente). ✅ **Cerrado en el router por ADR-057 (v0.59.0):** con `por_deposito` y más de un depósito activo, el ajuste absoluto sin depósito es 422. Para un marcado con el stock repartido, el FEFO corre sobre ese depósito (donde se escribe), y puede dejar su
  «sin lote» en negativo aunque otro depósito tenga lotes. Quien ajusta un producto con varios depósitos tiene que pasar
  `deposito_id`; no se cambió porque alteraría el camino de los productos sin marcar. Un test lo fija.
- 🔵 `listar_ventas` y `erp.reportes._rango` filtran `sales.occurred_on <= hasta` sobre texto libre: una venta con hora del día `hasta`
  queda afuera (el margen lo evita con `_filtro_de_ventas`). Preexistente, no se tocó. ✅ **Arreglado el 2026-10-09** (ADR-039).
- 🔵 La edición de un producto no es atómica respecto de un código duplicado (ver la nota de la carga). Preexistente, no se tocó. ✅ **Arreglado en ADR-028** (`update_producto` guarda el producto y su código en una transacción).
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
  puede figurar vencido al consultar meses después. Una venta anulada conserva sus avisos (son historia). ✅ **Arreglado en ADR-057 (v0.59.0):** el detalle mide contra el día de la venta (el `POST` que la crea sigue usando el de hoy).
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

> **Nota 2026-10-13 — estado de los límites de este ADR (v0.59.0).** Al revisar los límites que quedaron escritos acá, cada uno quedó en uno de tres estados. (Los relevamientos los citan con letras que este ADR no lleva; se identifican por su texto.)
> - ✅ **Resueltos:** la edición de un producto no atómica con un código duplicado (**ADR-028**); el filtro de fecha de `listar_ventas` y `_rango` (**ADR-039**); «Recibir mercadería» sin idempotencia, pedido al kit (**ADR-046**); el aviso ámbar fijo de Vencimientos que ya no era cierto (kit `libra-ui` **v0.117.2**); el ajuste con lote por API sin guarda (**ADR-047**, `autorizar_lotes`); el ajuste absoluto sin depósito, los avisos del `GET` con la fecha de hoy, el `plan-salida` apagado que daba 405 y la carrera servicio / marca `vence` (**ADR-057**); y, aunque no estaba escrito acá, que devolver un servicio escribía stock (**ADR-054**).
> - 🔵 **Aceptados sin código** (ADR-057): las cantidades de más de ~15 dígitos significativos (`float`); marcar o desmarcar `vence` durante una venta del mismo producto; los caminos viejos sin FEFO (`usecases.sales`, `transfer_stock` directo, `add_movimiento_stock` con cantidad negativa sin lote, los scripts de migración).
> - ⏳ **Siguen abiertos:** `devolver_items` no es receta-aware (pendiente de Restolibra); lo anterior a A-4 sigue «sin lote» hasta conciliarlo con el conteo físico (la guarda de `dar_de_baja_lote`); `transfer_stock` con `tramos` devuelve sólo el par del primer tramo; la transferencia y el ajuste siguen la marca ACTUAL; el plan de salida es una simulación sobre el estado de ahora.

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
- **Un reintento devuelve las mismas órdenes, tal como están AHORA** (no una foto congelada de la primera respuesta: si alguna se recibió o se editó, se ve), **e incluido lo que NO se pidió** (`sin_proveedor`, `omitidos`): viaja codificado en las `notes` de la primera orden. Una petición que
  no crea ninguna orden no deja registro (no escribió nada): repetirla vuelve a calcular.
- **Concurrencia:** toda la generación va detrás de **un candado global de la transacción** (`pg_advisory_xact_lock` en PostgreSQL; en SQLite, que sólo se usa en pruebas, un `UPDATE` sin filas
  que toma el candado de escritura), tomado antes de mirar la clave y de calcular. La clave vive en las `notes` y no hay restricción única que impida dos pedidos a la vez; un candado por producto
  no alcanzaba (conjuntos disjuntos con la misma clave, y tomarlos en tandas cruza el orden y abre un deadlock). Es una operación rara y corta. Medido contra PostgreSQL real con hilos: sin el
  candado se crean el doble y una clave queda con dos huellas; con él, una por proveedor y el segundo pedido de la misma clave con otros datos es un conflicto.
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

## ADR-023 — Reposición v2: estacionalidad por lo que pasó hace un año (2026-10-02)

**Contexto.** La rotación de los últimos `N` días supone que lo que viene se parece a lo reciente. En un producto estacional no es así (el helado en octubre, el pan dulce en diciembre) y la
reposición pide de menos justo antes de la temporada y de más justo después. Diferido desde ADR-017.

**Decisión.** Parámetro **opt-in** `estacionalidad` (apagado por default: sin él, el resultado es byte a byte el de antes) en `sugerencia_reposicion`, `GET /api/reportes/reposicion` (y su export) y
en el cuerpo de la generación de órdenes en borrador. Con él, para cada producto se comparan dos ventanas **de hace un año** (la misma fecha, un año atrás; el 29 de febrero cae en el 28):
la **de referencia** (los `N` días que terminaban entonces, equivalente a la ventana de ahora) y la **proyectada** (los `H` días que venían después, con el horizonte propio de cada producto,
incluido su plazo de entrega propio, cortada en hoy si pasa de un año). El `factor_estacional` es la razón de las rotaciones diarias, proyectada / de referencia, **acotado entre 0,25 y 4**, y
multiplica la proyección: `necesidad = unidades × H / días_de_muestra × factor`. No toca el stock, lo vencido, lo que viene en camino, el mínimo ni el techo.

- **Sin factor (`None`, sin ajuste)** si la ventana de referencia del año pasado tiene **menos de 3 días con venta**: una instancia con menos de un año de historia o un producto que entonces no se
  vendía no inventa una temporada. Sin migración ni configuración: usa las ventas de siempre (`erp.margen.unidades_netas`, con el filtro de sucursal).
- **Límites, dichos en voz alta:** confía en **un solo año** (un año atípico se hereda), no corrige los quiebres de entonces (una temporada con el producto agotado parece más floja de lo que
  fue), no inventa temporada para un producto sin rotación reciente (necesidad cero sigue en cero) y el factor es por producto, no por categoría.
- Cada fila trae `factor_estacional` (también en el CSV); la respuesta devuelve `estacionalidad`.

**Consecuencias.** Falta el interruptor y la columna en la reposición (libra-ui) y activarlo en VentaLibra. Sigue diferido: `min_stock` por sucursal y descontar lo que vence dentro del horizonte.

## ADR-024 — Reposición v2: stock mínimo por sucursal (2026-10-03, v0.36.0)

**Contexto.** `catalog_items.min_stock` es global: la reposición por sucursal lo usaba como piso de todas por igual, aunque una sucursal chica y una grande no necesitan el mismo colchón de
un mismo producto. Diferido desde ADR-017 y ADR-023.

**Decisión.** Tabla nueva **`item_branch_min_stock(item_id, branch_id, min_stock NUMERIC NOT NULL CHECK (min_stock >= 0), PRIMARY KEY (item_id, branch_id))`**, con FK a `catalog_items` y a
`branches`, creada por la revisión **`0005_min_stock_por_sucursal`** (aditiva, vacía, idempotente por introspección, el mismo SQL en SQLite y PostgreSQL; el downgrade baja la tabla y cada sucursal
vuelve al global). Como las revisiones 0002 a 0004, **no está en `init_schema()`**: esa función y su fixture congelada (`test_schema_congelado`) no se tocan.

- **Resolución del piso.** En `sugerencia_reposicion` con `sucursal_id`, el piso del producto es el de esa sucursal si tiene fila y, si no, el global. **Sin `sucursal_id` (toda la instancia) usa
  siempre el global**, como hasta ahora: no se suman ni se promedian los mínimos de las sucursales (hablan del stock de cada una, no del total) y el global sigue siendo «el mínimo del producto».
  `0` sigue siendo «no me avises», también como mínimo propio (una sucursal puede apagar el aviso de algo que el global vigila; borrar el propio —`None`— y poner `0` son cosas distintas).
- **Lo que devuelve.** Cada fila trae `stock_minimo` **ya resuelto** (mismo nombre y tipo que antes) y `stock_minimo_propio` (`bool`: viene de la sucursal). El CSV agrega la columna
  `stock_minimo_propio` **al final**, para no correr las columnas de quien lo lee por posición. Sin la revisión `0005` la consulta es la de siempre y `stock_minimo_propio` es `False`. La generación
  de órdenes en borrador usa `sugerencia_reposicion` y hereda el cambio.
- **API del motor.** `minimos_por_sucursal_de(conn, item_id)` lista **todas las sucursales activas** con `stock_minimo` (el efectivo), `stock_minimo_propio` y `stock_minimo_global` (la referencia).
  `fijar_minimo_sucursal(conn, item_id, sucursal_id, stock_minimo)`: `None` borra el propio. Valida: el producto existe (`ProductoNoEncontrado`), la sucursal existe (`ValueError`), un número finito
  de 0 a `MAX_STOCK_MINIMO` (mil millones: un seguro contra `1e400`, no una regla de negocio) y, si el producto tiene techo (`max_stock`), no mayor que él (el invariante de ADR-020). Como los demás
  `fijar_*`, no distingue servicios de productos (un servicio no entra en la reposición, así que su mínimo no se usa). Una sucursal dada de baja no admite fijar un valor, pero sí borrarlo.
  La inversa del invariante también se cuida: `fijar_parametros` rechaza un techo menor que algún mínimo por sucursal del producto.
  Las dos escrituras (`fijar_parametros` y `fijar_minimo_sucursal`) se **serializan por producto** antes de validar: PostgreSQL toma el candado de la fila del producto (`SELECT ... FOR UPDATE`), SQLite un `UPDATE` sin efecto que toma el candado de escritura de la base. Sin eso, un techo de 50 y un mínimo de 80 a la vez pasaban cada uno contra lo que el otro no había confirmado.
- **HTTP.** `build_reposicion_minimos_router`: `GET /{producto_id}/reposicion/minimos` y `PUT /{producto_id}/reposicion/minimos/{sucursal_id}` (cuerpo `{stock_minimo}`, número o `null`), con la
  misma estructura que `build_reposicion_parametros_router` (falla al construirse sin `dependencias_escribir`; 404 producto, 422 valor o sucursal inválidos, 503 sin la `0005`).

**Bordes dichos en voz alta.** (1) La vista de toda la instancia no refleja los mínimos por sucursal: un producto puede no figurar «a pedir» en el total y sí en una sucursal. (2) `delete_producto`
borra sus mínimos por sucursal antes (la FK lo exigiría); las sucursales no se borran nunca (baja lógica, ver `erp.catalogo`). (3) Si el global **baja**, los propios no se tocan; si el global
**sube** por encima del techo se rechaza como siempre, pero los propios no se validan contra el global (pueden ser mayores o menores). (4) `catalogo.update_producto` (subir el mínimo global contra el techo) no toma ese candado: su carrera con el techo viene de antes y no se cambió acá (la cerró ADR-026). (5) Un propio no se valida contra el techo de otra forma que
al fijarlo o al fijar el techo; una base con datos cruzados de antes de esta guarda sigue siendo regida por «el techo manda sobre el piso» (ADR-020).

**Consecuencias.** Falta la pantalla (libra-ui) y exponer el router en los productos. El router de `GET /api/reportes/reposicion` no cambia de contrato salvo el campo nuevo por fila.


## ADR-025 — Reposición v2: descontar lo que vence dentro del horizonte (2026-10-03)

**Contexto.** ADR-019 descuenta del stock lo que ya está vencido, pero un lote que vence dentro de poco y no se va a vender a tiempo sigue contando como stock: el producto figura cubierto y a los pocos
días se tira. Diferido desde ADR-019, ADR-022 y ADR-023.

**Decisión.** Parámetro **opt-in** `descontar_por_vencer` (**apagado por default**: cambia números que hoy se ven; sin él, el resultado es el de v0.36.0) en `sugerencia_reposicion`, en
`GET /api/reportes/reposicion` (query param, eco en la respuesta y su export CSV) y en el cuerpo de la generación de órdenes en borrador (las órdenes se calculan con el mismo ajuste que se ve, como
`estacionalidad`; la `clave_operacion` ya usada con el otro valor es un pedido distinto, 409). Cada fila trae **`por_vencer`** (cantidad, con la escala de informe; la columna del CSV va **al final**, después
de `stock_minimo_propio`; `0` sin la opción). Sólo cuenta para productos con `tracks_expiry = 1`, saldos positivos con fecha de los depósitos que se miran (la misma consulta de lotes que usa `vencido`,
`erp.lotes.saldos_por_bucket`: una sola, no dos); el saldo «sin lote» no cuenta y un lote con `vence < hoy` ya va en `vencido` y no entra acá (tampoco con `descontar_vencido=false`).

**La cuenta.** Con `H = dias_cobertura + plazo` del producto (el `horizonte` de la fila) y `r = proyectado / H` (la rotación diaria **proyectada**, que ya incluye el factor estacional si está prendido),
los lotes se venden por orden de vencimiento (FEFO). Para cada lote `j` con `d_j <= H`, donde `d_j = (vence_j − hoy).días + 1` (hoy cuenta y el día del vencimiento todavía se vende) y `C_j` es el saldo acumulado
de los lotes no vencidos hasta `j` inclusive, al vencer `j` se vendieron a lo sumo `r × d_j` unidades y sobran `C_j − r × d_j`.

    por_vencer = max(0, máx_j (C_j − r × d_j))          utilizable = max(stock − vencido − por_vencer, 0)

Es el **máximo** del acumulado y no la suma: lo que sobra de un lote ya está contado en el acumulado del siguiente. Nunca pasa de la suma de saldos de los lotes dentro del horizonte (`r × d_j >= 0`).
`stock` sigue siendo el real y `vencido` lo ya vencido; `cobertura_dias` usa el nuevo utilizable y `posible_quiebre` no cambia.

- **Aritmética exacta.** `proyectado`, `r`, la pérdida, el disponible, el mínimo y el techo son `Fraction` y se redondean con `_techo`/`_piso` (que ahora aceptan un `Fraction` y redondean con enteros):
  un resto decimal no pide una unidad de más ni de menos. Se midió al diseñarlo que con cocientes de `Decimal` (`1/30 × 5` y la resta que sigue) el caso «1 vendida en 30 días, horizonte 5, un lote de 1 que vence en 5 días»
  pedía 1 unidad cuando la cuenta exacta da 0. Las filas sin la opción dan lo mismo que antes (las suites previas, sin cambios salvo el encabezado del CSV, lo confirman). `por_vencer` se **informa**
  redondeado hacia arriba a la escala del informe, pero `sugerido` usa la pérdida exacta, no la redondeada.
- **Sin migración.** Una base sin la revisión `0002`, o sin productos marcados, devuelve `por_vencer = 0` sin error.

**Bordes dichos en voz alta.** (1) **Sin ventas (`r = 0`) todo lo que vence dentro del horizonte se pierde:** un producto sin rotación con un lote por vencer y mínimo > 0 se sugiere reponer (lo que va a vencer sin
venderse no sirve de colchón: es lo que dice la cuenta); sin mínimo no se sugiere nada. (2) Supone que la rotación de la ventana se mantiene todo el horizonte y que todo lo vendido sale de los lotes por FEFO: lo «sin lote» no compite con ellos, así que si en la práctica parte de lo vendido sale de lo
«sin lote», la pérdida real es mayor que la calculada (se subestima, no se sobreestima). (3) El acumulado junta los lotes de todos los depósitos que se miran, como un solo FEFO y una sola rotación (en la realidad cada depósito vende de lo suyo). (4) `por_vencer` no mira lo que viene en camino ni su vencimiento. (5) Dos lotes del mismo día valen como uno (el máximo cae en el último del día). (6) La `clave_operacion` de una
generación de órdenes hecha antes de este cambio, reintentada después, tiene otra huella (el parámetro entra en ella) y responde 409, como pasó con `estacionalidad`.

**Consecuencias.** Falta el interruptor y la columna en la reposición (libra-ui) y activarlo en VentaLibra. Con esto queda cerrado lo que ADR-019 había diferido («lo que vence dentro del horizonte»).

## ADR-026 — Reposición v2: el candado del producto en la edición y booleanos fuera de los campos numéricos (2026-10-03)

> **Nota (ADR-030, 2026-10-04).** Desde ADR-030 la canónica de `sin_booleanos` y de la guardia de booleanos que se mencionan acá vive en libracore v1.125.0 (`libracore.validacion` y `libracore.testing`); este repo las reexporta, con el mismo comportamiento. El texto de abajo es la historia y no se reescribe.

**Contexto.** Dos defectos de antes, medidos al revisar ADR-020 a ADR-025; ninguno cambia un contrato que alguien use bien.

**1. `catalogo.update_producto` y el techo.** Valida que el mínimo global no pase el techo (`max_stock`, ADR-020) leyendo el techo **sin candado**, y ADR-024 lo había dejado dicho como borde (4).
Con un techo de 100 y un hilo que lo baja a 50 sin haber confirmado, otro que sube el mínimo a 80 leía el 100, validaba y escribía apenas el primero confirmaba: mínimo 80, techo 50. **Decisión:**
`update_producto` toma `_bloquear_producto` (el candado de `fijar_parametros` y `fijar_minimo_sucursal`) **antes de leer** el techo, dentro de `_exigir_minimo_bajo_el_techo`. El orden de candados sigue siendo
producto primero (como `delete_producto`: producto y después mínimos por sucursal), así que no hay un orden nuevo que pueda cruzarse. Una base sin la revisión `0003` no tiene techos y no toma el candado.
El repositorio confirma dentro de `save_catalog_item` (salvo dentro de `transaction()`), así que el candado se suelta apenas se guarda el producto: validar y escribir quedan juntos.

**2. Booleanos como números en el cuerpo de `PUT /{producto_id}/reposicion` y de la generación de órdenes.** Los campos `int`/`float` de pydantic convierten `true` en `1` y `false` en `0` antes del
rechazo `isinstance(bool)` del motor: `plazo_entrega_dias: true` quedaba como 1 día, `stock_maximo: true` como un techo de 1.0 y `proveedor_id: true` como el proveedor 1. **Decisión:** el validador
`mode="before"` que ya tenía `MinimoDeSucursal` (ADR-024) pasa a ser uno compartido, `_sin_booleanos(*campos)`, que responde 422 con `true`/`false` y se aplica a `ParametrosDeReposicion`
(`plazo_entrega_dias`, `stock_maximo`, `proveedor_id`) y a `GenerarOrdenes` (`dias_rotacion`, `dias_cobertura`, `plazo_entrega_dias`, `sucursal_id`, `proveedor_id`, y los `producto_ids` y los valores de
`topes`, donde mira también adentro de la lista y del diccionario). Todo lo demás se convierte como antes: números, enteros, textos numéricos y el `0` numérico (que el motor sigue juzgando él).

**Bordes dichos en voz alta.** (1) Los parámetros de `GET` (`dias_rotacion=true` en la query) no tenían el defecto: son texto y pydantic no convierte `"true"` en entero. (2) Los campos `bool` de verdad
(`descontar_vencido`, `estacionalidad`, `descontar_por_vencer`) siguen aceptando lo que pydantic acepta como booleano (`1`, `"yes"`, …), como antes. (3) El candado nuevo toma la fila del producto en cada
edición, incluida la actualización masiva de precios (una por línea, confirmando cada una): en PostgreSQL una escritura que referencia al producto (una línea de venta, por la clave foránea) puede esperar lo que dure esa edición (el `FOR UPDATE` choca con el `FOR KEY SHARE` de la FK; no se midió
con carga); `fijar_parametros` y `fijar_minimo_sucursal` ya lo hacían. (4) `actualizacion_masiva.aplicar` relee el mínimo global antes de llamar a `update_producto`, fuera del candado: puede pisar con el valor de antes un mínimo que otro editó en el medio, pero
nunca lo sube por encima del techo.

## ADR-027 — Booleanos fuera de los campos numéricos en todos los routers, y la actualización masiva relee dentro del candado (2026-10-03)

> **Nota (ADR-030, 2026-10-04).** Desde ADR-030 la canónica de `sin_booleanos` y de la guardia de booleanos que se mencionan acá vive en libracore v1.125.0 (`libracore.validacion` y `libracore.testing`); este repo las reexporta, con el mismo comportamiento. El texto de abajo es la historia y no se reescribe.

**Contexto.** Dos pendientes que ADR-026 dejó dichos: (a) arregló en `reposicion_router` que pydantic convierte `true`/`false` en `1`/`0` en un campo `int`/`float` **antes** de que el motor (que en varios lugares rechaza el
booleano a propósito) lo vea, y quedaba el resto de los routers; (b) borde (4) de ADR-026: `actualizacion_masiva.aplicar` relee el producto fuera del candado que `update_producto` toma. Sin migración.

**1. Relevamiento, con método.** Un script (no a ojo) arma cada una de las 21 factories de `web/` con sus opciones prendidas (`con_vencimientos`, `con_lotes`, `por_deposito`, `promociones`, `con_avisos_de_vencimiento`),
recorre las rutas reales (`route.dependant`) y, para cada campo numérico de cada cuerpo (`int`, `float`, `Decimal`, y dentro de `list[...]`/`dict[...]` y de los modelos anidados), **instancia el modelo real
con `True` y con `False`** en esa posición (con los validadores incluidos) y anota si lo acepta; para los parámetros de query y de path numéricos prueba el texto `"true"`, que es como llegan. Resultado antes del cambio:

- **Aceptaban el booleano (y se arreglaron, `web/_validacion.sin_booleanos`):**
  - `catalogo_router`: `ProductoPayload` (`precio_venta`, `precio_costo`, `stock_minimo`; también `ProductoConVencePayload`, que hereda), `DepositoCreatePayload.branch_id`, `DepositoPredeterminadoPayload.deposito_id`,
    `TransferenciaPayload` (`producto_id`, `origen_id`, `destino_id`, `cantidad`, `variant_id`) y `AjustePayload` (`cantidad`, `factor`, `deposito_id`, `variant_id`; también `AjusteConLotePayload`).
  - `ventas_router`: `ItemPayload` (`qty`, `precio`, `producto_id`, `variante_id`), `PagoPayload` (`monto`, `recibido`), `VentaPayload` (`descuento`, `cliente_id`, `deposito_id`), `DevolucionLinea` (`sale_item_id`, `cantidad`),
    `DevolucionPayload.deposito_id`, y `PlanSalidaLinea`/`PlanSalidaPayload` (`producto_id`, `qty`, `variante_id`, `deposito_id`).
  - `listas_router`: `ItemsPayload.precios` (los valores), `AjustePorcentualPayload.porcentaje`, `ImportarPayload.fuente_lista_id`, `QuiebrePayload` (`min_quantity`, `amount`), `PrecioVigentePayload` (`monto`, `sucursal_id`,
    `cantidad_minima`) y `ListaDeClientePayload.lista_id`.
  - `promociones_router`: `ItemPromocionPayload` (`producto_id`, `cantidad`), `PromocionPayload` (`paga`, `precio`) y, en `POST /calcular`, `LineaCalculoPayload` (`producto_id`, `qty`, `precio`).
  - `compras_router`: `OrdenCreatePayload` (`proveedor_id`, `branch_id`), `OrdenItemPayload.item_id`, `RecepcionCreatePayload` (`proveedor_id`, `purchase_order_id`), `RecepcionItemPayload.item_id`, `ConfirmarPayload.deposito_id`.
  - `vencimientos_router` (los tres `POST` que escriben el ledger): `AsignarPayload`, `EntradaPayload` y `MermaPayload` (`producto_id`, `deposito_id`, `variante_id`).
  - `planillas_router` (otro mecanismo, el mismo defecto): una **celda VERDADERO** de la columna de costo del `.xlsx` llega como `True` y `float(True)` es `1.0`: la planilla bajaba el costo a 1 y el precio de venta con él.
    Se rechaza con el mismo texto de siempre («el costo "True" no es un número»). Una FALSO ya caía en «mayor que cero».
- **Ya lo rechazaban (se dejó fijado con tests, sin tocar):** todos los `Decimal` (`quantity_ordered`, `unit_cost`, `tax_rate`, `quantity` de compras y `cantidad` de los tres `POST` de vencimientos: pydantic no convierte un
  booleano en `Decimal`), los `bool` de verdad y `StrictBool` (`vence`, `activo`, `activa`, `es_principal`, `cobrar_con_qr`, `solo_activos`, …) y `Literal` (`tipo`, `modo`, `base`).
- **Llegaban al motor sin riesgo:** (i) las claves de un diccionario (`precios{id}`, `topes{id}`): una clave JSON siempre es texto, no hay clave booleana (el helper igual las mira); (ii) los 96 parámetros numéricos (distintos por ruta) de
  `query` y de `path` de todos los routers (`dias_rotacion`, `producto_id`, `sucursal_id`, `limite`, …): llegan como texto y `"true"` no se convierte en entero ni en flotante (medido: ninguno lo acepta); (iii)
  `QuiebrePayload.min_quantity`: el motor exige 2 o más, así que `true` (1.0) y `false` (0.0) se rechazaban igual, pero con un mensaje que no decía por qué; se aplicó el validador igual por uniformidad.
  Ojo con la otra mitad de la frase: **«el motor lo rechaza de todos modos» no vale como excusa** para `variante_id` de vencimientos (`_validar_variante` rechaza el booleano) ni para `isinstance(bool)` de reposición: pydantic convierte
  antes, así que el motor recibe un `1` y no ve nada. Por eso esos campos se arreglaron en el modelo.

**Decisión.** `_sin_booleanos` de `reposicion_router` se movió a `libracommerce/web/_validacion.py` como `sin_booleanos(*campos)`, **sin cambiar su comportamiento ni su mensaje** («<campo> tiene que ser un número, no un booleano»,
422, también dentro de listas y diccionarios); `reposicion_router` lo importa. Se aplica en los campos listados arriba, y sólo donde un `1` o un `0` producen un efecto real de negocio (ids de producto, proveedor, sucursal,
depósito, lista, variante y cliente; cantidades, precios, costos, descuentos, porcentajes y factores). **`false` tampoco es un `0`**, aunque `0` sea un valor legítimo del campo (un conteo en cero, un descuento de 0, `stock_minimo: 0`):
el `0` numérico sigue valiendo, el booleano no. Las respuestas no cambian de forma. `POST /ventas/plan-salida` y `POST /promociones/calcular` son lecturas que no escriben, pero son lo que el punto de venta muestra antes de cobrar:
un `qty: true` mostraba el plan o el ahorro de una unidad; se aplicó también ahí. Los tests (`tests/test_web_booleanos.py`) llevan, para cada endpoint tocado, `true` y `false` en cada campo arreglado: 422, con el
mensaje, y **ninguna tabla cambia** (se compara el contenido de todas); el cuerpo numérico y el mismo con los números como texto siguen dando 200. Sin el validador, los 18 casos × 2 motores fallan (se probó sobre una copia del repo fuera
del worktree con los routers de antes: precio 1.0 escrito, venta de una unidad, +1 % a toda una lista, costo 1.0 desde la planilla).

**2. La actualización masiva relee dentro del candado.** `update_producto` reescribe **todos** los campos del producto con lo que se le pasa y `aplicar` los sacaba de una relectura hecha antes de tomar `_bloquear_producto`:
un mínimo editado en el medio (también el nombre, la categoría, el `activo`, …) se pisaba con el valor de antes. Dos salidas: (a) tomar el candado en `aplicar` **antes de releer**, o (b) no releer ni reescribir lo que no cambia
(un `UPDATE` acotado de costo y precio). Se eligió **(a)**: `aplicar` llama `_bloquear_producto(conn, item_id)` y recién después `catalogo.get_producto` y `update_producto`. Razones: el repositorio confirma dentro de `save_catalog_item`
(salvo dentro de su `transaction()`, que acá no aplica porque `repositorio_de(conn)` crea uno nuevo por llamada), y el candado (`FOR UPDATE` de la fila en PostgreSQL, el candado de escritura en SQLite) es de la transacción de
`conn`, así que lo que `aplicar` toma queda tomado a través de la relectura y de la escritura, y `update_producto` lo vuelve a tomar sin esperar (la misma transacción) y lo suelta al confirmar: releer y escribir son una sola
sección crítica, por línea, sin refactorizar `update_producto`. La (b) habría sido un camino de escritura paralelo al del producto (se salteaba `repo.save_catalog_item`, sus ganchos y el `_set_codigo`), que es justo lo que el docstring del
módulo dice que no hace. Orden de candados: **producto primero**, igual que `delete_producto`, `fijar_parametros`, `fijar_minimo_sucursal` y `update_producto`; la tanda no retiene el candado de una línea al pasar a la siguiente (cada
línea confirma), así que dos masivas con líneas en distinto orden no se esperan una a otra. Test (`tests/test_actualizacion_masiva.py`): dos hilos con barrera determinista, contra SQLite y PostgreSQL; la masiva, al releer, avisa y espera
al otro hilo, que sube el mínimo de 5 a 9 y confirma. Sin el arreglo el mínimo final es 5 (la masiva lo pisó); con él es 9 y el costo y el precio son los nuevos.

**Bordes dichos en voz alta.** (1) Un producto que hoy mande `true`/`false` a propósito en alguno de estos campos recibiría 422: la suite del motor no tiene ninguno, y los productos (VentaLibra, Contalibra, Restolibra) tienen que correr la
suya al subir el pin; los `bool` de verdad no cambian. (2) Un producto que **herede** un payload (`ProductoPayload`, `AjustePayload`) y le sume campos numéricos propios no los tiene cubiertos: aplicar `sin_booleanos` a los suyos. (3) `aplicar`
ahora toma el candado también en una base sin la revisión `0003` (`update_producto` no lo toma ahí): es la misma fila y el mismo orden, y arregla el mismo lost update en el nombre o la categoría; el borde (3) de ADR-026 (una escritura
que referencia al producto —una línea de venta, por la FK— puede esperar lo que dure la edición en PostgreSQL) vale ahora también para esas bases. (4) Queda una ventana mínima que no cubre el candado: `update_producto` guarda el producto
(confirma, suelta el candado) y recién después hace `_set_codigo`; si en ese instante otro cambia el código principal, `_set_codigo` lo reemplaza por el que `aplicar` había leído. Es el mismo orden de siempre de `update_producto` (no lo
toca esta decisión); el código principal casi nunca cambia y `_set_codigo` no escribe si es el mismo. (5) `PUT /api/productos/{id}` sigue siendo un reemplazo completo con lo que manda el cliente: un precio editado a mano con datos viejos pisa
el de otro; lo que se cierra acá es que la **masiva** pise lo que ella misma lee. (6) Si un producto pasa su propia fábrica de repositorio (`repositorio_de`) y su `save_catalog_item` no confirma, el candado queda tomado hasta que `conn` confirme,
y varias líneas de una misma tanda retendrían varios candados a la vez (en el orden de las líneas); los dos productos que usan la masiva hoy confirman por línea. (7) El test de la masiva simula el «otro hilo» con una escritura que
toma el candado y toca sólo el mínimo: no hay hoy en el motor un endpoint que cambie el mínimo global sin reescribir el resto del producto.

**Consecuencias.** Sin migración; la versión sale del tag. Cierra el borde (4) de ADR-026. Los routers nuevos que reciban `int`/`float` en un cuerpo y donde un `1` o un `0` cambien algo deben usar `sin_booleanos` (ARCHITECTURE.md).


## ADR-028 — Guardia reutilizable contra booleanos en campos numéricos, «por vencer» con la escala de la unidad, y `update_producto` guarda el producto y su código en una transacción (2026-10-03)

> **Nota (ADR-030, 2026-10-04).** Desde ADR-030 la canónica de `sin_booleanos` y de la guardia de booleanos que se mencionan acá vive en libracore v1.125.0 (`libracore.validacion` y `libracore.testing`); este repo las reexporta, con el mismo comportamiento. El texto de abajo es la historia y no se reescribe.

> **Nota (ADR-031, 2026-10-04).** Desde ADR-031 `repo.transaction()` **anida** (la profundidad es de la conexión, cada interior es un `SAVEPOINT`); el «no anida» y el «sobre OTRA instancia no cambia» de abajo son la historia y no se reescriben.

**Contexto.** Tres pendientes que ADR-026 y ADR-027 dejaron dichos. Sin migración; la versión sale del tag.

**1. La guardia: `libracommerce.testing.campos_numericos_que_aceptan_booleano(app, *, ignorar=frozenset())`.** ADR-027 relevó los routers con un script que se tiró; ADR-027 (borde 2) dejó dicho que un producto que hereda un payload y le suma un número propio, o monta un router propio, no queda
cubierto. Una regla escrita en ARCHITECTURE.md no avisa del campo que se olvidó. El script pasó a ser una función pública del motor (`libracommerce/testing.py`, que viaja en el wheel; hasta ahora el repo sólo tenía helpers de test en `libracore.testing`), para que **cada producto la corra en su suite sobre su `create_app()` completa**:

```python
from libracommerce.testing import campos_numericos_que_aceptan_booleano

def test_ningun_campo_numerico_acepta_un_booleano():
    assert campos_numericos_que_aceptan_booleano(create_app()) == []
```

*Contrato.* Recibe una `FastAPI` (o un `APIRouter`) ya armada; devuelve `list[tuple[str, str, str]]` ordenada y sin repetidos: `(método y ruta, campo, tipo)`, p. ej. `("POST /api/ventas", "items[].qty", "float")`; el campo va en notación de punto (`[]` una lista, `{valor}` el valor de un
diccionario, y el nombre del parámetro delante si la ruta tiene más de un cuerpo o el cuerpo es un número suelto). **Vacía es lo esperado**; lo que devuelve es lo que *sigue* aceptando `true`/`false`. Es sólo de lectura: no ejecuta ningún endpoint ni toca una base.

*Criterio: se mide, no se lee el código.* Para cada ruta (con el prefijo de su `include_router`, lo que cuelga de un `Mount` y los parámetros de sus `Depends`) y cada hoja numérica (`int`, `float`, `Decimal`; dentro de modelos anidados, `list`/`set`/`tuple[T, ...]`, `dict` y uniones) **instancia el modelo real, con sus validadores,**
con `True` (y con `False`) en esa posición y lo compara con el mismo cuerpo con `1` (y con `0`): si la validación da **exactamente lo mismo**, el booleano pasó como número y el campo se informa. Un modelo cuyo resto no se pudo armar con valores de relleno falla igual en las dos medidas y no cuenta. Un campo con
`ge=2` que recibe `true` falla por el rango, igual que un `1`: se informa, porque el booleano se convirtió en número y se lo rechazó por otra razón (el caso de `QuiebrePayload.min_quantity`, ADR-027 (iii)). En query, path, header, cookie y formulario, que llegan como texto, se prueba `"true"` contra `"1"` (hoy ninguno lo acepta).

*Qué ignora, a propósito.* `bool`, `StrictBool`, `StrictInt`/`StrictFloat` y `Literal` (no son hojas numéricas o ya lo rechazan: la medición lo confirma sin necesidad de listarlos); `Decimal`, que se mide igual pero pydantic ya lo rechaza solo (si una versión futura lo aceptara, aparecería); las claves de un diccionario (en JSON son texto);
y lo que no es un modelo pydantic (un `Any`, una tupla de largo fijo, una dataclass; tampoco la **celda** de una planilla: la de `planillas_router` la cubre `test_web_planillas.py`). **`ignorar={(ruta, campo), …}`**: excepciones a sabiendas, con la `ruta` como la devuelve la función (`"POST /api/ventas"`) o sin el método (`"/api/ventas"`, vale
para todos). Sólo para un campo donde un `1` o un `0` no cambian nada del negocio (un orden de pantalla, un contador sin efecto), **con un comentario al lado que lo justifique**; no es un atajo para tapar un informe. Una entrada que no coincide con nada no avisa; una mal formada (no son dos textos) levanta `ValueError`.

*Límite.* Un `model_validator(mode="after")` que rechace el booleano sólo se ve si el resto del modelo se pudo armar con los valores de relleno (los campos requeridos de al lado). Los `field_validator` y los `before` (que es lo que es `sin_booleanos`) se ven siempre.

*Medido.* Con las 21 factories de `web/` en una sola app, la función mide 293 hojas en 108 operaciones y devuelve `[]` (`tests/test_guardia_booleanos.py`). Con `sin_booleanos` convertido en un no-op **antes** de importar los routers (otro proceso; es el segundo test del archivo) devuelve 80 campos, los de ADR-026 y ADR-027: la guardia no es muda, así que ese `[]` significa algo.
Los tests de la función (routers de juguete) fijan: un `int`/`float`/`list[int]`/`dict[str, float]`/anidado/lista de modelos sin el validador se detecta, campo por campo; con `sin_booleanos` no; `bool`, `Decimal`, `StrictInt`, `Literal` no; un validador propio que rechaza el booleano no se marca (se mide el resultado, no el mecanismo); `ignorar` con y sin método; el prefijo del `include_router`, un `Mount`, un `APIRouter` suelto, una dependencia y un
payload heredado con un número nuevo. **FastAPI 0.141 ya no aplana `app.routes` al incluir un router** (deja un `_IncludedRouter` perezoso: una app armada con 22 `include_router` mostraba 26 entradas, 22 de ellas `_IncludedRouter` y las otras 4 las de la documentación; ninguna era una `APIRoute`): la función recorre `fastapi.routing.iter_route_contexts`, lo mismo que usa FastAPI para armar el OpenAPI, y cae a `app.routes` en una versión que no la tiene
(el rango que declara el proyecto es `fastapi>=0.115,<1`; **sólo se probó con 0.141.1**).

**2. «Por vencer» con la escala de la unidad.** `por_vencer` se informaba con `max(escala, 3)` decimales (los 3 mínimos del informe) mientras `sugerido` iba con la `escala` de la unidad: una unidad entera (`allows_fraction = 0`) mostraba `8.667` (`23.572` en el caso que se reportó) en una columna al lado de un `sugerido` entero.
Ahora `por_vencer` se informa **redondeado hacia arriba a la `escala` de la unidad**, igual que `sugerido`: entero para una unidad entera, `decimal_scale` (o 3 si no lo trae) para una fraccionable. **La cuenta no cambia**: `sugerido`, el mínimo, el techo y `cobertura_dias` siguen usando la pérdida exacta (`Fraction`), no el número informado.
Un test lo ata: 10 vendidas en 30 días, lote de 10 que vence en 3 días, horizonte 13: la pérdida exacta es 26/3, `por_vencer` sale `9` y `sugerido` `3` (con el 9 redondeado saldría 4, y la cobertura 3 días en vez de 4). Tres casos nuevos en `tests/test_por_vencer.py` (entera, kg con 3 decimales: `8.667`, y una unidad de 1 decimal: `8.7`); sin el cambio, 5 tests de ese archivo se ponen rojos.
*Efecto visible:* para una unidad entera `por_vencer` pasa de `float` a `int` también con la opción apagada (`0.0` es `0`), en el JSON y en la columna del CSV de la reposición (`...,no,0` en vez de `...,no,0.0`); una fraccionable no cambia (salvo que su `decimal_scale` sea menor que 3: informa con menos decimales, hacia arriba). Un consumidor que compare el texto del CSV lo ve; uno que lo lea como número, no.

**3. `update_producto` guarda el producto y reemplaza su código principal en UNA transacción.** El borde (4) de ADR-027: `update_producto` guardaba el producto (el repositorio confirmaba dentro de `save_catalog_item` y soltaba el candado) y **recién después** hacía `_set_codigo`. Se reprodujo con una barrera determinista, contra SQLite y PostgreSQL:
el hilo A edita sin querer cambiar el código (manda el `111` que leyó, como la masiva) y se detiene entre el guardado y `_set_codigo`; el hilo B edita el mismo producto con el código `222` y confirma sin esperar (el candado ya estaba libre); al seguir, el `_set_codigo` de A ve `222`, distinto de su `111`, y lo reemplaza. Medido: precio de B guardado y código de B perdido (`111`, esperado `222`).
Un primer informe de este ADR lo dejó abierto por dos riesgos que no se habían medido; se midieron y se aplicó.

**Decisión.** El guardado y `_set_codigo` van dentro de `with repo.transaction():`. Ningún paso confirma solo, hay un único commit al salir, y el candado del producto (`_exigir_minimo_bajo_el_techo`, o el que toma quien llama: la masiva) sigue tomado hasta ahí. (Mover `_set_codigo` antes del guardado no servía: `save_item_code` también confirma y soltaba el candado antes de guardar el producto, el defecto de ADR-026 al revés.)

**Lo que cambia para los productos.**
- **Un código repetido pasa a dar 422 y NO guardar nada.** Antes el 422 llegaba tarde, con el nombre, el precio, la categoría nueva y demás campos ya confirmados (`test_limitacion_preexistente_la_edicion_no_es_atomica_respecto_del_codigo_duplicado` lo fijaba como limitación preexistente y decía «si algún día el guardado se hace atómico, hay que invertir las dos primeras aserciones»: se invirtió y ahora se llama `test_la_edicion_es_atomica_respecto_del_codigo_duplicado`). Cubre el `PUT /api/productos/{id}` del router y la actualización masiva (una línea con un código repetido no deja guardado su costo ni su precio).
- Un `RepositorioAuditado` (el de VentaLibra, declarado con `usar_fabrica_de_repositorio`) ahora **audita dentro de la transacción**: la fila de `actividad_log` se confirma junto con el producto y se revierte con él (ver abajo, es un arreglo de `db/auditoria.py`).
- La ventana de ADR-027 (4) queda cerrada: `test_el_codigo_de_otra_edicion_no_se_pisa_entre_el_guardado_y_el_reemplazo` (los dos motores, barrera determinista) da `222`; sin el cambio da `111`.
- **Cada producto debe correr su suite completa al subir el pin**, en especial lo que mire el estado tras un 422 de código repetido y lo que lea `actividad_log` tras una edición de producto.

**Lo medido (los tres riesgos).**
1. *Con un repositorio que audita.* `RepositorioAuditado._registrar` hacía `self._conn.commit()` a secas, así que **adentro de `repo.transaction()` cada fila del log confirmaba todo lo escrito hasta ahí**: con sólo envolver `update_producto`, el guardado y la auditoría se confirmaban antes del `_set_codigo` y un código repetido dejaba el producto editado **y** su fila de log (6 de los tests nuevos en rojo, 2 conteos de commit: 2 adentro, 0 esperado). Es un defecto latente de antes en el motor (`usecases/sales.py` e `inventory.py` ya usan `repo.transaction()` y con un repositorio auditado su atomicidad se rompía igual). **Arreglo:** `_registrar` confirma con el `_commit()` del repositorio de adentro, que no confirma dentro de `transaction()` y sí fuera (un repositorio sin `_commit` conserva el commit de siempre). Fuera de una transacción nada cambia (`tests/test_auditoria.py` sigue verde); dentro, la auditoría se confirma o se revierte con lo que audita. Un test con un repositorio auditado (`RepositorioAuditado(SqliteCommerceRepository(conn), conn)`, igual al de VentaLibra) fija: una edición buena registra `editar producto` + `crear codigo`; un código repetido no deja ni el producto ni su auditoría; un error adentro de `transaction()` revierte la fila del log.
2. *`transaction()` en todas las implementaciones.* El puerto (`ports/persistence.CommerceRepository`) lo declara y el motor tiene dos: `SqliteCommerceRepository` (la misma clase sirve a SQLite y a PostgreSQL: recibe la `conn` de LibraCore) y `RepositorioAuditado` (delega por `__getattr__` en la de adentro). Con una conexión que cuenta los `commit()`: dentro de `transaction()`, `save_catalog_item` y `save_item_code` no confirman (0) y al salir se confirma una vez (1), en las dos implementaciones y los dos motores. Un repositorio de un producto que no cumpla el puerto no tendría `transaction()` y `update_producto` fallaría con `AttributeError`: no hay ninguno en este repo.
3. *Los otros llamadores y los candados.* `update_producto` sólo lo llaman el router (`PUT /api/productos/{id}`) y `actualizacion_masiva.aplicar`; `fijar_parametros` y `fijar_minimo_sucursal` no lo llaman. El candado (`FOR UPDATE` en PostgreSQL, el candado de escritura en SQLite) es de la transacción de `conn` y `transaction()` no abre otra: no hay doble toma que se espere a sí misma, y `aplicar`, que ya lo tomaba antes de releer, lo vuelve a tomar dentro de `update_producto` en la misma transacción (el test de ADR-027 sigue pasando contra los dos motores). Orden de candados: producto primero, igual que `delete_producto`, `fijar_*` y `update_producto`; lo único que se suma bajo el candado es la fila de `item_codes` de **ese** producto (`DELETE` + `INSERT`), que ninguna otra ruta toma antes que el producto. Una escritura que referencia al producto (una de `item_codes` por `add_codigo`, vía la clave foránea) espera lo que dure la edición, como ya valía para `fijar_*` (ADR-026, borde 3). Medido con barrera, PostgreSQL: cuatro hilos editando el mismo producto a la vez terminan todos y el producto queda con el código y el precio de **uno solo** (sin la transacción, queda con el código de uno y el precio de otro), y dos productos que quieren el código del otro a la vez terminan los dos, sin deadlock (fallan por repetido: cada código sigue siendo del otro). Un llamador que ya estuviera en su propia `repo.transaction()` sobre OTRA instancia del repositorio no cambia: `update_producto` confirmaba igual antes (cada llamada arma el suyo).

**Bordes dichos en voz alta.** (1) La guardia no corre sola: cada producto tiene que agregar su test; hasta entonces un router propio sin `sin_booleanos` sigue sin avisar. (2) Un producto que ya mande booleanos a propósito a un campo numérico lo verá en el informe, no en producción (el 422 de ADR-027 no cambió). (3) Un `model_validator(mode="after")` con relleno inarmable no se mide (arriba). (4) La medición compara con `1`/`0`: un campo que acepta el booleano y lo
rechaza después con un mensaje propio de rango se informa (ver `ge=2`); si ese es el comportamiento querido, va en `ignorar` con su comentario. (5) Un producto que sustituya `RepositorioAuditado` por un envoltorio propio que confirme por su cuenta adentro de `save_*` no cierra la ventana ni gana atomicidad: tiene que respetar `_commit()` como ahora el del motor. (6) Un `PUT /api/productos/{id}` sigue siendo un reemplazo completo con lo que manda el cliente (ADR-027, borde 5). (7) La reversión de la auditoría vale para el repositorio del motor; la de VentaLibra se midió acá con el mismo `RepositorioAuditado`, no con la suite de VentaLibra.

**Consecuencias.** Sin migración; la versión sale del tag. `libracommerce/testing.py` es API pública nueva del motor (extra `[web]`). `por_vencer` cambia de forma en las unidades enteras (punto 2). `update_producto` es atómico (un código repetido ya no guarda nada) y `RepositorioAuditado` no confirma dentro de `transaction()` (punto 3). Los productos suman el test de la guardia a su suite y, si les informa algo, ponen `sin_booleanos` o lo justifican en `ignorar`; y **corren su suite completa al subir el pin**. Cierra el borde (4) de ADR-027.


## ADR-029 — Un código repetido es un error de dominio en castellano, no el texto de la base (2026-10-04)

> **Nota (ADR-031, 2026-10-04).** Desde ADR-031 `transaction()` anida: el borde (2) de abajo («un producto que lo llame dentro de su propio `transaction()` recibe `RuntimeError`») ya no vale; la historia no se reescribe.

**Contexto.** Hallado en una verificación en Chromium real: `PUT /api/productos/{id}` con un código que ya tiene otro producto daba 422, pero el `detail` era el texto crudo de la base, en inglés —`duplicate key value violates unique constraint "item_codes_code_type_code_key" DETAIL: Key (code_type, code)=(internal, YERBA-1) already exists.` en PostgreSQL, `UNIQUE constraint failed: item_codes.code_type, item_codes.code` en SQLite— y la UI lo mostraba tal cual. Desde ADR-028 la edición guarda producto y código en una transacción, así que el rollback era correcto y la excepción subía cruda hasta `raise HTTPException(422, str(e))`. Lo mismo pasaba en el alta y en `POST /api/productos/{id}/codigos`. Sin migración; la versión sale del tag.

**Decisión.**
1. **`catalogo.CodigoRepetido(ValueError)`**, con `.codigo` y el mensaje «Ya existe un producto con el código «X».» (la base queda de `__cause__`). Es un `ValueError` como el resto de los rechazos del motor: el `except Exception` del router lo manda a 422 con ese texto, sin tocar su forma (`{"detail": "..."}`).
2. **Un solo helper decide si un error es ése: `catalogo._es_codigo_repetido(exc)`**, y un solo context manager (`_codigo_repetido_como_error_de_dominio(codigo)`) lo aplica en TODO camino del motor que escribe un código: `create_producto`, `update_producto`, `add_codigo` y `agregar_codigo_balanza` (la actualización masiva pasa por `update_producto` con el código sin cambiar, así que no escribe uno nuevo). Se aplica por **fuera** de `repo.transaction()`: al traducir, el rollback ya se hizo.
3. **Cómo se detecta (por clase, no por texto).** *PostgreSQL*: `psycopg.errors.UniqueViolation` con `diag.constraint_name == "item_codes_code_type_code_key"`. Ojo: `libracore` convierte el error de psycopg en un `sqlite3.IntegrityError` (para que los `except` de los productos anden en los dos motores) y deja el de psycopg de `__cause__`, así que el helper recorre la cadena de causas. *SQLite*: `sqlite3.IntegrityError` con `sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"`; SQLite no da el nombre de la restricción, sólo sus columnas en el mensaje, y **eso es lo único que se lee de un texto**: el conjunto `{item_codes.code_type, item_codes.code}`, y sólo cuando la clase ya es la de una unicidad. Un segundo principal (`idx_item_codes_one_primary_per_item` / `item_codes.item_id`), una FK, un NOT NULL o la unicidad de otra tabla **no** se traducen y siguen su camino de hoy. El nombre en el texto de un error de PostgreSQL tampoco engaña: manda la causa.
4. **`create_producto` pasa a ser atómico, como `update_producto` (ADR-028).** Medido: el alta confirmaba el producto en `save_catalog_item` y recién después fallaba `_set_codigo`, así que un código repetido devolvía 422 pero **dejaba el producto guardado y sin código** (en los dos motores). Ahora producto, categoría nueva y código van en una `repo.transaction()`.

**Status.** No cambian: 422 en el alta y la edición; **409 en `POST /api/productos/{id}/codigos`** (`test_los_codigos_de_un_producto` lo fija, y un código ya existente es un conflicto): sólo cambia el texto. El segundo principal conserva su mensaje («Ese código ya existe, o el producto ya tiene un código principal.»).

**Bordes dichos en voz alta.** (1) La constante `item_codes_code_type_code_key` es el nombre que PostgreSQL da al `UNIQUE(code_type, code)` de `init_schema`; una base cuyo schema lo nombre distinto caería al camino de antes (el texto crudo, 422/409), nunca a un falso «código repetido». (2) `create_producto` ahora usa `repo.transaction()`, que **no admite anidamiento**: un producto que lo llame dentro de su propio `transaction()` recibe `RuntimeError` (lo mismo que ya pasaba con `update_producto` desde ADR-028); en este repo sólo lo llama el router. (3) El mensaje dice «producto» aunque el choque sea con un código de otro tipo de uso (el único es `(tipo, código)`): es el texto pedido.

**Consecuencias.** Sin migración; la versión sale del tag. `CodigoRepetido` es API pública nueva del motor. Los productos que ya atrapaban `sqlite3.IntegrityError` alrededor de `add_codigo`/`create_producto` para un código repetido ahora reciben `CodigoRepetido` (un `ValueError`, con la `IntegrityError` de `__cause__`): lo revisan al subir el pin y corren su suite completa. `tests/test_codigo_repetido.py` fija los dos motores.

## ADR-030 — `sin_booleanos` y la guardia de booleanos se reexportan de libracore: el extra `web` pide `libracore>=1.125` (2026-10-04)

**Contexto.** La regla del humano (`reglas/producto.md` del wiki, 2026-10-03): «toda lógica de fondo se escribe y se arregla en libracore». Este motor tenía copias de dos piezas que no son del comercio sino de cualquier router de la familia: `web/_validacion.sin_booleanos` (ADR-026, ADR-027) y `testing.campos_numericos_que_aceptan_booleano` (ADR-028). Con libracore v1.125.0 (ADR-013 de libracore) quedaron las canónicas: `libracore.validacion` (`sin_booleanos(*campos)` y `rechazar_booleanos(valor, campos, donde, *, esperado)`) y `libracore.testing` (`campos_numericos_que_aceptan_booleano(app, *, ignorar)`, en `libracore/testing/booleanos.py`). Cada copia traía su propio arreglo futuro: un defecto de la guardia había que corregirlo en dos repos. Sin migración; la versión sale del tag (se propone **v0.40.0**: sube el piso de libracore del extra `web`).

**Medido antes de cambiar.** Se compararon con `diff` las copias de este repo contra `git show v1.125.0:libracore/validacion.py` y `…:libracore/testing/booleanos.py`: el cuerpo de `sin_booleanos` y toda la guardia (`_pelar`, `_dummy`, `_hojas`, `_errores`, `_se_convierte`, `_rutas`, `_dependants`, `_parametros`, `_ignorados`, `campos_numericos_que_aceptan_booleano`, `_tipo_del_parametro`, `_nombre_del_campo`) son **línea por línea iguales**, con el mismo mensaje (422): «<campo> tiene que ser un número, no un booleano». Sólo difieren los docstrings (las referencias a ADR-026/027/028 contra ADR-013) y tres puntos de import: (1) este repo pasaba por `web.fastapi()` antes de importar FastAPI, para levantar `SinFastAPI` (ya no, ver Bordes); (2) libracore trae además `rechazar_booleanos`, que este motor no tenía; (3) en este repo el módulo vivía en la capa `web/`.

**Decisión.**
1. `libracommerce/web/_validacion.py` y `libracommerce/testing.py` pasan a ser módulos finos que **reexportan** (`__all__`): `sin_booleanos` y `rechazar_booleanos` de `libracore.validacion`, y `campos_numericos_que_aceptan_booleano` de `libracore.testing`. Se reexporta, y no se cambia a cada llamador, porque los siete routers (`catalogo`, `compras`, `listas`, `promociones`, `reposicion`, `vencimientos`, `ventas`) y los productos ya importan de ahí: ninguno se toca. La identidad es la misma (`libracommerce.web._validacion.sin_booleanos is libracore.validacion.sin_booleanos`, y lo mismo para `rechazar_booleanos` y la guardia), y `tests/test_reexporta_libracore.py` la fija.
2. **El extra `web` pide `libracore>=1.125,<2`** (sin URL, igual que `migrations` y `erp`: el consumidor pinea el tag). El piso es 1.125.0 porque ahí llegan `libracore.validacion` y `libracore.testing.booleanos`. Esto es una excepción **documentada y sólo para `web`** al principio de P9-M0 («el motor no importa libracore en runtime», comentario de `pyproject.toml`): `dependencies = []` del núcleo **no cambia** y sin extras el motor (dominio, repositorio, schema) sigue sin importar libracore; `erp` y `migrations` ya lo importan y ya lo pedían por versión. **Medido, y dicho sin maquillar:** la capa `web/` ya dependía de libracore sin declararlo: `web/ventas_router.py` hace `from libracore import medios_pago, pagos` y `from libracore.db.caja import …` a nivel de módulo (líneas 47 a 49), y `catalogo_router` y `listas_router` lo importan dentro de funciones; el extra `web` no lo pedía, así que `pip install libracommerce[web]` solo ya fallaba al importar `ventas_router`. Esta decisión declara lo que ya era cierto, con un piso que además cubre lo nuevo. FastAPI y pydantic siguen en el extra `web` y también los trae libracore.
3. Sin libracore, o con uno anterior a v1.125.0, importar `libracommerce.testing` o `libracommerce.web._validacion` (y por ende cualquier router que use `sin_booleanos`) levanta `libracommerce.web.SinLibracore`, un `ImportError` cuyo texto dice «libracommerce[web] necesita libracore>=1.125 (pip install libracommerce[web])» y que viene `from` el `ImportError` original. Mismo criterio que `SinFastAPI` y `SinOpenpyxl`: que la ausencia se lea como lo que es.
4. El pin de la suite (`dev`: `libracore @ git+…@v1.122.0`) sube a **v1.125.0**; `uv.lock` se regeneró con `uv lock`.

**Qué no cambia.** La API pública (`sin_booleanos` y `campos_numericos_que_aceptan_booleano` se importan de los mismos módulos, con las mismas firmas y el mismo informe); el mensaje y el 422; los routers; el comportamiento de la guardia (21 factories, `[]`). `tests/test_guardia_booleanos.py` y `tests/test_web_booleanos.py` pasan sin tocar sus aserciones. El test de que la guardia «no es muda» anula ahora `libracore.validacion.sin_booleanos` (la canónica, antes de importar los routers) y mide los mismos campos.

**Bordes dichos en voz alta.** (1) **Los productos que suban libracommerce a esta versión tienen que tener libracore >= v1.125.0 pineado**; con uno anterior pip/uv no resuelve (el extra `web` lo pide) o, si instalan sin el extra, el `SinLibracore` de arriba lo dice. (2) `libracommerce.testing` ya no pasa por `web.fastapi()`: sin FastAPI instalado (pero tampoco libracore, que lo trae) el error es `SinLibracore`, no `SinFastAPI`; con libracore instalado FastAPI siempre está. (3) `rechazar_booleanos` queda disponible desde `libracommerce.web._validacion`, pero este motor no lo usa (sus cuerpos son tipados). (4) Desde ahora un arreglo de `sin_booleanos` o de la guardia se hace en libracore y llega acá al subir el piso; las notas de ADR-026/027/028 apuntan acá y no se reescribe su historia.

**Consecuencias.** Menos código propio (−202 líneas netas en `testing.py` y `_validacion.py`, contando el docstring nuevo), una sola fuente de verdad, y la guardia que corre cada producto es la de libracore sin importar por qué motor la importe. Los productos corren su suite completa al subir el pin, como siempre.


## ADR-031 — `repo.transaction()` es reentrante: la exterior confirma, cada interior es un SAVEPOINT (2026-10-04)

**Contexto.** Desde ADR-028 y ADR-029 `update_producto` y `create_producto` hacen `with repo.transaction(): save_catalog_item(...); _set_codigo(...)`. ADR-028 dejó dicho el borde: `transaction()` **no anida**, y un producto de la familia (VentaLibra, Contalibra, Restolibra) que llame a esas funciones dentro de SU propia `transaction()` (la de un caso de uso, la de una importación) tiene un problema. Sin migración; la versión sale del tag (se propone **v0.40.1**: arreglo sin cambio de API).

**Medido antes de cambiar** (`tests/test_transaction_reentrante.py`, 38 casos, los dos motores; contra el código de v0.40.0 fallan 28 y pasan 10). El borde estaba **peor que como se lo dijo**, en dos partes:
1. El mismo objeto anidado sí daba `RuntimeError: transaction() no admite anidamiento`.
2. Pero `update_producto` y `create_producto` arman su repositorio con `repositorio_de(conn)`, que devuelve **uno nuevo en cada llamada**, y la bandera `_en_transaccion` era del objeto. Un producto con `with repo.transaction():` sobre su instancia y un `update_producto(conn, ...)` adentro **no recibía error**: el `transaction()` de adentro (otra instancia, bandera en `False`) **confirmaba al salir**, o sea todo lo que la exterior llevaba escrito. La exterior ya no podía deshacerlo: tras un `raise` posterior quedaban producto, código y fila de auditoría grabados (`test_una_excepcion_en_la_exterior_despues_de_un_update_interior...`, rojo en SQLite y PostgreSQL). Un commit en el medio de lo que otro cree atómico es peor que un `RuntimeError`, que al menos se ve.

**Decisión.**
1. **La profundidad es de la conexión, no del repositorio**: `libracommerce.db.repository._PROFUNDIDAD` (`id(conn)` → niveles abiertos). Es un dict del módulo porque `sqlite3.Connection` no admite atributos ni referencias débiles (medido); la entrada existe sólo mientras haya una transacción abierta (se borra al salir la exterior, bien o mal), así que ni se acumula ni un `id()` reusado hereda una profundidad. `_en_transaccion` queda como propiedad de sólo lectura sobre ese dict.
2. **La transacción exterior es dueña del commit y del rollback.** Una interior no confirma ni revierte la exterior. Una excepción que se propaga hasta la exterior la revierte entera (lo de siempre para una sola capa).
3. **Cada interior es un SAVEPOINT** (`SAVEPOINT lcm_sp_<nivel>`; al salir bien `RELEASE`; al fallar `ROLLBACK TO` + `RELEASE` y la excepción sigue subiendo). Si el código que rodea al `with` interior la atrapa, la exterior queda viva **con lo anterior intacto** y revierte sólo lo de la interior: no hay un medio revertido. Vale en cada nivel: si falla una del medio, se va con ella lo de las interiores que ya se habían liberado (probado con tres niveles).
4. **`_commit()` mira la profundidad de la conexión**, no la bandera del objeto: ninguna escritura confirma en ningún nivel, venga del repositorio que abrió la transacción o de otro armado sobre la misma conexión (`repositorio_de` en cada función del ERP). `RepositorioAuditado.transaction()` ya delegaba (`__getattr__`) y su `_registrar` ya llamaba a `_commit()` del repositorio de adentro (ADR-028): con el estado en la conexión no necesitó cambios, y la fila de auditoría de una interior revertida se revierte con ella (probado).

**Por qué savepoints y no un contador.** El contador sin savepoints deja una interior que falla y se atrapa en el peor lugar: en PostgreSQL el error **aborta la transacción entera**, y la exterior que sigue muere con `current transaction is aborted` (en SQLite sigue con lo escrito a medias de la interior). Se midió quitando los tres `execute` de savepoint de la implementación: fallan 8 casos (el del código repetido atrapado, los de tres niveles y el de la auditoría), con exactamente ese error en PostgreSQL. Los savepoints andan **en las dos conexiones reales**: SQLite en archivo (`isolation_level` por defecto del módulo, BEGIN perezoso) y la capa de libracore (`libracore/db/_postgres.py`, `ConnectionWrapper` sobre psycopg, no-autocommit): `Cursor.execute` deja pasar `SAVEPOINT`/`ROLLBACK TO SAVEPOINT`/`RELEASE SAVEPOINT` tal cual (ninguna de sus traducciones los toca) y traduce `psycopg.InFailedSqlTransaction` a `sqlite3.InternalError`, que es justo lo que se vio en el experimento de arriba.

**El detalle de SQLite que un SAVEPOINT a secas hace mal.** El módulo `sqlite3` emite el `BEGIN` perezosamente, antes del primer `INSERT/UPDATE/DELETE`. Un `SAVEPOINT` fuera de una transacción **abre una** y su `RELEASE`, siendo el más externo, **la confirma**: una interior abierta antes de que la exterior escribiera algo habría confirmado todo. Por eso, si la conexión dice `in_transaction is False` (sólo `sqlite3.Connection` lo tiene; el wrapper de PostgreSQL no, y psycopg abre la transacción con la primera sentencia), la interior emite un `BEGIN` antes del `SAVEPOINT`. `test_una_interior_abierta_antes_de_cualquier_escritura_de_la_exterior_tampoco_confirma` falla en SQLite sin esa línea (se quitó para comprobarlo).

**Qué NO cambia.** La API (`transaction()` sigue siendo un context manager que devuelve el repositorio; la firma del puerto `CommerceRepository` no cambia); el comportamiento de una sola capa (un commit al salir, rollback ante cualquier excepción, `_commit()` confirma fuera de `transaction()`); ADR-028/029 intactos (los de `test_update_producto_atomico.py` y `test_codigo_repetido.py` pasan sin tocar, incluidos los de cuatro hilos sobre el mismo producto y los dos productos que se intercambian códigos); el candado del producto (`_bloquear_producto`: `FOR UPDATE` en PostgreSQL, el de escritura en SQLite) se toma y se suelta como antes: lo suelta la transacción exterior, no un savepoint. Se probó con cuatro hilos, cada uno con su `transaction()` exterior propia y un `update_producto` adentro: terminan todos, sin deadlock, y el producto queda con el código y el precio de uno solo. El `test_transaction_no_admite_anidamiento` de `test_inventory_usecases.py` afirmaba justo lo que esta decisión deja de ser verdad: se reescribió como `test_transaction_anida_y_la_exterior_es_duena_del_commit`.

**Consecuencias.** Sin migración; la versión sale del tag. Cierra el borde de ADR-028 («no anida») y el (2) de ADR-029. Un producto puede envolver `update_producto`/`create_producto`/`usecases` en su propia `repo.transaction()` y obtener una única confirmación al final.

**Bordes dichos en voz alta.**
1. ✅ **Cerrado en v0.40.2:** `update_producto` arma el `CatalogItem` (y con él crea la categoría nueva) ADENTRO de su `with`, así que una categoría nueva se va con el savepoint si falla el código. Lo que sigue antes del `with` es sólo `_exigir_minimo_bajo_el_techo` (el candado, que no escribe datos). Texto original: lo que `update_producto` hace **antes** de su `with repo.transaction()` (`_exigir_minimo_bajo_el_techo`: el candado; `_resolver_categoria_id`: una categoría nueva) no está dentro del savepoint de la interior: si el código repetido se atrapa, esa categoría nueva queda en la exterior y se confirma con ella. Es una fila inocua (se reusa en la próxima edición) y no es nueva, pero ya no la deshace un `abrir()` que revierte al fallar.
2. Una excepción atrapada **sin** savepoint (código que usa `conn.execute` por su cuenta fuera de `repo.transaction()`) no se protege: sólo cubre lo que pasa dentro de un `with repo.transaction()`.
3. La profundidad es por conexión y la conexión no se comparte entre hilos (como siempre): dos hilos sobre la **misma** conexión no están soportados, ni antes ni ahora.
4. Un `ROLLBACK TO SAVEPOINT` que falla (conexión caída) sube su propio error, con el original encadenado; la profundidad se restaura de todas formas. No se intenta recuperar una conexión rota.
5. Los `conn.commit()` directos que siguen en `erp/ventas.py`, `web/*_router.py` y `db/auditoria.py` (`init_actividad`) son del llamador o de arranque, no del repositorio: no pasan por `_commit()` y **no respetan** la profundidad. Ninguno está en el camino de `catalogo.update_producto`/`create_producto`.
6. `with repo.transaction() as r:` sobre un `RepositorioAuditado` entrega el repositorio de **adentro** (el que no audita), como antes (`__getattr__` delega y el generador hace `yield self` del delegado). Quien necesite escrituras auditadas dentro del bloque usa el `repo` de afuera, no `r`. No se cambió: es otro defecto, previo, de ADR-028.
7. La suite de cada producto corre completa al subir el pin, y un producto que ya tuviera su propio envoltorio de `transaction()` para sortear el «no anida» puede quitarlo.

## ADR-032 — Anular una venta con factura CON CAE exige la nota de crédito antes, y no acredita dos veces la cuenta corriente (2026-10-04)

**Contexto:** `anular_venta` no miraba la factura ni el CAE (relevamiento del 2026-10-04). Anular una venta con factura autorizada por ARCA dejaba la factura vigente allá —y el cliente la tiene— mientras el stock volvía y la caja se revertía; y si además se emitía la nota de crédito del motor (`POST /api/facturas/{id}/nota-credito`), la cuenta corriente se acreditaba **dos veces** en una venta a cuenta corriente (saldo −total). Ningún test combinaba una venta con una factura vinculada. Decisión del humano: opción A del diseño `nota-de-credito-en-ventas-diseno` del wiki (exigir la nota antes; emitirla sola queda para más adelante).

- Decisión 1 — **`anular_venta` levanta `VentaConFacturaCAE`** si la factura vinculada (`venta_links.factura_id`) tiene CAE (no vacío y distinto de `PENDIENTE`, el criterio del núcleo de notas del motor) y **no tiene una nota de crédito con CAE** (`facturas.cbte_asoc_*` apunta a ella). El chequeo va **antes** de tocar stock, caja o estado, igual que `VentaConDevoluciones`. Una factura sin CAE se anula como siempre; anular una venta ya anulada sigue devolviendo `False`.
- Decisión 2 — **con la nota emitida se anula, y la cuenta corriente no se acredita otra vez** si `libracore.notas_de_credito.cc_acreditada_por_nota(conn, factura_id)` (ADR-016 de `libracore`). La regla de «quién acredita» es del motor; acá sólo se la consulta. La caja se revierte igual: la nota no la toca.
- Decisión 3 — `POST /api/ventas/{id}/anular` contesta **409** con texto (el id de la factura va en el mensaje), como los demás 409; y `obtener_venta` trae `factura_cae` para que la pantalla avise antes de anular (el aviso en `libra-ui` es un cambio aparte).
- Lo que **no** resuelve: emitir la nota desde la anulación (opción B); una venta anulada *antes* de este cambio con factura CAE a la que después se le emita la nota (acreditaría dos veces; no se midió que exista); la nota parcial; el aviso en la UI.
- Consecuencias: el extra `erp` pide `libracore>=1.128`. **Cambia el comportamiento de anular** en Contalibra, Restolibra y VentaLibra cuando la venta tiene factura con CAE (hoy sólo Contalibra tiene instancia real). VentaLibra además necesita montar el router de comprobantes para poder emitir la nota (cambio de producto).


## ADR-033 — La revisión 0005 no supone que el arranque de la app ya creó `branches` (2026-10-04)

**Contexto:** al probar el salto de Contalibra (`libracommerce` v0.23.0 → v0.41.0) sobre una **copia real de Compulibra**, `libracommerce-migrar upgrade` moría en `0005_min_stock_por_sucursal` con `relation "branches" does not exist` y abortaba el deploy entero. `branches` (las sucursales) llegó después de la baseline: la crea `init_schema()` —el **arranque** de la app—, no la cadena, y el deploy migra *antes* de arrancar. Una base cuya baseline (`0001`) ya estaba registrada nunca volvió a pasar por `init_schema()`. Los tests no lo veían: sólo cubrían una base viva *sin versión* con el schema ya al día, y una base vacía. VentaLibra no lo sufrió porque tiene su propia revisión que crea `branches`.

- Decisión 1 — **`0005` llama a `init_schema()` antes de crear la tabla.** Es idempotente (todo `CREATE ... IF NOT EXISTS`) y es lo mismo que hacen la baseline y el arranque, así que no cambia nada de lo que ya esté.
- Decisión 2 — nuevo test sobre los dos motores: una base en la baseline vieja (el schema sin `branches`, estampada en `0001`) llega al `head`.
- Lo que **no** cambia: las revisiones `0002` a `0004` (agregan columnas por introspección y pasaron sobre la copia real).
- Regla que queda: **una revisión no puede suponer que el arranque de la app ya corrió.** Medido sobre la base de un cliente real, no sobre una sintética.


## ADR-034 — El detalle de la venta trae `nota_credito_display`: la pantalla sabe, al reabrir la venta, que la factura ya tiene su nota (2026-10-04)

**Hallazgo (verificación en Chromium de VentaLibra con libra-ui 0.113.x).** Emitir la nota de crédito desde el detalle de la venta sólo dejaba una marca en el **estado local** de la pantalla: al recargar o abrir la venta en otra pestaña, con la nota ya emitida en el servidor, el aviso volvía a decir «hay que emitir antes la nota de crédito» y el botón reaparecía; al tocarlo, el motor contestaba 409 («una factura se acredita una sola vez»). El detalle (`obtener_venta`) traía `factura_cae` pero nada de la nota.

**Decisión (arreglo de fondo en el motor).** `obtener_venta` agrega `nota_credito_display`: el comprobante de la nota de crédito **con CAE** de la factura de la venta, como lo lee una persona (`NOTA CREDITO C 0005-00000001`, mismo `factura_display`), o `None` si no hay factura, la factura no tiene CAE o no tiene nota. Una nota sin CAE (`PENDIENTE`) no cuenta, el mismo criterio con el que `anular_venta` exige la nota (ADR-032): `_nota_con_cae_de` pasa a apoyarse en `_nota_de_credito_con_cae_de`, que devuelve la nota. Campo aditivo: ningún consumidor existente cambia. Sin migración.

**Límites.** Es la nota **total** de la factura (la única que el motor emite hoy). Si hubiera más de una nota con CAE para la misma factura se informa la primera por `id`.

## ADR-035 — Anular una venta exige que las notas de crédito SUMEN el total de la factura (2026-10-04)

**Contexto:** `libracore` v1.130.0 (ADR-018) permite acreditar una factura en varias notas parciales. El ADR-032 pedía *una* nota con CAE para anular: con parciales eso dejaba anular una venta cuya factura seguía vigente casi entera.

- Decisión 1 — `anular_venta` levanta `VentaConFacturaCAE` si **lo que acreditan las notas con CAE** de la factura **no llega a su total**. La cuenta es del motor (`libracore.notas_de_credito.saldo_acreditable`), no se reescribe acá. Una nota sin CAE no suma. El mensaje dice cuánto falta cuando ya hay notas.
- Decisión 2 — con la factura acreditada por completo se anula como siempre, y la cuenta corriente no se acredita otra vez si las notas ya la abonaron (`cc_acreditada_por_nota`, que ahora reconoce el abono de cada nota).
- Sin cambios: venta sin factura, factura sin CAE, `nota_credito_display` (ADR-034) y la venta ya anulada (no-op).
- El extra `erp` pide `libracore>=1.130`.


## ADR-036 — El detalle de la venta trae `factura_total` y `factura_saldo_acreditable` (2026-10-05)

**Contexto:** con las notas parciales del motor (libracore ADR-018) una factura se acredita en varias notas. `nota_credito_display` (ADR-034) dice que *existe* una nota con CAE, y la pantalla lo usaba para dejar de ofrecer la nota: con parciales eso la escondía con la factura casi entera sin acreditar. La pantalla necesita saber **cuánto queda**, para seguir ofreciendo la nota, sugerir el importe y decir cuándo ya se puede anular la venta.

- Decisión 1 — `obtener_venta` trae `factura_total` y `factura_saldo_acreditable` (`float`, como el resto de los importes del detalle). El saldo lo calcula el motor (`saldo_acreditable`, ADR-035): las notas **con CAE** suman y una sin CAE no cuenta.
- Decisión 2 — ambos son `None` si no hay factura o la factura no tiene CAE: ahí no hay nada que acreditar. `nota_credito_display` no cambia (sigue siendo la primera nota con CAE).
- Sin cambios de esquema ni de rutas.

## ADR-037 — La reposición se puede pedir ordenada; el CSV sale en el orden de la tabla (2026-10-05)

**Contexto:** la pantalla de reposición de `libra-ui` ordena la lista en el navegador (una columna y un sentido), pero el CSV lo arma el motor y salía siempre en el orden de urgencia (menor cobertura primero): quien ordenaba por nombre o por sugerido y exportaba recibía otro orden. El margen ya resolvía lo mismo con `orden` y `sentido` (`erp.margen.ORDENES`/`SENTIDOS`); el CSV no se arma en el kit porque ahí viven la neutralización de fórmulas (`csv_seguro`) y los booleanos legibles.

- Decisión 1 — `GET /api/reportes/reposicion` y `/export` aceptan `orden` (una de `erp.reposicion.ORDENES`: las columnas de la pantalla) y `sentido` (`asc` por default, o `desc`). Sin `orden`, nada cambia: el de urgencia. Uno desconocido es 422.
- Decisión 2 — `erp.reposicion.ordenar_sugerencia` es una función pura sobre las filas. Los textos se comparan sin mayúsculas ni acentos (como `localeCompare(..., 'es', {sensitivity: 'base'})` de la pantalla); `motivo` se ordena como se lee («Bajo el mínimo», «Bajo el mínimo y por rotación», «Por rotación»), no por el código; lo que no tiene valor va siempre al final, sea cual sea el sentido; un empate conserva el orden de urgencia.
- Decisión 3 — la lista y el CSV comparten el orden: es la misma función en el mismo `_reporte`, así que no pueden divergir.
- Límite: si la pantalla suma una columna ordenable, hay que sumarla a `ORDENES`; mientras no esté, la pantalla no puede pedirle al motor ese orden para el CSV.

## ADR-038 — Quien sólo ve sus turnos ve sólo las ventas de los turnos de caja que abrió (2026-10-07)

**Contexto:** VentaLibra tiene roles (su ADR-049) y el cajero y el vendedor leían el historial de ventas completo: `GET /api/ventas` y `GET /api/ventas/{id}` no filtraban por usuario ni por turno, y lo mismo pasaba con anular y devolver (que el cajero puede hacer, decisión de su humano del 2026-09-15) sobre cualquier venta del local. Lo de «quién ve qué venta» es de fondo: va en el motor y no en el producto.

- Decisión 1 — `OpcionesVentas.solo_sus_turnos: Callable[[dict], bool] | None` (default `None`). Recibe el usuario de la sesión y dice si se le limitan las ventas. Para ése, `GET ""` lista sólo las ventas ligadas (`venta_links.turno_id`) a un turno de `turnos_caja` que abrió, y `GET /{vid}`, `POST /{vid}/anular` y `POST /{vid}/devolver` de una venta ajena dan **404 «Venta no encontrada»**, igual que una que no existe: quien está limitado no distingue una de otra.
- Decisión 2 — **«sus turnos», no «su turno abierto»** (decisión del humano, 2026-10-07): una venta de un turno ya cerrado sigue siendo de quien la cobró, que la puede devolver al día siguiente. Una venta sin turno no es de nadie: no la ve quien está limitado.
- Decisión 3 — `build_guarda_de_venta(solo_sus_turnos=..., parametro="vid")` es la misma regla como dependencia de FastAPI, para colgarla de los routers de otros paquetes que reciben un id de venta en el path (el cobro y la factura de `libracore.ventas_cobro_router`, el ticket del producto). Una ruta sin el parámetro pasa sin consultar; uno no numérico lo rechaza la validación de la ruta (422), como sin la guarda.
- Decisión 4 — el filtro vive en el dominio: `erp.ventas.listar_ventas(..., de_turnos_de=usuario_id)` y `erp.ventas.es_de_sus_turnos(conn, venta_id, usuario_id)`.
- Sin la opción, nada cambia: las rutas, las dependencias y las respuestas son las de siempre, y el listado ni resuelve la sesión (Contalibra y Restolibra no la prenden). Sin cambios de esquema.
- Límite: el criterio es el turno de caja de LibraCore (`turnos_caja.usuario_id`); un producto que venda sin turnos y prenda la opción deja a sus usuarios limitados sin ninguna venta visible.

## ADR-039 — Los rangos de fecha de las ventas son por día completo, con la función del motor (2026-10-09)

**Contexto.** `sales.occurred_on` es texto libre y `POST /api/ventas` acepta `fecha` con hora. `listar_ventas` y `erp.reportes._rango`
filtraban `occurred_on <= hasta`, así que una venta con hora del último día del rango quedaba afuera del listado y de los reportes de
ventas, medios de pago, productos y resumen. El margen ya lo evitaba con su propio `_filtro_de_ventas`. El mismo defecto estaba en
quince filtros de libracore (y su tesorería lo parcheaba con `23:59:59`, que fallaba con la `T`).

**Decisión.** El criterio vive en el motor: `libracore.fechas.rango_por_dia` (libracore v1.148.0, ADR-037 del motor): `desde` por su día
y `hasta` como cota exclusiva del día siguiente. `listar_ventas`, `_rango` y `margen._filtro_de_ventas` la usan; se borró el `_dia`
propio del margen. El piso de `libracore` sube a `>=1.148`. Para una fecha sin hora, el rango es el de siempre.

**Tests.** `test_erp_ventas.py`: ventas del día con y sin hora, con espacio y con `T`, y los vecinos del día anterior y siguiente, en el
listado y en `reporte_ventas` (falla sin el arreglo); y un rango de varios días sin hora que no cambia.

## ADR-040 — La devolución reintegra lo efectivamente pagado: el descuento se prorratea (2026-10-09)

**Contexto.** `erp.ventas.devolver_items` reintegraba `cantidad * unit_price` y no miraba ningún descuento: ni el de la venta
(`sales.discount_total`, donde caen el descuento manual y el ahorro de las promociones) ni el de la línea (`sale_items.discount_amount`).
Medido en la demo real de VentaLibra (POS-000008): 2 × Lavandina a $1.600 con promoción 2x1 (subtotal 3.200, descuento 1.600, cobrado
1.600); se devolvió 1 unidad y el sistema reintegró $1.600, todo lo cobrado, y el cliente se quedó con la otra gratis. Pasa igual con
cualquier descuento general. Todos los tests de devolución usaban `descuento=0`.

**Decisión (del humano, 2026-10-09): reintegro PRORRATEADO.** Se devuelve lo efectivamente pagado por lo que vuelve; el descuento se
reparte en proporción. En el caso medido, $800 por unidad.
- La semántica de los totales es la de `erp.margen` (ADR-015): `crear_venta` no escribe `discount_amount` ni `tax_total`, y el dominio puede
  dejar el descuento en la línea Y en `discount_total` (el mismo dinero). Neto de línea = `quantity*unit_price - discount_amount`; del descuento de
  la venta sólo cuenta lo que las líneas no explican (`max(discount_total - Σ discount_amount, 0)`, con tope en lo que vale la venta); lo
  cobrable = `Σ netos - ese descuento`. Cada unidad vale `neto de su (ítem, variante) / unidades` por `cobrable / Σ netos`. Las líneas de
  servicio entran en la suma —cargan descuento— y nunca se devuelven.
- **Exactitud acumulada.** El importe es una diferencia de acumulados, `redondeo(prorrateo(ya devuelto + esto)) - redondeo(prorrateo(ya
  devuelto))`, con `Decimal` y redondeo al centavo hacia arriba en el empate. Redondear cada devolución por separado dejaba un centavo de más
  (3 × $100 con $10 de descuento: 96,67 × 3 = 290,01); así dan 96,67 + 96,66 + 96,67 = 290,00. Con la venta devuelta entera el total es
  exactamente lo cobrable.
- **Nunca más de lo cobrado.** Si el acumulado superara el cobrable, `_reintegro_prorrateado` levanta `ValueError` (defensa: el tope por
  cantidad de `devolver_items` ya lo frena antes).
- Sin descuentos el factor es 1 y el importe es `cantidad * unit_price`, como siempre. La función devuelve el mismo `float` y la misma forma.

**Tests.** `test_erp_ventas.py` (sección ADR-040): 2x1 (800 + 800 = 1.600), descuento general del 10% sobre dos líneas (900 y 450), devolución
total en una llamada, centavos que no dividen exacto (96,67 / 96,66 / 96,67), sin descuento (idéntico), descuento de línea y de línea más
general, línea de servicio en el factor, y la guarda. Fallan con la cuenta vieja.

**No cubre.**
- **Reintegros hechos antes de este cambio**: pagaron de más y no se corrigen. Una venta ya devuelta en parte con el cálculo viejo seguirá
  pagando su resto según este (la suma puede pasar lo cobrado: la guarda mira lo prorrateado, no lo que ya salió de la caja).
- **IVA**: `tax_total`/`tax_amount` no entran (0 en el mostrador).
- **Dos líneas del mismo (ítem, variante) a precios distintos**: el ledger no dice de qué línea volvió lo ya devuelto, así que las unidades
  se valúan al promedio de la clave (con el mismo precio, lo de siempre).
- **La nota de crédito** de una factura ya emitida: sigue siendo un acto aparte (ADR-032/035); esto sólo cambia el importe de caja o cuenta
  corriente.

**La referencia de caja de cada devolución lleva lo ya devuelto (mismo día, mismo PR).** `_referencia_devolucion` armaba `devolucion:venta:{id}:{línea}x{cantidad}` y `create_caja_movimiento` no repite una referencia: dos devoluciones sucesivas iguales (1 y después la otra de un 2x1) reponían el stock las dos veces pero escribían un solo egreso, y el arqueo no veía la plata que salió. Ahora termina en `@{ya_devuelto}`: cada devolución sucesiva es otra referencia, coherente con el stock. Test: `test_dos_devoluciones_sucesivas_iguales_escriben_los_dos_egresos_de_caja` (falla sin el cambio: caja 800 en vez de 1600). Queda pendiente una clave de operación del cliente para distinguir un reintento de doble clic de una segunda devolución legítima (hoy, como antes, ese reintento repone stock de nuevo si hay disponible).

## ADR-041 — La devolución acepta una `clave_operacion` para que un reintento no duplique stock ni caja (2026-10-09)

**Problema.** `erp.ventas.devolver_items` no distingue un REINTENTO (doble clic, timeout y el usuario vuelve a apretar) de una segunda
devolución legítima: los dos piden lo mismo. Si queda cantidad disponible, el reintento repone el stock otra vez y escribe otro egreso de
caja. Cerrado el hueco de la referencia de caja (ADR-040: lleva lo ya devuelto) quedaba justo esto, que quedó anotado como pendiente.

**Patrón reutilizado.** El de `vencimientos` y `reposicion_ordenes`: una `clave_operacion` por intento, validada, estampada como `[op:<clave>]`
en la nota o referencia de lo que se escribe y buscada antes de escribir; con los mismos datos devuelve el resultado anterior con
`repetida: true`, con otros datos `ClaveReusada` (un `ValueError`; el router, 409). Lo común vive ahora en `erp/claves.py` (`normalizar_clave`,
`marca`, `ClaveReusada`, `MAX_LARGO_CLAVE`) y las tres operaciones lo usan. La validación de `reposicion_ordenes` sigue siendo la suya (letras,
números y `. _ : -`, más estricta que la de `vencimientos`): unificarla cambiaría qué claves acepta una de las dos.

**Decisión.**
- `devolver_items(..., clave_operacion: str | None = None)` y `DevolucionPayload.clave_operacion`: **opcionales en el motor** (Restolibra y quien no la
  mande siguen igual: sin clave nada cambia, salvo que el resultado trae `repetida: False`). **La UI de VentaLibra la manda siempre**, una por intento.
- **Fuente de «ya se aplicó»: las filas de stock `devolucion` de la venta con la marca al final de la nota.** Se escriben siempre y en la misma
  transacción; la caja no (un reintegro a cuenta corriente va a `cc_pagos`, y ahí `reintegrar_devolucion` no es idempotente). Se comparan las
  cantidades por (ítem, variante, depósito) —el ledger no dice de qué línea volvió lo devuelto, y dos líneas del mismo producto son lo mismo que una
  con la suma— y el medio de pago, que se lee del reintegro. El `importe` que se devuelve es el que se reintegró (leído de `caja_movimientos` o
  `cc_pagos` por la referencia), no uno recalculado.
- Con clave, la referencia de caja es `devolucion:venta:{vid}:op:{clave}` (reemplaza a `…@{ya_devuelto}`): `create_caja_movimiento` también frena el egreso repetido.
- La búsqueda va **antes** de mirar el estado de la venta: el reintento de la última devolución llega con la venta ya `devuelta`.
- **Concurrencia:** con clave, antes de buscarla se toma la venta con un `UPDATE` de sí misma (fila en PostgreSQL, base en SQLite). Dos pedidos
  con la misma clave se serializan y el segundo ve la marca del primero. No se usa el bloqueo por producto de `vencimientos` porque un producto sin
  marcar no toma ninguno; el de la venta se toma antes que el de los productos y `anular_venta` no toma ninguno.
- La respuesta de `POST /api/ventas/{vid}/devolver` es la venta de siempre más `repetida`.

**Tests.** `test_erp_ventas.py`: reintento con la misma clave (no duplica stock ni caja, mismo importe, `repetida`), reintento de la última devolución con la
venta ya `devuelta`, misma clave con otras cantidades / otro depósito / otro medio (`ClaveReusada`), dos claves = dos devoluciones con el prorrateo de
ADR-040, la clave es por venta, sin clave igual que antes (incluidas las referencias `@n`), cuenta corriente, clave inválida, dos hilos con la misma clave.
`test_fefo_devolucion_transferencia_ajuste.py`: el par por lote con clave. `test_web_ventas.py`: 200 con `repetida`, 409 y 422. Los del reintento fallan si
no se busca la clave y el de los hilos falla sin el bloqueo de la venta. La carrera en PostgreSQL no se probó acá (sin base de test); en SQLite la serializa la base. *(Actualización 2026-10-13: el test de los dos hilos ahora corre contra SQLite y PostgreSQL —usa `abrir_ventas`— y falla en los dos, 3 de 3 veces, si se saca el `UPDATE` de la venta.)*

**No cubre.**
- **Un reintento sin clave** (un cliente que no la manda) sigue siendo indistinguible de una segunda devolución.
- **Una clave reusada a propósito** para otra devolución con los mismos datos de la misma venta se toma por reintento: la clave es por intento, y es del cliente generar una nueva.
- **Devoluciones hechas antes** de este cambio no llevan marca: no se reconocen como aplicadas.
- Sigue sin cubrir la receta (ADR-018) ni la nota de crédito de una factura emitida.

## ADR-042 — La reposición toma «hoy» de Argentina, y sus routers lo reciben de un `reloj` (2026-10-10)

**Problema.** `sugerencia_reposicion` y `generar_ordenes_borrador` caían en `datetime.date.today()` sin `hoy`, y los dos routers nunca lo pasaban:
el «hoy» era la fecha del sistema. Los contenedores desplegados tienen `TZ=America/Argentina/Buenos_Aires` (medido el 2026-10-10 en las tres
demos), así que hoy no se corre el día. Pero un servidor o un CI en UTC sí lo corre entre las 21 y las 24, y la ventana de rotación se mueve un
día. Además, los tests que pasan por el router tenían que fijar el reloj con un `monkeypatch` sobre las funciones del motor (libracommerce#201).

**Patrón reutilizado.** `erp.vencimientos.hoy_argentina()`, que ya usaban `vencimientos` y los avisos FEFO de la venta.

**Decisión.**
- Sin `hoy`, `sugerencia_reposicion` y `generar_ordenes_borrador` usan `hoy_argentina()`.
- `build_reposicion_router` y `build_reposicion_ordenes_router` aceptan `reloj: Callable[[], date] = hoy_argentina`. Cada pedido lo llama **una
  vez**: en el export, la lista y el nombre del CSV salen del mismo día. Los productos no cambian nada.
- Los tests pasan `reloj=lambda: HOY` al router, y se borró `fijar_hoy_del_router`.

**Tests.** `test_reposicion.py`: con el reloj en `HOY` el router da lo mismo que el motor con `hoy=HOY` y el CSV se llama `reposicion_2026-09-30.csv`.
Con el reloj 60 días después, las ventas salen de la ventana. Sin `hoy`, el motor usa `hoy_argentina` y el reloj por default de los dos routers es
esa función. Los dos fallan con el código anterior. La suite de reposición pasa con el reloj del proceso adelantado 60 y 400 días.

**No cubre.** Quedan cinco `date.today()` fuera de la reposición, con el mismo riesgo en un servidor en UTC: la fecha por default de un ajuste de stock (`erp/stock.py`, `erp/catalogo.py` y `web/catalogo_router.py`) y el período por default del margen (`web/margen_router.py`).

## ADR-043 — `usecases.sales.return_sale_items` reintegra lo efectivamente pagado, con la misma cuenta que `devolver_items` (2026-10-10)

**Problema.** ADR-040 hizo que `erp.ventas.devolver_items` reintegrara lo efectivamente pagado (descuento prorrateado), pero la otra devolución
del motor, `usecases.sales.return_sale_items` (API pública sobre el dominio `Sale`), seguía haciendo `importe += cantidad * linea.unit_price`:
ni `Sale.discount_total` ni `SaleItem.discount_amount`. Con un 2x1 de 2 × $1.600 devolvía $1.600 por una unidad. Ningún producto la llama en
producción (sólo tests), pero es el contrato del motor y tenía que dar lo mismo que el camino nuevo.

**Patrón reutilizado.** El prorrateo de ADR-040, sin duplicarlo: la cuenta salió de `erp.ventas._reintegro_prorrateado` (que leía filas SQL) a una
función pura del dominio, `domain.sales.reintegro_prorrateado(lineas, descuento_venta, ya_devuelto, pedido)` con `LineaReintegro(clave, quantity,
unit_price, discount_amount)`. `_reintegro_prorrateado` queda como adaptador (filas → `Decimal` por el texto) con la misma firma y el mismo
resultado; `return_sale_items` es el otro llamador. Mismas reglas: neto de línea, descuento de la venta sólo por lo que las líneas no explican,
diferencia de acumulados redondeada al centavo, servicios dentro de la suma pero nunca devueltos, guarda contra reintegrar de más (`ValueError`).

**Decisión.**
- `return_sale_items` devuelve el importe prorrateado (`Decimal` al centavo). Sin descuentos da `cantidad * unit_price`, como siempre.
- **La clave de valuación es la posición de la línea**, no `(ítem, variante)`: el ledger de acá guarda de qué línea volvió cada unidad
  (`reason_code`), así que dos líneas del mismo producto a distinto precio se valúan cada una por lo suyo. Con una línea por producto, o a igual
  precio, da exactamente lo mismo que `devolver_items`. (El camino del ERP promedia porque su ledger no lo dice; ver «No cubre» de ADR-040.)
- **Se valida todo y se calcula el importe antes de escribir en el ledger.** Antes validaba y escribía línea por línea: un pedido con una segunda
  línea inválida dejaba repuesta la primera; y la guarda del prorrateo, al levantar después de escribir, habría dejado stock repuesto sin reintegro.

**Tests.** `test_usecases.py`: 2x1 (800 + 800), descuento de la línea, descuento general repartido entre dos líneas, descuento en la línea y en el
total (el mismo dinero), centavos que no dividen exacto (96,67 / 96,66 / 96,67 = 290,00), parcial más resto que suma lo cobrado, servicio dentro del
prorrateo, sin descuentos (idéntico), dos líneas del mismo producto a distinto precio, coincidencia con la cuenta de `devolver_items` y pedido inválido
sin stock a medias. Diez fallan con el código anterior (los otros dos son de control: sin descuento y precios distintos sin descuento).

**No cubre.**
- **IVA**: `tax_amount`/`tax_total` no entran, como en ADR-040.
- **Reintegros hechos antes** con el cálculo viejo: pagaron de más y no se corrigen.
- **La caja**: este caso de uso sigue sin mover plata; sólo calcula cuánto (el dinero es del contexto de LibraCore).
- **Ventas con `discount_total` mayor que lo que valen las líneas**: se topa en lo que vale la venta, como en ADR-040.

## ADR-044 — «Hoy» es el día de Argentina en todo el motor: ajustes, transferencias y margen (2026-10-10)

**Problema.** Después de ADR-042 (reposición) quedaban cinco `date.today()` en el motor, que tomaban la fecha del sistema:
- la fecha por default de un movimiento de stock (`erp/stock.add_movimiento_stock`);
- la de una transferencia (`erp/catalogo.transferir_stock`);
- la de un ajuste por el router de stock (`web/catalogo_router`);
- y el período por default del reporte de margen (`web/margen_router._fechas_default`, dos usos).

Los contenedores desplegados tienen la zona de Argentina, así que hoy no fallan. Un servidor o un CI en UTC, en cambio, fecha un ajuste hecho
a las 22 con el día siguiente, y el margen arranca el mes nuevo tres horas antes.

**Patrón reutilizado.** `vencimientos.hoy_argentina()`, con el import diferido de `lotes._hoy`: `vencimientos` importa `stock` y `catalogo`,
así que un import de arriba en esos dos módulos sería un ciclo. Los routers ya podían importar `vencimientos`.

**Decisión.** Los cinco usan `hoy_argentina()`. Fijar `vencimientos.hoy_argentina` en un test fija «hoy» en todo el motor.

**Tests.** `tests/test_hoy_argentina.py`: con `hoy_argentina` fijado en 2024-03-15, el ajuste por HTTP, el movimiento y la transferencia sin
fecha quedan con ese día, y el margen sin fechas va del 2024-03-01 al 2024-03-15, en la función y en el router. Los cuatro fallan con el código anterior.

**No cubre.** Los `datetime.now()` que estampan un instante, con hora (auditoría, `repository.save`, vigencia de listas y promociones), siguen
con la hora del sistema. Con hora no se corre «el día», sino que cambia la zona en que se guarda el instante, y eso es otra decisión.

## ADR-045 — Las variantes reciben las promociones de su producto y cuentan juntas (2026-10-11)

**Problema.** VentaLibra tenía anotado desde el 2026-09-29 que «la ruta de variantes del POS sigue al precio plano, sin promoción». Se midió
antes de tocar nada, y la mitad del título era falsa en el motor: ADR-014 ya aplica «llevá N pagá M» y combos a las líneas con variante. Una
venta con dos talles de la misma remera y un 2x1 sobre la remera descuenta una unidad hoy, en SQLite y en PostgreSQL, sin cambiar una línea del
motor. Lo que sí estaba mal era del lado del POS: la variante entraba al precio plano y no al de la lista predeterminada (ADR-042 de VentaLibra).
Ese arreglo vive en el producto (su `Pos.tsx`); acá queda fijada la regla del motor para que no se pierda.

**Por qué ya andaba.** Las promociones se definen sobre **productos** (`promotion_items.item_id` es `catalog_items.id`), y la variante
(`item_variants`) no tiene precio ni categoría propios: `item_prices` no tiene columna de variante y `catalog_items` guarda la categoría.
`erp.promociones.calcular` agrupa las líneas por `producto_id` y la `variante_id` no interviene.

**Decisión.** Se deja como está y se documenta:
- **Promoción por producto:** alcanza a **todas** sus variantes. No existe «sólo el talle L»: el ítem de la promoción es un producto, y una
  variante no se puede nombrar en ella.
- **Promoción por categoría:** **no existe en el modelo** (`promotions` son `nxm` y `combo` sobre productos, y no hay descuentos por
  porcentaje). Si se agrega, la categoría se lee **del producto** (`catalog_items.category_id`) y la variante la hereda: nunca se le da categoría
  a la variante. Queda dicho acá para que quien la construya no cree una segunda fuente de verdad.
- **2x1 (o N×M) mezclando variantes del mismo producto: cuentan juntas.** Es lo que espera un comercio (dos remeras, la más barata o no, son
  «dos remeras»; el cliente no tiene por qué llevar dos del mismo talle). Como la variante no tiene precio propio, no hay «la más barata»:
  todas valen el precio del producto, y si las líneas difieren (un quiebre por cantidad en una, una etiqueta de balanza en otra) rige el
  promedio ponderado de siempre.
- **Combos:** un producto con variantes cuenta una vez por cada unidad de cualquiera de sus variantes.
- **Las líneas no se tocan.** Cada variante sigue siendo su línea, a su precio de lista, con su `variante_id`: el stock, el costo y las
  devoluciones (que ya prorratean por `(ítem, variante)`) siguen siendo por variante. El ahorro viaja en el `descuento` de la venta, como en ADR-014.
- `POST /api/promociones/calcular` no recibe `variante_id` (Pydantic lo descarta si llega): el cálculo no lo necesita y que lo ignore es parte de
  la regla.

**Tests.** `tests/test_promociones_variantes.py`, en los dos motores: 2x1 con dos variantes del mismo producto, con una sola variante y dos
unidades, 3x2 con tres variantes y una suelta, la variante de otro producto que no suma, un combo con un producto con variantes y otro sin, la
venta completa (descuento, `sale_promotions`, líneas intactas con su variante) y que el servidor cobre lo mismo que mostró el cálculo. Son de
**caracterización**: pasan con el código de antes. Si `calcular` agrupara por `(producto_id, variante_id)`, los dos de la venta se ponen rojos
(medido).

**No cubre.**
- **Quiebres por cantidad:** el precio por cantidad de la lista (ADR-042 de VentaLibra) sigue resolviéndose **por línea**, no por producto. Dos
  talles en dos líneas de a una unidad no suman para un quiebre de 2; un producto sin variantes escaneado dos veces tampoco. Es el comportamiento
  de siempre y cambiarlo toca los productos sin variantes. **Resuelto en ADR-048.**
- **Promociones por variante, por categoría o por porcentaje:** no existen.

## ADR-046 — «Recibir mercadería» es idempotente: `POST /api/purchase-receipts/recibir` con `clave_operacion` (revisión 0006, 2026-10-11)

**Problema** (anotado el 2026-10-01 en los pendientes de VentaLibra). Recibir mercadería eran tres llamadas con tres commits —`POST /purchase-receipts`, una `POST …/items` por línea y `POST …/confirm`— y ninguna llevaba una identidad de operación. Si el motor
guardaba la recepción pero la respuesta se perdía y la persona reintentaba, el cliente volvía a empezar desde la primera: otra recepción y el stock entraba dos veces (con lote o sin lote). Además, `confirm_purchase_receipt` hace cada escritura con su commit, así que un fallo entre
la recepción confirmada y sus movimientos dejaba una recepción confirmada sin mercadería.

**Patrón reutilizado.** El de ADR-041 (`devolver_items`), `vencimientos` y `reposicion_ordenes`: una `clave_operacion` por intento (`erp/claves.py`: `normalizar_clave`, `ClaveReusada`), una búsqueda antes de escribir, el mismo resultado con `repetida: true` si los datos son los mismos y
`ClaveReusada` (409) si son otros. Lo propio de esta operación es **dónde vive la clave**.

**Decisión.**
- **Una ruta nueva, `POST /api/purchase-receipts/recibir`** (`erp.compras.recibir_mercaderia`): recibe la recepción entera —`proveedor_id`, `purchase_order_id?`, `document_reference?`, `deposito_id`, `items` (`item_id`, `quantity`, `unit_cost`, `lot_code?`, `expires_at?`) y `clave_operacion`
  (obligatoria *acá*: es su razón de ser)— y la crea, la carga y la confirma **en una transacción** (`repo.transaction()`): o queda todo o no queda nada. La respuesta es la recepción de siempre más `repetida`. La respuesta de la primera vez se vuelve a leer de la base, para que sea
  idéntica letra por letra a la de un reintento (PostgreSQL devuelve `8.0` para lo que se mandó como `8`).
- **Las tres rutas de siempre no cambian**: no llevan clave, siguen andando como hoy y, sin clave, siguen sin poder distinguir un reintento de otra recepción (lo mismo que ADR-041 dice de la devolución). Un cliente viejo sigue andando.
- **La clave vive en `purchase_receipts.clave_operacion`** (revisión **`0006_clave_operacion_recepciones`**, aditiva, opcional, sin tocar una fila existente), con un **índice único parcial** (`WHERE clave_operacion IS NOT NULL`: las recepciones sin clave no compiten). No se estampa en una nota
  como `[op:<clave>]` (lo que hacen vencimientos y devolución) porque acá no hay una nota en la recepción y buscarla en `stock_movements.note` sería recorrer el ledger entero sin índice. La columna no está en `init_schema()` (el gate `test_schema_congelado` sigue verde): es de la
  revisión, como `supplier_party_id` o `item_branch_min_stock`; la revisión llama a `init_schema()` antes, por la lección de la 0005.
- **La unicidad es global, no por producto**: una recepción es una sola fila, no una por producto. La misma clave con otro proveedor, orden, remito, depósito o líneas es `ClaveReusada` (409). «Los mismos datos» se compara contra lo escrito: proveedor, orden, remito, depósito (de los
  movimientos de stock de la recepción) y las líneas en orden, con las cantidades y los costos como `Decimal` (`8` y `8.0` son lo mismo).
- **Concurrencia.** Con la clave, antes de buscarla se toma un candado de ESA clave hasta el fin de la transacción: `pg_advisory_xact_lock` en PostgreSQL (como `reposicion_ordenes`) y, en SQLite, un `UPDATE` que no toca ninguna fila (toma el candado de escritura). Dos pedidos con la misma
  clave se hacen cola y el segundo ve la recepción del primero. Detrás, el índice único: si el candado fallara, la base rechaza la segunda en vez de duplicar.
- **Sin la revisión** la ruta contesta 503 con el texto de `libracommerce-migrar upgrade` (como vencimientos); las tres rutas de siempre no la necesitan. Se sondea por metadatos (`PRAGMA table_info`), no con un `SELECT` (en PostgreSQL uno fallido aborta la transacción).
- **Validación** antes de escribir: clave bien formada, al menos una línea, cantidad > 0 y costo ≥ 0 (la ruta de siempre acepta cualquier cantidad: no se tocó), depósito y productos existentes (`ValueError`, 422), orden inexistente (404). La búsqueda de la clave va **antes** de los chequeos que dependen de la
  base: un reintento no puede fallar por algo que ya no hace falta.

**Tests.** `tests/test_recibir_mercaderia.py`, contra SQLite y PostgreSQL: el mismo intento dos veces cuenta una recepción y el stock una vez (y la respuesta es la misma); el reintento no vuelve a sumar a la orden ni a pisar el costo; con lote y vencimiento no duplica el lote; claves distintas son dos
recepciones; la misma clave con otro proveedor / orden / remito / depósito / producto / cantidad / costo / lote / más líneas es 409 sin escribir; una línea inválida o un fallo a mitad no dejan nada (y la clave no se gasta); los pedidos inválidos; sin clave las rutas de siempre son las de siempre
y no se deduplican; dos pedidos simultáneos con la misma clave cuentan una vez; sin la revisión, 503; la revisión 0006 sobre datos previos, idempotente por introspección, con su downgrade, y el índice único (con NULL repetidos permitidos). Fallan si no se busca la clave, si no se toma el candado
(el simultáneo) o si no hay transacción (el fallo a mitad).

**No cubre.**
- **Un reintento sin clave** (las tres rutas de siempre) sigue siendo indistinguible de otra recepción.
- **Una clave reusada a propósito** para otra recepción con exactamente los mismos datos se toma por reintento: la clave es por intento y es del cliente generar una nueva.
- **Las recepciones hechas antes** no llevan clave: no se reconocen como aplicadas.
- Las rutas de siempre siguen sin transacción única: `confirm_purchase_receipt` commitea cada escritura. No se tocó (cambiaría qué queda ante un fallo a mitad para quien no manda clave).
- No valida que lo recibido no pase de lo pendiente de la orden (la ruta de siempre tampoco).

**Cómo se adopta.** Subir el pin del motor, correr `libracommerce-migrar upgrade --prefijo <producto>` (la 0006 es opt-in: una instancia sin ella sigue andando con las tres rutas) y subir la pantalla del kit (`libra-ui` ADR-049, que usa la ruta nueva). El producto no cambia código si ya monta
`build_compras_router`: la ruta cuelga de `/purchase-receipts`, así que los permisos y filtros por prefijo de ruta de VentaLibra (`compras.recibir`, costos) la cubren.

## ADR-047 — El ajuste de stock con lote exige una guarda propia: `OpcionesStock.autorizar_lotes` (2026-10-12)

**Problema** (anotado el 2026-10-01 en los pendientes de VentaLibra: «Ajuste de stock con lote por API, deshabilitado a propósito»). Con `OpcionesStock(con_lotes=True)` el cuerpo de `POST /api/stock/{pid}/ajuste` acepta `lot_code` y `expires_at`: el conteo de UN lote (`modo="absoluto"`) o una entrada a un lote (`modo="entrada"`). Eso es lo mismo que hacen `POST /api/vencimientos/asignar`, `/entrada` y `/merma`,
pero el producto monta el ajuste con la capacidad del ajuste común y esas tres con otra (`dependencias_movimientos`). Con un rol que pudiera ajustar stock y no mover lotes, el ajuste habría sido una vía lateral para alterar lotes. Por eso VentaLibra dejó la opción apagada.

**Qué ya existía** (se miró antes de cambiar). El motor **ya respeta** `lot_code` y `expires_at` en el ajuste desde A-4 PR-3 (ADR-018; `stock.ajustar_stock` y `stock.entrada_manual_con_lote`): con la opción prendida el conteo es contra el saldo de ESE bucket, un lote que no existe se crea, y un producto sin marcar es 422. Lo que se anotó como «el motor ignora `lot_code`» describe la opción **apagada**,
que lo ignora a propósito (el cuerpo es el de siempre). Lo único que faltaba era la guarda.

**Patrón reutilizado.** El de `OpcionesCatalogo.autorizar_marcar_vence` (un `Callable[[dict], bool]` que recibe el usuario de la sesión) y el de `build_vencimientos_escritura_router` (la factory FALLA al construirse si falta lo que hace seguro el router).

**Decisión.**
- **`OpcionesStock.autorizar_lotes: Callable[[dict], bool] | None = None`.** Se consulta **por cuerpo**: sólo si el pedido trae `lot_code` o `expires_at` (con `is not None`: un `""` también cuenta como «traer lote»), y **antes de leer la base**, así que un pedido con lote de quien no puede tocar lotes es **403** sea cual sea el producto (existente o no, marcado o no) o el modo (incluso uno que después se rechazaría con 422).
  No se escribe nada. El ajuste sin esos campos no la consulta: es el ajuste común, con la protección con la que el producto monte el router.
- **Default seguro: la opción no se puede prender sin la guarda.** `build_stock_router` levanta `ValueError` al construirse si `con_lotes=True` y falta `autorizar_lotes` **o** `usuario_actual` (sin sesión no hay a quién preguntarle). Se eligió fallar al construir y no «denegar siempre»: un 403 permanente sería indistinguible, desde afuera, de un permiso mal configurado.
  Apagada (`con_lotes=False`, el default) no se pide nada y `autorizar_lotes` no se consulta: Contalibra y Restolibra no cambian, ni el cuerpo, ni el esquema OpenAPI, ni las respuestas.
- **Sin migración**: no toca el esquema ni el ledger.
- **Cambio de contrato.** Quien ya montaba `con_lotes=True` tiene que pasar la guarda. Ningún producto de la familia lo hacía (VentaLibra la tenía apagada; sólo los tests del motor la prendían, y se adaptaron).

**Tests** (`tests/test_fefo_devolucion_transferencia_ajuste.py`, contra SQLite y PostgreSQL): sin la capacidad, ocho formas de pedido con lote (absoluto, entrada, sólo `expires_at`, `lot_code` vacío, modo inválido, producto sin marcar, producto inexistente) dan 403 y el ledger no cambia; con la capacidad, el conteo mueve el lote pedido y no toca los otros, y la entrada va a un lote nuevo;
el ajuste sin lote (absoluto, entrada, salida; marcado y sin marcar) anda sin la capacidad y la guarda ni se llama; la guarda recibe el usuario de la sesión; la factory falla sin `autorizar_lotes` o sin `usuario_actual`, y apagada no pide nada. `tests/test_guardia_booleanos.py` pasa la guarda al prender la opción. Los tests de la guarda se ponen rojos sin ella.

**No cubre.**
- **El ajuste SIN lote de un producto marcado sigue yendo por FEFO** (ADR-018, A-4 PR-3): un delta negativo consume lotes, y eso lo puede hacer quien tenga el permiso del ajuste común. No es nuevo ni depende de `con_lotes`; la guarda es sobre elegir un lote en el cuerpo, no sobre el efecto de un ajuste común en los lotes.
- La guarda es un solo permiso para «contar un lote» y «entrar a un lote». Si un producto quiere separarlas necesita otra costura.

**Cómo se adopta.** Subir el pin. Quien quiera el ajuste con lote por API monta `OpcionesStock(con_lotes=True, autorizar_lotes=<su capacidad de mover lotes>)` y le pasa `usuario_actual` a `build_stock_router`. Quien no lo use no cambia nada.

## ADR-048 — El quiebre por cantidad cuenta por producto: `POST /api/listas-precio/{id}/precios` (2026-10-12)

**Problema.** El precio por cantidad de la lista (`item_prices.min_quantity`) se resolvía **por línea** (`GET /api/listas-precio/{id}/precio`): dos talles de una remera, de a una unidad, no llegaban a un quiebre de 2, y un producto sin variantes escaneado dos veces en dos líneas tampoco. Las promociones «llevá N pagá M» ya agrupaban por `producto_id` (ADR-045), así que el mismo carrito tenía dos reglas distintas para «cuántas unidades llevó». Es lo que ADR-045 dejó anotado en «No cubre».

**Qué ya existía** (se miró antes de elegir).
- `resolve_price` (`db/repository.py`) ya elige el quiebre más alto aplicable a **una cantidad**, con vigencia (`valid_from`/`valid_until`), sucursal y desempate por el más reciente. No hace falta tocarlo: lo que faltaba era **qué cantidad se le pasa**.
- `erp.listas_precio.precio_vigente` ya valida la variante contra el producto y pasa `en` a hora local (`a_hora_local`); se reusa tal cual.
- `erp.promociones.calcular` ya agrupa por `producto_id` y suma las cantidades de las líneas con y sin variante. Es la regla que se alinea.
- **El motor NO recalcula el precio de la venta.** `POST /api/ventas` (`web/ventas_router.crear`, `erp/ventas.crear_venta_directa`) guarda el `precio` que manda el cliente en cada línea (`max(0, precio)`), y sólo recalcula las promociones, a partir de esos precios. El que decide el precio es el cliente (el POS), el mismo trato que ya tenía el quiebre por línea. Este ADR **no** cambia eso (ver «No cubre»): el motor ofrece la cuenta bien hecha y el POS la pide.

**Decisión.**
- **Función del motor `erp.listas_precio.precios_por_producto(conn, lista_id, lineas, *, sucursal_id, en)`**: dadas las líneas `[{producto_id, variante_id, cantidad}]`, suma las cantidades **por `producto_id`** (con o sin variante, en `Decimal`) y resuelve **una vez por producto** con ese total (`resolve_price`). Devuelve una entrada por línea, **en el mismo orden**: `{producto_id, variante_id, cantidad_producto, precio}`. Un mismo momento (`en`) y una misma sucursal para todo el carrito.
- **Ruta `POST /api/listas-precio/{lista_id}/precios`** en `build_quiebres_router`, junto a `GET /precio`. Cuerpo `{lineas: [...], sucursal_id?, en?}`; respuesta `{precios: [...]}`. Es un POST por el cuerpo, **no escribe nada**. Una lista inexistente es 404; una variante que no es del producto, un `en` que no es fecha o más de 500 líneas, 422; los campos numéricos rechazan booleanos (ADR-027).
- **`GET /precio` no cambia**: resuelve **una línea por su cantidad**, como siempre (lo usan Contalibra para re-cotizar un renglón de presupuesto y los tests de quiebres). El quiebre por producto es una **pregunta distinta** («¿a cuánto va cada línea de este carrito?») y por eso es una ruta aparte, no un parámetro más del GET.
- **Una cantidad que no es positiva no suma** (igual que en `promociones.calcular`): la línea se cotiza al total que junta el producto, o al precio base si no junta nada. La variante **no cambia el precio** (no tiene precio propio, ADR-045): se valida y se devuelve, nada más.
- **Productos distintos no se suman**, aunque tengan el mismo quiebre. Sin migración, sin cambio de esquema.

**Tests** (`tests/test_quiebre_por_producto.py`, contra SQLite y PostgreSQL): dos variantes de a una unidad con un quiebre de 2 dan el precio del quiebre; un producto sin variantes en dos líneas, igual; una sola unidad no llega; productos distintos no se suman (solos y mezclados); la suma de cantidades distintas contra un quiebre de 5; varios quiebres sobre el total; la suma en decimal (0,1 + 0,2 contra un quiebre de 0,3); cantidades 0 y negativas; producto sin precio en la lista; vigencia (`en` pasado, y con zona `...Z` convertido a hora de Argentina) y precio por sucursal; variante ajena, lista inexistente, `en` inválido, booleanos y tope de líneas; y que `GET /precio` siga resolviendo por línea. Los de la suma se ponen rojos si `precios_por_producto` resuelve cada línea por su cantidad.

**No cubre.**
- **El servidor no valida el precio de la línea.** `POST /api/ventas` confía en el `precio` que manda el cliente, con el quiebre por línea o por producto. Si un día se quiere que el motor recalcule al confirmar, es otra decisión (cambia el contrato de la venta, y el POS offline manda el suyo). **Resuelto en ADR-049** (`OpcionesVentas.validar_precios`, opt-in).
- **Contalibra (mayorista) no cambia**: sigue cotizando cada renglón de presupuesto con `GET /precio`, por su cantidad. La ruta nueva queda montada ahí (viaja en `build_quiebres_router`, detrás del add-on `mayorista`) y sin uso. Si el humano quiere el mismo criterio en presupuestos, el motor ya lo tiene.
- Las líneas con precio fijado por fuera de la lista (la etiqueta de la balanza) las resuelve el producto: el motor no sabe qué línea vino de una balanza, así que el POS las cuenta para el total pero no las reprecia.

**Cómo se adopta.** Cortar tag y subir el pin en los productos que lo quieran. Quien monta `build_quiebres_router` recibe la ruta nueva; si la gatea por ruta (VentaLibra, `requiere_segun_ruta`) tiene que decidir el permiso de `POST /{lista_id}/precios`: por defecto cae en el más estricto, y el POS lo necesita con el de **leer** precios.

## ADR-049 — El servidor valida el precio de cada línea de la venta: `OpcionesVentas.validar_precios` (2026-10-12)

**Problema.** `POST /api/ventas` guardaba el `precio` que mandaba el cliente en cada línea (`max(0, precio)`) y sólo recalculaba las promociones, a partir de esos mismos precios. El motor ya sabía contestar «¿a cuánto va cada línea de este carrito?» (ADR-048) y el POS de VentaLibra le preguntaba, pero nada impedía que un cliente armado a mano —o una pantalla abierta mientras la lista cambiaba— cobrara a otro precio. ADR-048 lo dejó anotado en «No cubre» («el servidor no valida el precio de la línea»); el humano decidió cerrarlo (2026-10-12).

**Qué ya existe** (se miró antes de elegir).
- `erp.listas_precio.precios_por_producto` (ADR-048) es exactamente la cuenta del POS, con el quiebre por producto; `resolve_price` elige la lista predeterminada activa sola (`is_default = 1 AND active = 1`), y `precio_vigente` pasa `en` a hora local. Se reusa tal cual: **no hay una segunda cuenta de precios**.
- `domain.scale.parse_scale_barcode` y `erp.catalogo.get_formato_balanza` (la `commerce_settings` con clave `scale.format`) son **del motor**, y `catalogo.escanear` ya resuelve la etiqueta a producto, cantidad e importe. VentaLibra sólo guarda esa configuración (`app/services/scale.py`). Por eso **no hace falta una costura** para leer la balanza: el servidor decodifica con lo que ya tiene.
- `OpcionesVentas` ya es el lugar de las reglas opt-in de la venta (`exigir_pago_completo`, `promociones`, `guardar_costo`…), y `promociones.calcular` ya es un precedente de «el servidor calcula y no confía en el cliente».
- El POS de VentaLibra cotiza con `POST /api/listas-precio/{lista predeterminada activa}/precios`, **sin `sucursal_id`** y con `en` = ahora; usa el precio plano del producto (`default_sale_price`) cuando no hay lista o la lista no tiene precio para el producto; y **nunca manda un `descuento`** (ni en `confirmar` ni en `cobrarConQr`) ni una línea sin `producto_id`.

**Decisión.** Opción **aditiva**, `OpcionesVentas.validar_precios: bool = False` (el comportamiento de hoy: respuestas byte a byte las de antes; Contalibra y Restolibra no cambian). Prendida, `crear()` valida **antes de escribir nada**, con una lectura pura (`erp.precio_de_venta.validar_precios`):
- **Precio esperado de cada línea:** el de `precios_por_producto` con la lista predeterminada activa (el quiebre cuenta por producto, ADR-048), a ahora; si no hay lista, o no tiene precio para ese producto, el plano. Un centavo de tolerancia. Un producto que no existe no se valida: lo sigue rechazando `crear_venta_directa` con su 422.
- **Sin sucursal.** El motor cotiza con `sucursal_id=None`, **igual que el POS**: si resolviera la sucursal del depósito de la venta mientras el POS no la manda, cada venta con un precio por sucursal daría 409 y la pantalla re-preciaría de nuevo al precio general. Cuando el POS pase la sucursal, el servidor tiene que pasarla igual (ver «No cubre»).
- **Si alguna línea no coincide:** `409` con `{detail: {mensaje, codigo: "precios_distintos", precios: [{indice, producto_id, variante_id, enviado, esperado}]}}`; `indice` es la posición de la línea **en el pedido original** (una línea descartada por no tener cantidad no corre las demás). No se corrige en silencio y no se escribe nada: ni venta, ni stock, ni caja, ni pagos. Es una `JSONResponse` y no una `HTTPException` (el `detail` es un objeto), y **los precios van dentro del `detail`** y no al lado: el `ApiError` del cliente de `libra-ui` sólo conserva el `detail` (su texto es el `mensaje`, y el objeto queda en `detailData`, como los Términos pendientes), así que un `precios` suelto en la raíz no le llegaría a ninguna pantalla.
- **Balanza.** `ItemPayload.etiqueta` (opcional, el EAN-13 crudo) se decodifica con el formato de balanza del comercio y la línea se valida contra ella: **por importe**, el precio es el de la etiqueta y la cantidad tiene que ser 1 (cada etiqueta es un paquete); **por peso**, la cantidad es el peso de la etiqueta y el precio, el de la lista (o el plano) por unidad de peso. El `esperado` de un 409 por importe es el de la etiqueta. Una etiqueta que no se lee (o con la balanza apagada), que es de **otro producto** que el de la línea o cuya cantidad no coincide es **422** (no se cura re-preciando). Una línea **sin** etiqueta se valida como cualquier otra, a la lista o al plano: no hay forma de cobrar un importe de balanza sin la etiqueta que lo respalda.
- **Ítem libre (sin `producto_id`):** `422`. Sin producto no hay precio que esperar; el POS no los genera. (Contalibra y Restolibra, que sí venden renglones libres, no prenden la opción.)
- **`descuento` manual del pedido > 0:** `422`. El POS nunca lo manda; el ahorro de las promociones lo sigue calculando el servidor (`promociones=True`) y se suma como siempre.
- Una variante que no es del producto de su línea es 422 (la misma validación de `precios_por_producto`).
- `erp.listas_precio.lista_predeterminada_activa(conn)` es la consulta de la lista, nombrada una vez.

**Tests** (`tests/test_validar_precio_de_venta.py`, contra SQLite y PostgreSQL): la opción apagada no cambia nada (ni el precio, ni el descuento, ni el ítem libre, ni una etiqueta cualquiera); el precio de la lista pasa; uno distinto da 409 con el esperado y **ninguna de las cinco tablas de la venta cambia** (ni el stock); varias líneas con la posición original; la tolerancia de un centavo; precio negativo; sin lista, lista que no es la predeterminada, predeterminada desactivada, lista sin precio para el producto y vigencia futura; el quiebre entre variantes (dos de a una pagan el quiebre, una sola no, y mandar el base cuando corresponde el quiebre es 409), entre líneas del mismo producto y sin sumar productos distintos; ítem libre, descuento manual, producto inexistente y variante ajena; las promociones siguen calculándose en el servidor; y la balanza por importe y por peso (válida, con otro precio, sin etiqueta, con otra cantidad, con el peso sumando al quiebre) y las etiquetas que no valen. Sin la validación (`validar_precios` ignorada) 64 de los 90 se ponen rojos (medido; los 26 que siguen verdes son los de la opción apagada y los que ya rechaza el modelo).

**No cubre.**
- **Una etiqueta de balanza no se puede autenticar.** El EAN-13 es público y el dígito verificador no se controla: un cliente que arma una etiqueta falsa de $0,01 para un producto de balanza por importe la pasa. La validación protege de precios viejos, de pantallas desactualizadas y de errores, **no** de un cliente deliberadamente falso con una sesión válida de cajero. Cerrarlo pide otra cosa (una balanza que firme, o el importe cruzado contra el precio por kilo y el peso real).
- **El precio por sucursal** (`item_prices.branch_id`): el POS no lo usa hoy y, por eso, el servidor tampoco. Si el POS empieza a mandar `sucursal_id`, hay que sumarle a la opción una costura que lo resuelva desde el depósito de la venta; hasta entonces, un precio cargado por sucursal no se cobra en el POS ni se valida. ✅ **Resuelto en ADR-058 (v0.59.0):** `VentaPayload.sucursal_id` y la costura `OpcionesVentas.sucursal_de_la_venta`.
- **Lo que no entra por `POST /api/ventas` no se valida.** La venta que llega por la sincronización offline (`integrations.libraedge.apply_confirmed_sale_operation`, `docs/OFFLINE_SYNC.md`) se materializa por otro camino y conserva los precios con los que se vendió sin conexión; lo mismo cualquier caso de uso que llame a `erp.ventas` sin pasar por el router. Es lo razonable (la venta ya ocurrió al precio que tenía el nodo), pero es un límite: la opción cubre la API del POS, no toda escritura de ventas.
- **Cada etiqueta puede usarse más de una vez**: el servidor no recuerda las que ya vendió.
- Contalibra y Restolibra no cambian: no prenden la opción.

**Cómo se adopta.** Cortar `v0.56.0` (la versión sale del tag, no hay literal que mover) y subir el pin en los productos que lo quieran. VentaLibra prende `validar_precios=True` en su `OpcionesVentas` y su POS manda la `etiqueta` de las líneas de balanza y maneja el 409 (ADR-079 de VentaLibra).

## ADR-050 — El log de actividad deja de tener su propia copia del formato: `RepositorioAuditado` le pasa un `Cambio` a un registrador (2026-10-12)

**Pedido del dueño.** Que la auditoría de escrituras viva en el motor y el formato del log sea uno solo en la familia (`libraauth` ADR-022, `libracore` ADR-043).

**Problema.** `db/auditoria.py` era una **segunda copia** del formato que `libraauth.auditoria` arma para el ORM, y ya había divergido: tapaba menos columnas (sin `password_cifrada`, `token_hash` ni `secret_key`: el valor de una de esas configuraciones, cambiada, habría quedado copiado en el diff), ignoraba `created_at`, dejaba una fila `editar` vacía cuando no cambiaba nada y el alta de una configuración llevaba `cambios`. Dos copias de un formato se desalinean; la solución no es sincronizarlas a mano.

**Qué ya existía** (se miró antes de diseñar). El `registrador_dbapi` de `libraauth` v0.49.0 (formato en funciones puras: `fila_de_auditoria`, `diff_de_valores`, `etiqueta_de_valores`) y la costura `libracore.auditoria` (v1.156.0), cuyo registrador es un callable `(conn, cambio)` con `Cambio(tabla, accion, id, antes, despues)`. Este motor ya recibía el usuario como callable «para no depender de libraauth»: es el mismo criterio.

**Decisión.**
- **`RepositorioAuditado(repo, conn, registrador)`**: el wrapper arma un `Cambio` por escritura (fotos `antes`/`despues` como dict plano de los campos de la dataclass, una por método declarado en `AUDITABLES`) y se lo pasa al registrador con la `conn` de la escritura. Contrato idéntico al de `libracore.auditoria`, así **un solo registrador sirve a los dos** y el producto lo construye una vez. No importa `libraauth` ni `libracore`; el motor sigue con `dependencies = []`.
- **El registrador es obligatorio, sin default.** Un default sería una tercera copia del formato; un wrapper que no audita sin avisar es el modo de falla para el que existe todo este archivo. `RepositorioAuditado(repo, conn)` y `usuario=` son `TypeError`. **Es un cambio de contrato**: VentaLibra y LibraClub (`app/commerce.py`) tienen que pasar el registrador al subir el pin. Se prefirió romper fuerte y chico (dos consumidores, un archivo cada uno) a conservar «provisoriamente» un formato propio que vuelve a divergir.
- **Lo que este motor aporta al registrador** (`opciones_del_registrador()` → `columnas_ignoradas={"created_at"}`, `etiqueta=etiqueta_de_tabla`, `resumir=resumir_colecciones`; y `entidades_por_tabla()` para la lista blanca): lo que sólo él sabe — qué campo etiqueta cada tabla (`display_name`, `number`…), que `created_at` lo recalcula el motor y que una colección de items se resume en «N items» (la comparación sigue siendo sobre el valor crudo). `AUDITABLES` gana la `tabla` de cada entrada; un test la cruza con el schema.
- **Cambios de formato respecto de v0.56**, todos hacia la regla de `libraauth`: se tapan también `password_cifrada`, `token_hash` y `secret_key`; guardar sin cambiar nada **no deja fila**; el alta de una configuración **no lleva `cambios`**; `entidad_id` va como texto; el usuario sale del `ContextVar` de `libraauth` (ya no hay parámetro). Una edición cuyo método no sabe leer el estado previo (`save_item_code`, `save_item_price`, `save_purchase_receipt`) llega con `antes=None` y queda **como hecho sin detalle**, igual que antes.
- **`entidad_id INTEGER` del DDL no se toca, sin migración.** Se midió dónde corre cada DDL: este `CREATE TABLE IF NOT EXISTS` sólo crea la tabla en bases que no la tienen. En las que la crea `libraauth` (`AuditoriaBase`: LibraDesk, Gestiolibra, MedLibra y la base del dominio de LibraClub) es `String(100)` y este DDL no corre; en las que la crea este motor (VentaLibra, Contalibra, Restolibra y la base de LibraCore de LibraClub) todos los ids que se auditan —de este motor y de `clients`— son enteros, y el registrador entra igual (el id va como texto y la base lo asigna al entero: medido con SQLite y con PostgreSQL por psycopg). Cambiar el tipo sería un `ALTER COLUMN` sobre una tabla de producción para arreglar algo que no falla.
- **Tests**: `tests/test_auditoria.py` fija el contrato del wrapper (qué `Cambio` emite cada escritura, que sin registrador no se construye, que el guardián de escrituras sigue en pie); `tests/test_auditoria_formato_libraauth.py` fija el formato con el registrador real sobre la tabla de este motor (se saltea sin `libraauth` ≥ 0.49: el pin de `dev` se suma cuando exista el tag, junto con el `uv.lock`); `tests/registrador_de_prueba.py` es el registrador mínimo de las pruebas de transaccionalidad, que sólo necesitan que haya filas.

**No cubre.**
- Una instancia que ya tiene filas viejas de v0.56 las conserva tal cual (no hay backfill ni reescritura): el formato nuevo rige desde el despliegue.
- `ActividadRepository` (la lectura) no cambia: lee las mismas columnas.
- El camino de `set_setting` compara la clave sin pasar a minúsculas (antes: `key.lower()`); las claves de este motor son snake_case en minúscula, y una nueva con mayúsculas que se llame como una oculta no se taparía.

**Cómo se adopta.** Cortar tag (v0.57.0), subir el pin en VentaLibra y LibraClub y, en cada `app/commerce.py`, pasar `registrador_dbapi({**entidades_por_tabla(), "clients": "cliente"}, **opciones_del_registrador())` en lugar de `usuario=usuario_actual.get`. Los productos que no usan `RepositorioAuditado` (Contalibra, Restolibra) no cambian.



## ADR-051 — El depósito por defecto no se desactiva en ninguna capa, y un movimiento de stock no toca un depósito inactivo (2026-10-13)

**Problema** (pendiente B.4, medido el 2026-09-17). La guarda «no se puede desactivar el depósito por defecto» vivía sólo en la capa ERP/web (`catalogo.update_deposito`). Tres huecos sobre `develop` `53cf73a`:
1. `repo.save_location` guardaba `active=False` sobre el default sin ninguna guarda. **Hay un consumidor que lo hace**: `libradesk` (`app/services/inventario.py:editar_deposito`) guarda `Location(active=activo, is_default=actual.is_default)`, así que un default apagado desde LibraDesk es posible hoy.
2. `catalogo.get_default_deposito_id` no miraba `active`, y `erp.stock.add_movimiento_stock` (de donde cuelgan `descontar_stock_venta`, `ajustar_stock`, `salida_manual`, la recepción y los lotes) no validaba el depósito: un default inactivo seguía recibiendo stock, y un `deposito_id` explícito inactivo o inexistente llegaba al `INSERT` (si no existía, como `IntegrityError` de la FK, un 500).
3. Los casos de uso viejos (`usecases/`) recibían un `location_id` sin validar: `confirm_sale`, `confirm_purchase_receipt` (la recepción vieja de tres pasos y `recibir_mercaderia` sólo miraba que existiera, no que estuviera activo) y `transfer_stock`.

**Qué ya existía.** `catalogo.validar_deposito` y `DepositoInexistente` (ERP), usados por `registrar_venta`, `devolver_items`, `transferir_stock` y el ajuste del router; `update_deposito`/`set_default_deposito` con su guarda. Se reutiliza el mensaje y la clase; lo nuevo es llevar la regla al dominio.

**Decisión.** La regla es del dominio y vive en un solo lugar, `domain/inventory.py`:
- `exigir_default_activo(location)` → `DepositoPorDefectoInactivo` (un `ValueError`, con `MENSAJE_DEFAULT_INACTIVO`, el mismo texto de la ERP). **`repo.save_location` la aplica** (alta y modificación), de modo que ningún camino —ERP, web, un producto que guarda la `Location` directo— pueda guardar el default inactivo. `update_deposito` la llama también, antes de las otras guardas, para no depender del orden. Se aplica a lo que se GUARDA, nunca a lo que se lee: una base con un default inactivo heredado tiene que seguir abriéndose.
- `exigir_deposito_activo(location, id)` → `DepositoInexistente`, que **se movió** de `erp/catalogo.py` a `domain/inventory.py` (se reexporta con el mismo nombre; `catalogo.DepositoInexistente` y `ventas.DepositoInexistente` son la misma clase). La usan `catalogo.validar_deposito` (ERP) y `usecases.inventory.verificar_deposito` (dominio): un solo criterio y un solo mensaje.
- **`add_movimiento_stock` valida el depósito**: un `deposito_id` inexistente o inactivo es `DepositoInexistente` antes de escribir (hacia o desde: una salida de un depósito inactivo también). Sin `deposito_id`, el por defecto, y si no hay ninguno activo, `DepositoInexistente("No hay ningún depósito activo…")` en vez de un `NOT NULL`. Las validaciones tempranas de cada operación (`registrar_venta`, `devolver_items`, `transferir_stock`, el router de ajuste) siguen rebotando primero, antes de escribir el resto.
- **Excepción deliberada: `sobre_existencias=True`**, para las tres operaciones que trabajan con existencias que YA están en el depósito, que pudo darse de baja después: `anular_venta` (repone donde salió la mercadería; negarse dejaría la venta sin poder anularse), `vencimientos.asignar_vencimiento` (reclasifica «sin lote» a un lote) y `vencimientos.dar_de_baja_lote` (la merma). Esta última es una decisión anterior (ADR-018: «las existencias de un depósito dado de baja siguen existiendo»; `test_la_entrada_exige_deposito_activo_pero_leer_asignar_y_mermar_siguen_valiendo` la fija) y el primer intento de este ADR —rechazar todo— la rompió. **Stock nuevo** (una entrada, un ajuste, una venta, una recepción) nunca lleva el flag. `cancel_sale` (dominio) tampoco valida, por lo mismo.
- **`get_default_deposito_id` no devuelve nunca un inactivo**: el marcado por defecto si está activo; si no (o no hay marcado), el activo de menor id; si no hay ninguno, `None`.
- **Los casos de uso validan antes de escribir**: `confirm_sale`, `confirm_purchase_receipt` y `transfer_stock` (origen y destino) llaman a `verificar_deposito`. `erp.compras.recibir_mercaderia` usa `validar_deposito` (existe **y está activo**); el router de `confirm` contesta 422.
- Un hueco menor del mismo patrón, cerrado: `set_default_deposito` de un id que no existe levanta `DepositoInexistente` (antes el `UPDATE … is_default=0` dejaba al motor sin ningún default). (La baja de una sucursal que tiene el default ya estaba cubierta por `_verificar_baja_de_sucursal`: «el depósito por defecto de la instancia».)

**Si una base ya tiene un default inactivo** (heredado de LibraDesk, o de antes de la guarda de la ERP): no se migra nada. El código lo ignora —los movimientos sin depósito van al activo de menor id— y sigue pudiéndose leer y editar; para repararlo, un administrador marca otro depósito como default. Medir una base: `SELECT id, name FROM locations WHERE is_default = 1 AND active = 0;`. Una `CHECK (NOT (is_default = 1 AND active = 0))` quedó afuera a propósito: la migración fallaría justo en las bases que tienen el problema.

**Tests.** `tests/test_deposito_activo.py`, contra los dos motores: el repositorio no guarda el default inactivo (alta y modificación, con el mismo mensaje que la ERP) y sí uno que no es default; la baja de una sucursal con el default (fija lo que ya se frenaba); `set_default_deposito` de un id inexistente; un movimiento hacia o desde un depósito inactivo o inexistente (ajuste, salida manual, `descontar_stock_venta`); que asignar y dar de baja un lote siguen valiendo en uno inactivo lo fija el test existente de `test_carga_vencimientos.py`; la base con un default inactivo heredado; ningún depósito activo; `anular_venta` con el depósito dado de baja después; la recepción (`recibir`, 422 y `confirm`, 422); `confirm_sale`, `confirm_purchase_receipt` y `transfer_stock` (origen y destino) sin escribir nada.

**No cubre.**
- **No hay candado contra la carrera**: dos pedidos, uno desactivando el depósito y otro moviendo stock, pueden cruzarse (la validación y el `INSERT` no comparten un `SELECT … FOR UPDATE`). Es el mismo límite que ya tenían `registrar_venta` y `transferir_stock`.
- El stock que un depósito tenía **antes** de apagarse queda ahí: apagar un depósito no lo traslada (ya era así).
- Un producto que escribe `stock_movements` por SQL propio (no por `add_movimiento_stock` ni por el repositorio) no pasa por la regla. Medido con `git grep "INSERT INTO stock_movements"` sobre `origin/develop` de los ocho productos: ninguno, fuera de los tests; el único es el script de migración de este repo (`libracommerce/scripts/migrate_from_contalibra.py`).

**Cómo se adopta.** Subir el pin; sin migración. Efectos visibles: LibraDesk al desactivar el default recibe el `ValueError` (su router ya lo traduce a 422); `libradesk` y `libraclub` pasan `location_id` a `confirm_purchase_receipt`/`confirm_sale` y ahora se rechaza uno inactivo.

## ADR-052 — «Ahora» y «hoy» salen de un reloj único, `libracommerce.hora`: quedaban cuatro lecturas de la hora del sistema (2026-10-13)

**Problema.** ADR-042 (reposición) y ADR-044 (ajustes, transferencias, margen) cerraron los `date.today()`. Releído el paquete con `git grep "date.today()\|datetime.now()\|datetime.utcnow()\|time.localtime"` sobre `develop` `53cf73a`: ya no queda ningún `date.today()`, pero sí cuatro `datetime.now()` sin zona (ADR-044 los había dejado afuera, «son un instante, no el día») y un reloj de SQL:
- `db/repository.py:613` — `resolve_price` sin `at`: compara contra `valid_from`/`valid_until`, que están en hora local, con la hora del sistema. En un servidor en UTC, de 21 a 24 hs un precio vigente hasta las 23:00 ya figuraba vencido.
- `erp/listas_precio.py:338` — `set_precio_vigente` sin `desde` escribe «ahora» como `valid_from`: en UTC quedaba tres horas en el futuro.
- `erp/promociones.py:119` — `created_at` de una promoción.
- `db/repository.py:430` — `set_setting` escribía `updated_at = CURRENT_TIMESTAMP`: UTC en SQLite, `timestamptz` con su formato en PostgreSQL (la migración 0011 había pasado los DEFAULT a hora de Argentina, pero este `UPDATE` los pisaba con otra cosa).
(Más `promociones.py:210`, ya correcto pero con la zona repetida a mano.) Esto **no** era una discrepancia teórica: el contenedor desplegado tiene `TZ` de Argentina y por eso no se vio, igual que en ADR-042.

**Qué ya existía.** `vencimientos.hoy_argentina()` (ADR-042/044) y `listas_precio._ZONA_LOCAL`/`a_hora_local`. No servían para la capa de base (`db/` no puede importar `erp/`), y `hoy_argentina` leía el reloj real por su cuenta, así que cada test tenía que fijarla aparte. `libracore.db.core._ar_now()` (que `ventas.py` ya usa) existe pero este motor no importa `libracore` en runtime salvo en los extras.

**Decisión.**
- **Módulo nuevo `libracommerce/hora.py`**, sin dependencias: `ZONA_LOCAL` (UTC-3 fijo), `ahora_utc()` —el **único** lugar que lee el reloj del sistema—, `ahora_local()` (hora de pared sin zona, la forma en que se guardan las vigencias) y `hoy_local()`. Los helpers llaman a `ahora_utc` a través del módulo: **fijar `hora.ahora_utc` en un test fija «ahora» y «hoy» en todo el motor**.
- `hoy_argentina()` lee de `hora.ahora_utc` (misma firma, mismo resultado); `_ZONA_LOCAL` se conserva como nombre, apuntando a `hora.ZONA_LOCAL`. Las cuatro lecturas usan `ahora_local()`; `set_setting` pasa `updated_at` como parámetro, en el formato del DEFAULT (`YYYY-MM-DD HH:MM:SS`).
- **Guard** (`tests/test_sin_reloj_del_sistema.py`): recorre el paquete con `ast` y falla ante `.today()`, `.utcnow()`, `.localtime()`, `.now()` sin zona (o con `None`) y relojes de SQL sin modificador (`CURRENT_TIMESTAMP`, `datetime('now')`). Un instante CON zona (`datetime.now(UTC)` de `compras_router` y `precio_de_venta`) no se marca. Lista `EXCEPCIONES` explícita, hoy **vacía**; el test también falla si una excepción deja de hacer falta, y hay tests del guard mismo (caza cada forma, deja pasar `now(UTC)` y los textos de documentación).

**Tests.** `tests/test_hora_argentina.py`: con el reloj fijo a las 22:30 de Argentina (01:30Z del día siguiente): `ahora_local`, `hoy_local` y `hoy_argentina` dan el día de Argentina; el precio sin `desde` arranca 22:30; `resolve_price` sin `at` ve un precio que vence a las 23:00; la promoción y la preferencia se estampan 22:30; el ajuste de stock sin fecha queda con el día 14. El test de promociones del reloj en UTC (ADR-042) pasó a fijar `hora.ahora_utc`.

**No cubre.**
- Los `datetime.now(UTC)` de `compras_router` (instante de la recepción) quedan: son un instante con zona, y el ledger mezcla ese formato con las fechas locales sin zona desde siempre. Unificarlos es otra decisión (cambia el texto de `occurred_at` y el orden entre filas).
- Las filas que ya se escribieron con la hora del sistema no se corrigen.
- Los tests siguen pudiendo usar `date.today()` (el guard recorre sólo el paquete): siete módulos lo usan como fecha de sus fixtures (`HOY`). No se tocaron; si alguno es una bomba de 21 a 24 hs se ve corriendo la suite con el reloj del proceso en esa franja.

**Cómo se adopta.** Subir el pin; sin migración, sin cambio para quien despliega con `TZ` de Argentina.

## ADR-053 — Se retira `usecases.sales.return_sale_items`: ningún consumidor lo llama (2026-10-13)

**Problema.** El camino viejo de devolución (`usecases.sales.return_sale_items`, sobre el dominio `Sale`) recibe un `location_id` sin validar, y ADR-043 (2026-10-10) ya había tenido que arreglarle la cuenta del reintegro porque seguía devolviendo precio de lista. Dos copias de una operación de plata que nadie ejerce son una deuda que se rompe en silencio.

**Verificación de que es código muerto** (`git grep "return_sale_items"` y los usos de `libracommerce.usecases.*` sobre `origin/develop` y `origin/main` de todos los repos de `~/proyectos`, 2026-10-13): **ningún llamador** en `contalibra`, `restolibra`, `ventalibra`, `libradesk`, `libraclub`, `libracargo`, `gestiolibra`, `medlibra`, `libraedge`, `libracore`, `libra-panel` ni `libra-backoffice`. Las menciones son comentarios y tests de este repo. Quien usa `usecases` hoy: `libraclub` (`confirm_sale`), `libradesk` (`confirm_purchase_receipt`, `transfer_stock`, `verificar_disponibilidad`); `cancel_sale` tampoco tiene llamadores, y se deja (no era lo pedido).

**Decisión.** Se **borra** `return_sale_items` y su ayudante `_devuelto_por_linea`. **No** se borra lo que lo rodea:
- `domain.sales.reintegro_prorrateado` y `LineaReintegro` siguen: `erp.ventas.devolver_items` los usa (ADR-040). Sus tests, que pasaban por `return_sale_items`, se reescribieron directo sobre la función (`tests/test_reintegro_prorrateado.py`, mismos números).
- La lectura de las filas `source_type='sale_return'` (`erp.ventas._COND_DEVUELTO`, `erp.margen`, el tope de `devolver_items`) **se queda**: son datos que el caso de uso pudo haber dejado en una base, y hay tests con filas armadas a mano (`test_erp_ventas.py`, `test_margen.py`).
- ADR-043 queda como historia; su parte del dominio sigue vigente.

**Tests.** Se borraron los de `test_usecases.py` sobre `return_sale_items` (los de `confirm_sale`, `cancel_sale` y la recepción siguen) y `test_margen.py` arma la devolución vieja a mano. Un test confirma que `usecases.sales` ya no la exporta.

**Cómo se adopta.** Subir el pin; quien la importara (nadie, verificado) tiene que pasar a `erp.ventas.devolver_items`. Es un retiro de API pública de un motor 0.x: por eso el tag propuesto es una versión **menor** (v0.58.0).

## ADR-054 — Devolver un servicio del catálogo no escribe stock: se reintegra el importe y lo devuelto queda en `sale_service_returns` (revisión 0007, 2026-10-13)

**Problema.** Una línea con un servicio del catálogo (`catalog_items.item_type = 'service'`) queda en `sale_items` como `kind = 'product'` (el `kind` sólo mira si hay `producto_id`). La venta no escribe stock para ella (`erp.stock._es_servicio` en `descontar_stock_venta`), pero `erp.ventas.devolver_items` la trataba como un producto: escribía `+q` de `devolucion` sobre un ítem que no tiene inventario, y el servicio aparecía con stock (`get_stock_actual` > 0, una fila en `GET /api/stock/movimientos`). `tests/test_ledger_sin_marcar.py` lo fijaba como «comportamiento sorprendente». Decisión del humano (2026-10-13): **reintegrar el importe sin tocar stock**.

**Qué ya existía.** `_es_servicio` y su criterio en la venta; el reintegro prorrateado (ADR-040), la `clave_operacion` (ADR-041) y el estado `devuelta` / `devuelta_parcial` de `devolver_items`; y el precedente de una tabla que crea una revisión y que el código sondea por metadatos (`item_branch_min_stock`, 0005; `purchase_receipts.clave_operacion`, 0006).

**El problema escondido: el tope no tenía de dónde salir.** Todo lo que `devolver_items` sabe de «lo ya devuelto» sale del **ledger**: el tope por (ítem, variante), el reintegro prorrateado (que resta lo ya devuelto), el estado de la venta, la idempotencia por clave (la marca `[op:<clave>]` va en la nota de la fila) y la guarda de `anular_venta`; el margen resta lo devuelto leyendo las mismas filas. Sin escribir nada en el ledger, un servicio devuelto no dejaba rastro: se podía devolver la misma línea una y otra vez (cobrando de más), el reintento con clave de una devolución de sólo servicio reintegraba otra vez (no había fila que lo reconociera) y una venta mixta (servicio + producto) no llegaba nunca a `devuelta`. «Baja el tope» exige un lugar donde escribir.

**Decisión.**
- **Un servicio devuelto no llama a `add_movimiento_stock`.** `stock_movements` no se toca: ni una fila, ni siquiera un par `+q`/`−q` que se anule (tiene `CHECK (quantity_delta <> 0)`, y un par sería igual «tocar el stock»).
- **Tabla nueva `sale_service_returns`** (revisión Alembic `0007_devoluciones_de_servicio`, aditiva y vacía): `id`, `sale_id`, `item_id`, `variant_id`, `location_id` (el depósito de la operación, sólo para comparar contra la clave), `quantity` (> 0), `note` (con la marca `[op:<clave>]` al final, como las filas del ledger), `created_by`, `occurred_at`. Una fila por línea de servicio devuelta e intento. La escribe sólo `devolver_items`. Creada con `op.create_table`: el mismo código en SQLite y PostgreSQL. `init_schema()` sigue congelado (la tabla no existe hasta migrar).
- **Lo que la lee**, para que un servicio se comporte como cualquier otra línea salvo en el stock: el tope por (ítem, variante) y `ya_devuelto` (el reintegro prorrateado resta lo ya devuelto); el estado — `vendido_total` cuenta las líneas de servicio y `devuelto_total` suma lo devuelto de ellas, así que una venta mixta queda `devuelta_parcial` mientras falte cualquier cosa y `devuelta` recién cuando volvió **todo** (con servicio y producto, en cualquier orden o en una llamada); la idempotencia por clave (`_devolucion_de_la_clave` suma las filas de servicios con la misma marca, y un reintento contesta `repetida: true` con el mismo importe, o `ClaveReusada` si cambian cantidades, depósito o medio de pago); la guarda `VentaConDevoluciones` de `anular_venta`; y `margen._devuelto_por_clave` (un servicio devuelto baja unidades e ingreso, y devuelto entero sale del reporte).
- **Sin la revisión.** `tiene_devoluciones_de_servicio(conn)` sondea por metadatos (`PRAGMA table_info`). Una base sin la 0007 se comporta como antes para todo lo que no sea **pedir devolver un servicio**: ahí, `ventas.SinRevision` (503 en `POST /api/ventas/{id}/devolver`, con el comando `libracommerce-migrar upgrade`) **antes de escribir nada**, ni siquiera el producto de la misma llamada. Devolver productos de una venta que tiene servicios, listar, anular y el margen siguen andando.
- **Una línea sin producto del catálogo** (ítem libre, sin `item_id`) sigue sin poder devolverse: «anular la venta entera».

**Alternativa descartada: sin migración.** Escribir `+q` en el ledger como siempre y otra fila `−q` que lo anule (neto 0, como el par `devolucion` + `merma` de un perecedero, ADR-018) resolvería el stock sin tabla, pero deja dos filas de ruido por cada devolución en la pantalla de movimientos de un ítem sin inventario, y no cumple «no escribe stock». Si no se quiere migrar, es el plan B: se saca la tabla y `devolver_items` vuelve a escribir esas dos filas; el resto (tope, estado, clave, margen) ya sale del ledger.

**Historia que no se corrige.** Las devoluciones de servicios hechas **antes** de esta versión dejaron su `+q` en el ledger: esas filas siguen contando como devuelto (el tope y el margen las leen como siempre, y se suman a las de la tabla), así que nada se devuelve dos veces; el stock fantasma de esos servicios queda hasta que alguien lo ajuste.

**Tests.** `tests/test_devolver_servicio.py` (SQLite y PostgreSQL): el ledger vacío y el servicio sin stock; el tope; el prorrateo de a poco; la venta mixta en los dos órdenes y en una llamada; la clave (reintento, otras cantidades, otro depósito, y la devolución de sólo servicio); `anular_venta`; el margen; la base sin la 0007 (503, sin escribir, y el producto se sigue devolviendo); el router; y la revisión (datos previos, idempotencia, bajada). El `test_devolucion_de_un_servicio_del_catalogo_...` de `test_ledger_sin_marcar.py` se invirtió (ledger vacío).

**No cubre.** `devolver_items` sigue sin ser receta-aware (ADR-018). Ningún producto se enteró: la API de `POST /api/ventas/{id}/devolver` no cambia de forma.

**Cómo se adopta.** Subir el pin (tag propuesto **v0.59.0**, menor) y **correr `libracommerce-migrar upgrade` en el deploy**, antes de que nadie devuelva un servicio; sin migrar, sólo esa operación falla (503 con el comando).

## ADR-055 — Órdenes de compra: `POST …/purchase-orders/{id}/enviar` y `…/cancelar` (2026-10-13)

**Problema.** El motor tenía los cinco estados de una orden (`draft`, `sent`, `partial`, `received`, `cancelled`) pero **ninguna operación** que la pasara a `sent` o a `cancelled`: nacía `draft` y sólo una recepción la movía (`partial` / `received`). ADR-017 tuvo que contar el borrador como «en camino» porque «la orden que ya se le pidió al proveedor está en `draft`», y una orden equivocada no se podía anular (el kit no tenía qué botón mostrar).

**Decisión.** Dos transiciones y el modelo de estados escrito en `erp.compras`:

    draft ──enviar_orden──▶ sent ──(recepción)──▶ partial ──(recepción)──▶ received
      │                      │
      └──── cancelar_orden ──┴──▶ cancelled

- **`enviar_orden`** (`POST /api/purchase-orders/{id}/enviar`): `draft` → `sent`, y registra `ordered_at` (la primera vez; un segundo envío no lo pisa). **Idempotente:** enviar una `sent` no escribe nada y devuelve la orden. 409 desde `partial`, `received` o `cancelled`.
- **`cancelar_orden`** (`POST /api/purchase-orders/{id}/cancelar`): `draft` o `sent` → `cancelled`. **Idempotente** sobre una `cancelled`. **409 desde `partial` y `received`**: ya entró mercadería, y deshacerlo no es cancelar la orden sino devolverle al proveedor (una decisión que el modelo de estados no tiene y que no se inventa acá).
- **Una `UPDATE … WHERE status IN (…)` y no «leer, cambiar y guardar»**: `save_purchase_order` reescribe todas las líneas, y con una recepción que se confirma a la vez habría pisado `quantity_received`. La condición en el `WHERE` hace de candado en PostgreSQL (READ COMMITTED vuelve a evaluarla con la fila ya tomada) y en SQLite todo se serializa. Si no actualizó nada, se vuelve a leer para decidir entre idempotente, 409 o 404.
- **Una cancelada no recibe.** Sin esto, `confirm_purchase_receipt` recalculaba el estado de la orden de la recepción y una cancelada «resucitaba» como `partial` / `received`. Ahora `crear_recepcion`, `recibir_mercaderia` (después de buscar la clave: el reintento de una recepción ya hecha sigue contestando lo de la primera vez) y `confirmar_recepcion` (una recepción creada antes de cancelar) levantan `EstadoInvalido` → 409. `agregar_linea_orden` ya rechazaba todo menos `draft` / `sent`.
- **Una cancelada no cuenta como «en camino»** en la reposición (ya era así: `_ESTADOS_EN_CAMINO` no la incluye; ahora hay forma de llegar a ese estado). Ver ADR-056.
- **La guarda es la de escribir Compras:** ambas rutas llevan `OpcionesCompras.autorizar_escritura` (la misma `Depends(...)` de crear, agregar líneas y confirmar; sin ella, como siempre, pasa cualquiera que pase el gate del router). **No hay una costura nueva**: si un producto quisiera que enviar sea de otro rol que cancelar, es una opción más; hoy ninguno lo pide.

**Tests** (`tests/test_ordenes_enviar_cancelar.py`, SQLite y PostgreSQL): las transiciones y que no pierdan las líneas; `ordered_at`; la idempotencia de las dos; los 409 (enviar una cancelada o una recibida, cancelar una recibida y una parcial); el 404; la guarda; que una cancelada no reciba por ninguna de las tres rutas; y que no agregue líneas.

**No cubre.** No hay «reabrir» una cancelada. Enviar no avisa al proveedor por ningún medio (es un estado, no un mail). Una orden sin líneas se puede enviar.

**Cómo se adopta.** Subir el pin (v0.59.0). Sin migración. El kit suma los botones **Enviar** y **Cancelar** en el detalle de la compra (`CompraDetalle`).

## ADR-056 — Reposición: `contar_borradores` y `en_borrador` (2026-10-13)

**Problema.** Hasta v0.58 una orden de compra en `draft` restaba de lo sugerido como si ya estuviera pedida (ADR-017): no había forma de enviarla, así que la que ya se le había pedido al proveedor seguía en `draft`. Con `enviar_orden` (ADR-055) un borrador es un papel de trabajo todavía no mandado, y quien quiere ver qué falta pedir de verdad necesita poder dejar de restarlo. Decisión del humano, **opción C**: las dos cosas, con un interruptor.

**Decisión.**
- **Cada fila trae `en_borrador`:** lo pendiente (pedido menos recibido, sin bajar de 0 por línea) de las órdenes `draft`, aparte de `en_camino`. Siempre, con cualquiera de las dos opciones.
- **`contar_borradores: bool = True`** en `sugerencia_reposicion` y `?contar_borradores=` en `GET /api/reportes/reposicion` y su `/export`. Con `true` (el default) es **exactamente el comportamiento de siempre**: el borrador está en `en_camino` y resta; los tests de `test_reposicion.py` pasan sin tocarlos. Con `false`, `en_camino` sólo suma `sent` y `partial` (y `en_camino_sin_sucursal` lo sigue), los borradores no restan y se ven en `en_borrador`. Una `cancelled` no cuenta con ninguna de las dos.
- **El eco:** la respuesta repite `contar_borradores`, como `estacionalidad` y `descontar_por_vencer`, para que el kit sepa si el motor lo soporta (un motor viejo no lo devuelve).
- **`generar_ordenes_borrador` los sigue mirando siempre:** calcula con `contar_borradores=True` aunque la pantalla no los reste, así que un producto con un borrador abierto no vuelve a entrar en otra orden (generar dos veces con `false` no duplica; si el borrador se cancela, vuelve a pedirse). El parámetro se acepta en el cuerpo de `POST …/reposicion/ordenes` (la pantalla manda los mismos que ve) y no entra en la huella de la clave: no cambia lo que se crea.
- **El CSV** (`/export`) sale **como siempre** con `true` (ni una columna de más: tres tests comparan el encabezado) y, con `false`, suma `en_borrador` al final: es el caso en que los borradores no están en `en_camino` y hace falta verlos. `en_borrador` también es una columna ordenable (`ORDENES`).

**Tests** (`tests/test_borradores_en_la_reposicion.py`, SQLite y PostgreSQL): con `true` nada cambia y `en_borrador` aparece; con `false` un borrador de 10 no cambia el sugerido (8) y `en_borrador` = 10; enviadas y parciales cuentan con las dos; una mezcla; una cancelada no resta con ninguna; la sucursal; generar dos veces con `false` no duplica (ni con un borrador hecho a mano) y vuelve a pedir si se cancela; la huella; el router de generar; el eco, el 422, el orden y el CSV.

**Cómo se adopta.** Subir el pin (v0.59.0), sin migración. El kit suma la columna **En borrador** y el interruptor «Contar borradores como pedidos» en Reposición (detectando el motor por el eco del parámetro); VentaLibra decide el default de su pantalla.

## ADR-057 — Cuatro límites de ADR-018 cerrados: ajuste absoluto sin depósito, avisos con la fecha de la venta, `plan-salida` apagado y la carrera servicio / vencimiento (2026-10-13)

**1. Ajuste absoluto sin depósito → 422.** `ajustar_stock` sin `deposito_id` compara el total de **todos** los depósitos y escribe la diferencia en el depósito por defecto (límite de ADR-018, PR-3): con 10 en uno y 6 en otro, «fijar en 5» escribía −11 en el principal y dejaba el otro intacto. En `POST /api/stock/{pid}/ajuste`, con `OpcionesStock.por_deposito` y `modo="absoluto"` sin `deposito_id`, si hay **más de un depósito activo** responde **422** «Indicá el depósito: …». Con un solo depósito activo (un depósito dado de baja no cuenta) es igual que siempre; los otros modos (entrada, salida, merma) no cambian; sin `por_deposito` (Contalibra, Restolibra) tampoco. `erp.stock.ajustar_stock` no se toca (llamado directo conserva su comportamiento).

**2. Los avisos de una venta reabierta se miden en el día de la venta.** `GET /api/ventas/{id}` calculaba `avisos` contra el día de **hoy**: una venta de hace un mes con un lote que vencía a los diez días mostraba `lote_vencido` aunque estaba vigente al vender. `_agregar_avisos` recibe ahora `a_la_fecha_de_la_venta` y el detalle pasa el día de `ventas.fecha` (con o sin hora; si no se puede leer, cae a hoy). **`POST /api/ventas` no cambia** (la venta se acaba de hacer y el cajero necesita saber el estado de hoy, venga como venga la `fecha` del pedido): para una venta cargada con retraso, POST y GET difieren a propósito. Sigue fallando abierto (ADR-018, 2026-10-01).

**3. `POST /api/ventas/plan-salida` con la opción apagada es 404, no 405.** Sin la ruta, el path lo capturaba `GET /{vid}` y un `POST` daba 405. Ahora el router registra, sólo con `con_avisos_de_vencimiento` apagada, un `POST /plan-salida` que contesta `404 {"detail": "Not Found"}`, fuera de `/openapi.json` (que sigue sin mencionarla). `GET /plan-salida` sigue cayendo en `/{vid}` (422: no es un número), como hoy.

**4. Carrera «editar a servicio» vs «marcar vence».** Un servicio no puede estar marcado. El router lo rechazaba leyendo la marca **antes** de guardar, y `marcar_vence` leía el tipo antes de escribir: una edición a servicio concurrente con una marca dejaba un servicio marcado (límite de severidad media de ADR-018, 2026-09-30). Ahora: (a) `catalogo.update_producto` pone `tracks_expiry = 0` al pasar a servicio **dentro de su transacción** (si la base tiene la 0002); (b) `vencimientos.marcar_vence(…, True)` lleva la condición en el `UPDATE` (`… WHERE id = ? AND item_type <> 'service'`) y rechaza (`ReglaDeNegocio`, 409) si no actualizó nada. En PostgreSQL, si la edición todavía no confirmó, el `UPDATE` de la marca espera el candado de la fila y, al confirmar la edición, vuelve a evaluar el `WHERE` contra el servicio y no coincide. Desmarcar un servicio sigue pudiendo hacerse.

**Aceptado sin código** (se documenta, no se endurece): (i) las cantidades con más de ~15 dígitos significativos pueden perder una unidad al pasar por `float` (la columna y la API trabajan con `float`; no es un caso de comercio real); (ii) marcar o desmarcar `vence` **durante** una venta del mismo producto puede dejar esa venta con o sin lote (no pierde ni duplica stock; cerrarlo exige bloquear todos los productos de la venta, con costo para Contalibra y Restolibra); (iii) los caminos viejos sin FEFO (`usecases.sales.confirm_sale` / `cancel_sale`, `usecases.inventory.transfer_stock` directo, `add_movimiento_stock` con cantidad negativa sin lote, los scripts de migración) siguen escribiendo «sin lote»: lo dice la tabla de ADR-018 y quien los use tiene que saberlo.

**Tests.** `tests/test_ajuste_absoluto_sin_deposito.py`, `tests/test_avisos_fecha_de_la_venta.py` y `tests/test_servicio_y_vencimiento.py` (incluida la carrera con dos conexiones en PostgreSQL); en `test_fefo_devolucion_transferencia_ajuste.py` se invirtieron el 405 (ahora 404) y el «mismo aviso en el detalle».

**Cómo se adopta.** Subir el pin (v0.59.0), sin migración. El kit tiene que mandar `deposito_id` en el ajuste absoluto cuando hay varios depósitos (ya lo hace la pantalla por depósito).

## ADR-058 — El precio de venta por sucursal en la validación: `VentaPayload.sucursal_id` y `OpcionesVentas.sucursal_de_la_venta` (2026-10-13)

**Problema.** ADR-049 dejó anotado en «No cubre»: «el precio por sucursal (`item_prices.branch_id`): el POS no lo usa hoy y, por eso, el servidor tampoco. Si el POS empieza a mandar `sucursal_id`, hay que sumarle a la opción una costura que lo resuelva desde el depósito de la venta». El humano decidió hacerlo completo (2026-10-13); ésta es la parte del motor. La cotización ya aceptaba la sucursal (`POST /api/listas-precio/{id}/precios` y `GET …/precio` con `sucursal_id`, ADR-048): lo que faltaba era que la **validación** hiciera la misma cuenta.

**Qué ya existía.** `listas_precio.precios_por_producto(…, sucursal_id=)` y `resolve_price(…, branch_id=)`: entre los precios vigentes elige el **más específico** (el de la sucursal antes que el general, después el quiebre de cantidad más alto) y, si la sucursal no tiene precio propio, cae al general. `catalogo.get_deposito_de_venta` / `locations.branch_id` ligan un depósito a su sucursal. La venta **no persiste** su sucursal (`crear_venta` no escribe `sales.branch_id`; `margen._sucursal_de_las_ventas` la deduce del depósito).

**El riesgo de la transición.** Si el servidor pasara a validar contra la sucursal del depósito de golpe, un POS que todavía cotiza **sin** sucursal (una pantalla con el bundle viejo abierto en el navegador, o simplemente el kit sin actualizar) y vende desde un depósito de una sucursal con precio propio recibiría un 409; y su «re-preciar» volvería a cotizar el general: un 409 que no se cura sin recargar la pantalla. Se evaluaron tres salidas: (1) aceptar los dos precios (el de la sucursal y el general) mientras dure la transición — descartada: deja al cliente elegir el más barato siempre, que es justo lo que la validación quiere impedir; (2) una opción del producto que active todo, con default apagado — sirve para cerrar, pero obliga a coordinar el deploy del kit con el del producto al instante; (3) **que la validación se guíe por lo que el POS declaró**. Se eligió la 3, con la 2 como llave para cerrar.

**Decisión.**
- **`VentaPayload.sucursal_id: int | None = None`**: la sucursal por la que el POS cotizó. No se guarda, y se ignora si `validar_precios` está apagada (aparece, eso sí, como campo opcional del cuerpo en el OpenAPI de todos los productos). Un POS que la manda declara que sabe de precios por sucursal; uno que no (`None`, lo único que mandaban los POS anteriores) se valida **exactamente como hasta v0.58**: contra el precio general (`sucursal_id=None`).
- **`OpcionesVentas.sucursal_de_la_venta: (conn, payload) -> int | None`**, la costura. Default `sucursal_del_deposito`: la sucursal del depósito del que sale la venta (`locations.branch_id`; sin `deposito_id`, la del depósito por defecto; `None` si el depósito no tiene sucursal). **El servidor no le cree al pedido a qué sucursal pertenece su depósito:** la `sucursal_id` declarada solo *activa* la regla; el valor lo decide la costura. Un producto con otra regla (la sucursal del cajero, por ejemplo) pasa la suya.
- **`erp.precio_de_venta.validar_precios(conn, lineas, *, en="", sucursal_id=None)`** acepta la sucursal y la pasa a `precios_por_producto`. `_rechazo_por_precios` la obtiene de la costura **cuando el POS la declaró** (o cuando la opción de abajo está prendida).
- **`OpcionesVentas.exigir_precio_de_sucursal: bool = False`**: cierra la transición. Prendida, el servidor valida contra la sucursal de la venta **aunque el POS no la haya declarado**. Se prende cuando ningún POS viejo queda en pie; mientras está apagada, quien omite `sucursal_id` elige el precio general (como siempre pudo).
- **El 409** suma `sucursal_id` dentro del `detail` (la que usó el servidor) **sólo cuando se validó por sucursal**: sin sucursal el cuerpo es byte a byte el de antes (un test lo fija), y el kit puede mostrar o recotizar por esa sucursal.

**Los casos pedidos** (`tests/test_precio_de_venta_por_sucursal.py`, SQLite y PostgreSQL): (a) **sin precio de sucursal** nada cambia, declare o no la sucursal; (b) **con precio de sucursal y un POS que la manda**: 200 al precio de la sucursal, 409 al general (con el esperado y sin escribir nada); (c) **un depósito de otra sucursal** (o sin sucursal): rige el general, y un pedido que dice «Centro» pero vende desde un depósito de «Norte» se valida contra Norte; (d) **un POS viejo sin `sucursal_id`** vendiendo desde un depósito con precio propio: sigue validando contra el general, no se rompe (y tampoco puede colar el precio de la sucursal sin declararla). Además: `exigir_precio_de_sucursal`, la costura propia, el default de la costura, la ida y vuelta cotización → validación, el quiebre de cantidad conviviendo con el precio de sucursal, y las promociones.

**Promociones.** `erp.promociones.calcular` **no mira la sucursal**: trabaja con el precio de cada línea, que con la validación prendida es el ya validado (el de la sucursal si corresponde), y el ahorro (2x1, 3x2, combo) se calcula sobre ese precio — un 2x1 de un producto a 90 en Centro ahorra una unidad a 90. No hace falta pasarle la sucursal. Una promoción de combo tiene un precio cerrado propio, igual en todas las sucursales.

**No cubre.**
- **Las promociones no se limitan por sucursal** (`promotions` no tiene `branch_id`): una promoción vale en todas. Sería una columna y una revisión aparte.
- **La balanza por importe** (`etiqueta`) sigue valiendo lo que dice la etiqueta, estés en la sucursal que estés (ADR-049): la etiqueta manda sobre cualquier lista. Por peso, el precio por unidad de peso sí sale de la lista de la sucursal.
- La venta no guarda su sucursal (no se agregó `sales.branch_id`): se sigue deduciendo del depósito.
- La sincronización offline (LibraEdge) conserva el precio con el que se vendió sin conexión (ADR-049).
- Si `exigir_precio_de_sucursal` está apagada, un cliente armado a mano elige el precio general omitiendo `sucursal_id`: es la contrapartida de no romper a los POS viejos, y se cierra con esa opción.

**Cómo se adopta.** Subir el pin (v0.59.0), sin migración. **VentaLibra:** el POS manda `sucursal_id` en la cotización (que ya la aceptaba) **y** en `POST /api/ventas`, y maneja `sucursal_id` en el `detail` del 409; cuando todos los POS lo hacen, prende `exigir_precio_de_sucursal=True`. El kit suma el select de sucursal en el alta de un precio vigente («Nueva promoción»), que ya llama al endpoint con `sucursal_id`.

## ADR-059 — Las mermas se valorizan: el costo vigente queda en el movimiento (`stock_movements.unit_cost`) y hay un reporte de mermas por período (2026-10-14)

**Problema.** El motor sabía *cuánto* se mermó (un movimiento `waste` negativo) pero no *cuánto valía*: ninguna merma guardaba el costo, así que «cuánto perdimos en octubre» no tenía respuesta, ni la baja de un lote por vencimiento podía decirle al encargado lo que acababa de tirar. Pedido del humano (2026-10-14): guardar el costo vigente al mermar, por **todas** las vías, y poder leerlo en el historial, en la respuesta de la baja de un lote y en un reporte por período.

**Qué ya existía.** (1) `stock_movements.unit_cost NUMERIC` está en la baseline desde el scaffold (2026-07-25): la escribe `db.repository.append_stock_movement` para las entradas de compra (`usecases.purchasing`) y nadie más; ninguna merma la usó. (2) `sale_items.unit_cost_snapshot` y `OpcionesVentas.guardar_costo` (ADR-016): la misma idea para las ventas, **opt-in**, con `default_cost` vigente, `NULL` si el producto no tiene costo (un 0 significa «nadie lo cargó») y el costo del producto para una línea con variante (`item_variants` no tiene costo). (3) `margen_router` (`GET /api/reportes/margen`) como molde del reporte, y `web/margen_router._csv` para el export. (4) `add_movimiento_stock`: el único punto por el que pasa **toda** escritura de merma (el ajuste en modo «Merma» por `salida_manual`, la baja de un lote, la devolución de un perecedero y lo que un producto escriba directo).

**Decisión.**
- **Sin migración.** La columna ya existe en todas las bases (la baseline y `init_schema()` la crean), así que no hay revisión `0008`. Las mermas anteriores quedan con `unit_cost` NULL y **no se reconstruyen** (el costo de entonces no está en ningún lado); se leen como «sin costo». No hay chequeo de «falta la revisión» porque no hay revisión.
- **El costo se estampa en `add_movimiento_stock`**, con dos parámetros nuevos: `guardar_costo: bool = False` (un movimiento de tipo `merma` guarda `costo_vigente(conn, producto_id)`, o NULL si el `default_cost` es 0 o NULL) y `unit_cost` (fija el costo en vez de resolverlo). Es una **foto**: si el costo cambia después, la merma conserva el que tenía.
- **Opt-in, como ADR-016**: con `guardar_costo=False` (el default y lo único que mandan hoy Contalibra y Restolibra) el `INSERT` es carácter por carácter el de siempre y `unit_cost` queda NULL. Lo fija `tests/test_ledger_sin_marcar.py`, que **no se tocó** y sigue verde. Cada vía lo recibe: `salida_manual(..., guardar_costo)` (y `_salida_por_lote`, una fila por lote consumido), `devolver_items(..., guardar_costo)` (la `merma` del par de un perecedero devuelto) y, por HTTP, `OpcionesStock.con_costo_de_merma` (el ajuste en modo «Merma») y el `OpcionesVentas.guardar_costo` que ya existía (la devolución). **`dar_de_baja_lote` siempre estampa y devuelve el costo**: es de VentaLibra (Contalibra y Restolibra no montan los vencimientos) y su respuesta es nueva.
- **Lecturas.** (a) `get_movimientos_stock(..., con_costo=False)`: con `True`, cada fila trae `costo_unitario`, `costo_total` (`|cantidad| × costo`, 2 decimales) y `sin_costo`; sólo una merma los llena (`sin_costo=True` si no tiene costo), los demás tipos los traen en `None`. `GET /api/stock/movimientos` los pide con `OpcionesStock.con_costo_de_merma`. (b) `dar_de_baja_lote` / `POST /api/vencimientos/merma` devuelve `costo_unitario`, `costo_total` y `sin_costo`; un reintento (`repetida`) devuelve el costo que se guardó la primera vez, no el de hoy. (c) `erp.mermas.reporte_mermas(conn, desde, hasta, deposito_id=None, producto_id=None)` y `build_mermas_router` → `GET /api/reportes/mermas?desde&hasta[&deposito_id][&producto_id]` y `/export` (CSV): `{resumen, productos}`; por producto, `cantidad`, `costo_total`, `movimientos`, `movimientos_sin_costo`, `cantidad_sin_costo` y `sin_costo`. Sin fechas, el mes en curso. Un router aparte y opt-in en el montaje, como el margen.
- **Qué cuenta como merma en el reporte**: todo `waste` negativo, de cualquier origen, incluida la `merma` de la devolución de un perecedero (la mercadería volvió y se tiró: es plata perdida). No se separa por origen.
- **El importe es un piso, no un total.** Una merma sin costo no suma al `costo_total`: se cuenta aparte (`movimientos_sin_costo`, `cantidad_sin_costo`) para que la pantalla avise que el importe no está completo en vez de mostrarlo como si lo estuviera. Los importes se suman exactos (`Decimal`) y se redondean a 2 decimales al final.
- **La visibilidad por rol NO la decide el motor.** Los importes son costos del negocio. El motor los estampa y los devuelve; **el producto los filtra** (VentaLibra: capacidad `costos.ver`, encargado y admin; middleware `SinCostos`). Por eso todos los campos nuevos de importe llevan `costo` en el nombre (`costo_unitario`, `costo_total`, `sin_costo`, `movimientos_sin_costo`, `cantidad_sin_costo`: un test fija que casan `(^|_)(costos?|cost)($|_)`) y la ruta nueva cuelga de un prefijo propio, `/api/reportes/mermas`, que el producto puede cubrir entero.

**Alternativas descartadas.** *Siempre estampar* (sin opt-in): es más simple, pero cambia el ledger que Contalibra y Restolibra escriben hoy, rompe el contrato de `test_ledger_sin_marcar` y rompe la regla de la familia de que todo es aditivo y opt-in. *Una columna nueva (`unit_cost_snapshot`) con revisión*: la columna ya existía con el mismo sentido; agregar otra duplicaría. *Reconstruir las mermas viejas con el `default_cost` de hoy*: pondría un número que parece real y no lo es (el costo cambió); el margen ya mostró el problema (`costo_estimado`).

**Tests.** `tests/test_merma_valorizada.py` (SQLite y PostgreSQL): sin migración (la columna está en la baseline y la cabeza sigue siendo la `0007`); sin pedirlo la merma es la de siempre; el snapshot por cada vía (llamada directa, ajuste de un producto sin marcar, ajuste de un marcado con FEFO —una fila por lote—, el ajuste por HTTP prendido y apagado, la baja de un lote por función y por HTTP, la devolución de un perecedero por función y por HTTP con y sin `guardar_costo`); el costo que cambia después; sin costo → NULL y nunca 0 (y una variante toma el del producto); el reintento de la baja devuelve el costo viejo; el historial con y sin `con_costo`; el reporte (suma por producto, extremos inclusivos, por depósito y por producto, sólo mermas que restan, devolución y baja de lote incluidas, redondeo al final, entradas inválidas) y su router y CSV.

**No cubre.**
- Las mermas anteriores a esta versión: sin costo para siempre. Si el humano quiere un backfill estimado con el `default_cost` de hoy, es una decisión aparte (y marcada `costo_estimado`, como el margen).
- Una merma escrita por el camino hexagonal (`usecases` con `repo.append_stock_movement` y `StockMovementType.WASTE`): nadie lo hace hoy; si lo hiciera, pasa su `unit_cost` en el `StockMovement`.
- El reporte no separa el motivo (`Merma: <motivo>` viaja en la referencia, un texto libre) ni agrupa por período (sólo por producto).
- Un producto con receta (Restolibra): la merma es del ítem que se merma; el costo del plato por sus insumos no se calcula.

**Cómo se adopta.** Subir el pin (**v0.60.0**, menor). **Sin migración** (no hay `libracommerce-migrar upgrade` que correr por esto; el de la 0007, si falta, sigue siendo necesario). **VentaLibra:** `OPCIONES_DE_STOCK = OpcionesStock(por_deposito=True, con_costo_de_merma=True)`; ya manda `OpcionesVentas.guardar_costo=True`; monta `build_mermas_router(conexion=...)` con el gate del margen y suma `/api/reportes/mermas` a `PREFIJOS` de `SinCostos` (y confirma que `POST /api/vencimientos/merma` y `GET /api/stock/movimientos` pasan por el filtro por clave). **Contalibra y Restolibra:** no cambia nada si no lo piden. **Kit (libra-ui):** mostrar `costo_total` / `sin_costo` en la respuesta de la baja de un lote («Se dieron de baja 4 u · $240» o «sin costo cargado»), una columna Importe en el historial de movimientos del producto para las mermas (con «—» y un aviso si `sin_costo`) y una pantalla de Mermas por período (como Margen), con el aviso «el importe no incluye N mermas sin costo» cuando `resumen.sin_costo` es verdadero.

## ADR-060 — Conciliar el saldo «sin lote» negativo heredado de antes de A-4: `conciliacion_sin_lote` y `conciliar_sin_lote` (2026-10-14)

**Problema.** Antes de A-4 (2026-10-01) las salidas (ventas, devoluciones, transferencias, ajustes) descontaban del bucket «sin lote» aunque la mercadería estuviera en un lote (ADR-018). Quedó un saldo «sin lote» **negativo** y los lotes **sobreestimados** en las bases que ya venían vendiendo perecederos. `dar_de_baja_lote` se niega (409, `ReglaDeNegocio`) a mermar mientras el «sin lote» de ese depósito y variante sea negativo, y `proximos_a_vencer` los lista como `situacion='salidas_sin_lote'`; pero no había forma de *arreglarlo* salvo contar físicamente y ajustar. Pedido del humano (2026-10-14).

**Qué ya existía.** `asignar_vencimiento_a_saldo` (el par aditivo de ajustes `−sin lote` / `+lote`, misma referencia y fecha) como molde exacto, pero en el sentido contrario y para el saldo positivo; `lotes.plan_fefo` / `_orden_fefo` (el orden FEFO de la venta); `claves.marca` y `_movimientos_de_la_operacion` (la idempotencia por `clave_operacion`, única por producto); `_preparar_escritura` / `lotes.tomar_productos` (el candado del producto, ADR-018); `proximos_a_vencer(...)['sin_lote']` con `situacion='salidas_sin_lote'` (el listado de qué (producto, depósito, variante) tiene el negativo hoy: **no se agregó otro**).

**Decisión.**
- **`conciliacion_sin_lote(conn, producto_id, deposito_id, variante_id=None)` — lectura pura** (no escribe ni toma el producto). Con el «sin lote» en `-X`, devuelve `{producto_id, codigo, nombre, unidad, vence, deposito_id, variante_id, hoy, saldo_sin_lote, a_conciliar (= X, o 0), lotes, propuesta, cubierto, sin_cubrir}`. `propuesta` es un reparto **FEFO** (`plan_fefo`: vencidos primero, con fecha antes que sin fecha) entre los lotes con saldo > 0, con la forma `[{lote, vence, cantidad}]` del `reparto` de `conciliar_sin_lote` (se manda tal cual o editada); `sin_cubrir = X − cubierto` es lo que los lotes no alcanzan.
- **`conciliar_sin_lote(conn, producto_id, deposito_id, reparto, *, clave_operacion, variante_id=None, usuario_id=None, nota="", fecha="")`.** Por cada tramo, un **par aditivo** de `ajuste`: `+tramo` al «sin lote» y `−tramo` al lote; todas las filas con la misma referencia (`<nota> [conciliación xxxxxxxx]`, o `Conciliación sin lote [conciliación xxxxxxxx]`), la misma fecha (hoy en Argentina si no viene) y el mismo usuario. **Nunca un `UPDATE`**; el stock total del producto en el depósito **no cambia**. `sobre_existencias=True`: reclasifica lo que ya está, vale en un depósito dado de baja (ADR-051).
- **Lo que los lotes no cubren NO se toca.** Si el reparto suma menos que `X`, el resto del «sin lote» sigue negativo (y la guarda de `dar_de_baja_lote` sigue en pie): es un faltante real —mercadería que se vendió y no estaba en ningún lote—, que corrige un conteo físico (`ajustar_stock`), no una reclasificación.
- **Validaciones, todas antes de escribir la primera fila**: el reparto bien formado y sin lotes repetidos (`ValueError`, 422); producto que existe (404), es un producto con inventario y está marcado con `marcar_vence` (`ReglaDeNegocio`, 409, como `asignar`); hay saldo «sin lote» negativo (409); **cada lote cubre su tramo** (`SaldoInsuficiente`, 409; un lote que no existe tiene saldo 0); **la suma no supera `X`** (409). Variante que no es del producto y depósito inexistente, 422.
- **Idempotente por `clave_operacion`** (obligatoria; el mismo mecanismo que `asignar` y la baja: `[op:<clave>]` al final de la nota de cada fila, producto tomado antes de buscarla). La clave va **antes** de las validaciones de estado, así que un reintento de una conciliación hecha contesta `repetida: true` con lo de la primera vez aunque ya no quede negativo o el producto se haya desmarcado. Misma clave con otro reparto, otro depósito u otra operación (p. ej. la clave de un `asignar`) → `ClaveDeOperacionReusada` (409). El orden de los tramos en el reintento no importa.
- **Concurrencia (ADR-018).** `_preparar_escritura` toma el producto (el `UPDATE` de sí mismo) y recién después se leen los saldos: dos conciliaciones simultáneas del mismo producto se serializan y la segunda ve el negativo ya conciliado (no concilia dos veces); un test lo fija en PostgreSQL.
- **HTTP.** `POST /api/vencimientos/conciliar` en el router de **escritura**, bajo `dependencias_movimientos`. `GET /api/vencimientos/conciliacion?producto_id&deposito_id[&variante_id]` en el router de **lectura** (`build_vencimientos_router`), porque ese router sólo lee —un test existente fija que el de escritura sólo tiene `PUT`/`POST`—; como es la antesala de una escritura, la factory de lectura acepta `dependencias_conciliacion` (opcional) para ponerle a esa sola ruta el mismo gate que `dependencias_movimientos`. Sin ella, la ruta tiene el gate con el que el producto monte el router (el mismo que `GET /productos/{id}/lotes`, que muestra lo mismo).
- **El mensaje de la guarda de `dar_de_baja_lote` ahora nombra la salida** (`conciliar_sin_lote`), sin perder el texto que los tests y la pantalla ya leen.

**Tests.** `tests/test_conciliar_sin_lote.py` (SQLite y PostgreSQL): la vista previa FEFO (vencidos primero, lotes que no alcanzan, sin negativo, validaciones) y que no escribe; aplicar deja el «sin lote» en 0 con el total igual, las filas anteriores intactas y un par por tramo con la misma referencia, fecha y usuario; la conciliación parcial deja el resto negativo; lote sólo con código o sólo con vencimiento; depósitos y variantes por separado; idempotencia (reintento, orden de tramos, reintento sin nada por conciliar, clave reusada con otro reparto / otro depósito / `asignar`, misma clave en otro producto); un tramo mayor que su lote, una suma mayor que `X`, sin negativo, producto sin marcar / servicio / inexistente y un reparto mal formado, **sin escribir nada**; antes de conciliar la baja da 409 y después ya no (con conciliación parcial sigue dando 409); el reporte de próximos a vencer deja de listar el producto; la carrera en PostgreSQL; y el router (200, reintento, 404, 409, 422, el gate).

**No cubre.**
- Conciliar el negativo de un producto **sin marcar**: no hay lotes (409).
- Repartir entre depósitos: la conciliación es por (producto, depósito, variante), como la baja y `asignar`.
- Decidir *cuándo* conciliar o con qué lotes: la propuesta es FEFO, pero el reparto lo confirma una persona (la pantalla muestra la propuesta y deja editarla).
- El listado de qué tiene el negativo: ya lo da `GET /api/vencimientos` (`sin_lote`, `situacion='salidas_sin_lote'`).

**Cómo se adopta.** Subir el pin (**v0.60.0**, menor), **sin migración**. **VentaLibra:** montar el router de escritura como ya está (`conciliar` queda bajo `dependencias_movimientos`) y pasar `dependencias_conciliacion=[Depends(encargado_o_deposito)]` (las mismas de movimientos) al router de lectura. **Kit (libra-ui):** en la pantalla de Vencimientos, las filas de `sin_lote` con `situacion='salidas_sin_lote'` ofrecen «Conciliar»: abre un modal que llama a `GET /conciliacion` y muestra el negativo, los lotes con su saldo y la propuesta editable (una cantidad por lote, tope en el saldo del lote y total tope en `a_conciliar`); al confirmar, `POST /conciliar` con una `clave_operacion` nueva por intento (la misma si reintenta). Si `sin_cubrir > 0`, avisa que ese resto es un faltante y se corrige con un conteo físico.

## ADR-061 — Fiar sin cliente se rechaza por default: `OpcionesVentas.exigir_cliente_para_fiar=True` y `FiadoSinCliente` en el caso de uso (2026-10-16)

**Problema.** `POST /api/ventas` aceptaba (200) una venta con un pago en `cuenta_corriente` y `cliente_id: null`. La deuda queda a nombre de nadie: no aparece en ninguna cuenta corriente, no se puede cobrar y el libro de clientes no la ve. Lo midió el e2e de Contalibra (`frontend/e2e/cuenta-corriente.e2e.ts`, «una venta a cuenta corriente sin cliente se rechaza», que estaba en `test.fixme`): respuesta 200, venta `V-0000N` con `cliente_id: null`. La regla ya existía desde v0.16.2 (`OpcionesVentas.exigir_cliente_para_fiar`, 422 antes de escribir nada) pero **apagada por defecto**; sólo VentaLibra la prendía, así que Contalibra y Restolibra —que arman `OpcionesVentas` sin ella— quedaban expuestos. Era un opt-in para proteger el comportamiento de ellos, y lo que protegía era el defecto.

**Qué ya existía.** (1) La regla en el router (`web/ventas_router.py`, un `if` antes de `crear_venta_directa`) y su opción. (2) `devolver_items` ya exige cliente para devolver a cuenta corriente (`medio_pago == cuenta_corriente and cliente_id is None`). (3) `SinTurno`, `ProductoInexistente`, `DepositoNoPermitido`: el molde de «excepción de dominio que el router traduce y que se levanta antes de escribir nada». (4) `libracore.db.caja.MEDIOS_CUENTA_CORRIENTE`, las grafías de la marca de deuda.

**Decisión.**
- **El default pasa a `True`** (`OpcionesVentas.exigir_cliente_para_fiar: bool = True`). El comportamiento seguro es el que no hay que pedir; quien de verdad fía sin cliente lo dice a la vista con `exigir_cliente_para_fiar=False`. Se evaluó y se descartó dejarlo opt-in y que Contalibra y Restolibra la prendan: es exactamente lo que dejó el defecto en pie hasta hoy, y el próximo producto que monte `build_ventas_router` volvería a heredarlo.
- **La regla baja del router al caso de uso.** `registrar_venta` y `crear_venta_directa` suman `exigir_cliente_para_fiar: bool = True` y levantan **`FiadoSinCliente(ValueError)`** como **primera cosa** que hacen, antes de `validar_deposito`, de `SinTurno` y de cualquier escritura (mismo orden que tenía el router: el 422 del fiado va antes del 409 del turno). El router la traduce a 422 como `ProductoInexistente`; no se reintenta (es un dato del pedido). Así quien llama al caso de uso sin pasar por el router (una integración, un cobro armado a mano) tampoco deja una deuda a nadie. El `if` del router se retira: una sola fuente de la regla.
- **Qué cuenta como fiar:** al menos un pago cuyo medio sea `cuenta_corriente` (o `cuenta corriente`, sin mirar mayúsculas ni espacios) —un fiado parcial, con otro pago en efectivo, también se rechaza— y `cliente_id` vacío. La venta al contado sin cliente (consumidor final) no cambia.
- **El mensaje** sigue diciendo «No se puede fiar sin cliente» (VentaLibra lo fija en `test_cuenta_corriente.py`) y suma qué hacer: «Elegí uno o cobrala con otro medio».
- **Sin migración, sin cambio de contrato de respuesta:** lo que antes era 200 fiando a nadie ahora es 422; todo lo demás, byte a byte.

**Tests.** `tests/test_web_ventas.py` (SQLite y PostgreSQL): el default es `True`; fiar sin cliente da 422 «sin cliente» sin escribir nada (ni venta, ni líneas, ni pagos, ni caja, ni stock descontado); el fiado parcial también; con cliente se registra; sin fiado no hace falta cliente; el opt-out explícito `False` acepta; `True` explícito sigue rechazando. `tests/test_erp_ventas.py`: el caso de uso levanta `FiadoSinCliente` (un `ValueError`) y no deja nada, con cliente registra, con `exigir_cliente_para_fiar=False` registra, y se rechaza antes que la falta de turno. Se retiró `test_exigir_cliente_para_fiar_falso_por_default_permite`, que fijaba el defecto.

**No cubre.**
- **Un caso de uso propio de un producto que no pasa por `registrar_venta`.** Restolibra cobra una mesa con su propio `cobrar_pedido` (`app/db_cobro_pedido.py`, vía `POST /api/pedidos/{id}/cobrar`), que acepta un pago a `cuenta_corriente` y no mira el cliente del pedido: medido, con este default sigue dando 200 y `cliente_id: null`. Es de Restolibra (o de un `registrar_venta` que ese producto adopte); el motor no lo ve.
- **Contalibra, `POST /api/integraciones/consultas`** llama a `crear_venta_directa` con los pagos del emisor y sin cliente cuando la consulta no trae CUIT: con este default, una consulta con `medio_pago: cuenta_corriente` y sin CUIT levanta `FiadoSinCliente` y, sin capturarla, termina en 500. Hasta hoy entraba como deuda a nadie. El producto la tiene que traducir a 422 (o rechazar `cuenta_corriente` en `ConsultaPayload`); ningún emisor la manda hoy (MedLibra no ofrece cuenta corriente al cobrar).
- La **anulación** y el **cobro posterior** de una venta fiada: ya exigen o resuelven el cliente por sus propias reglas (ADR-027 de LibraCore).
- Ventas ya registradas con `cliente_id` nulo y pago a cuenta corriente: no se tocan ni se reasignan (si las hay, es una decisión aparte con el dueño de cada base).

**Cómo se adopta.** Cortar **v0.61.0** (menor: cambia un default) y subir el pin. **Sin migración.** **Contalibra y Restolibra:** el 422 llega solo (ninguna pantalla de ellos crea ventas por `POST /api/ventas`: `Ventas.tsx` sólo lista; lo usan la API y los e2e). Contalibra además captura `FiadoSinCliente` en `integraciones.py` (422) y saca el `test.fixme` del e2e. **VentaLibra:** no cambia nada; el `exigir_cliente_para_fiar=True` de `app/main.py` queda redundante (se puede dejar como documentación). **Un producto que fía sin cliente a propósito:** `OpcionesVentas(exigir_cliente_para_fiar=False)` y, si llama al caso de uso directo, `exigir_cliente_para_fiar=False`; hoy no hay ninguno en la familia.

## ADR-062 — El DDL del motor se corre de a un proceso por vez: `db.candado.serializar_esquema` en `init_schema()`, las tablas de cruce y `repuntar_fk_ventas_pagos` (2026-10-16)

**Problema.** El dueño decidió (2026-10-13) no prender más de un worker hasta medir la creación idempotente del esquema. ADR-053 de libracore arregló `init_core_schema()` (deadlock sobre una base armada, claves duplicadas del catálogo sobre una vacía) y dejó dicho que el `init_schema()` de este motor «tiene el mismo patrón de DDL idempotente y **no se midió**». Se midió, con el mismo reproductor (procesos de verdad que arrancan a la vez, cita común) y con el camino de arranque de cada producto.

**Medido** (2 procesos simultáneos, 30 corridas por fila, una base nueva por corrida; «falló» = el proceso salió con error; PostgreSQL 16 local, libracore v1.168.0):

| Camino de arranque | Base vacía, antes | Base vacía, ahora | Base armada, antes | Base armada, ahora |
|---|---|---|---|---|
| Reproductor sobre `init_core_schema()` **sin** el candado del motor (control: reproduce ADR-053) | 30/60 `duplicate key` (29 `pg_type_typname_nsp_index`, 1 `pg_class_relname_nsp_index`) | — | 17/60 `deadlock detected` | — |
| `init_schema()` solo (LibraDesk, LibraClub, primer paso de VentaLibra) | **30/60** `duplicate key … pg_type_typname_nsp_index` (una por corrida: 30 de 30 corridas rojas) | 0/60 | 0/60 | 0/60 |
| VentaLibra: `init_schema()` y después `init_core_schema()` + tablas de cruce + repuntado, cada una con su conexión | **30/60** `duplicate key` | 0/60 | 0/60 | 0/60 |
| Contalibra/Restolibra: `init_core_schema()` + `init_schema()` + repuntado + semilla + tablas de cruce, **una** conexión | 0/60 | 0/60 | 0/60 | 0/60 |
| Tablas de cruce (`crear_venta_links`, `crear_cliente_lista_precio`, `crear_promociones`) + `repuntar_fk_ventas_pagos` sobre una base con los dos motores y sin las tablas | — | — | **24/60** `duplicate key … pg_type_typname_nsp_index` | 0/60 |
| Con el arreglo **y sin** el candado del motor | 30/60 (Contalibra y VentaLibra) | — | 5/60 `deadlock` (Contalibra), 1/60 (VentaLibra) | — |

Todas las corridas sin error dejaron la base idéntica a la esperada (mismas tablas, una sola FK de `ventas_pagos` a `sales`, un solo depósito, `schema_migrations` completa). De punta a punta, `uvicorn app.asgi:app --workers 2` de **VentaLibra** contra una base nueva: con la base ya migrada (la cadena del compose: `libracore-migrar`, `libraauth-migrar`, `libracommerce-migrar`, `alembic upgrade head`) 0 de 8 corridas fallan antes y después; con una base a la que sólo se le aplicó LibraCore y LibraAuth, **12 de 12** corridas tenían a un worker muerto con `UniqueViolation … (typname)=(parties)` (uvicorn lo reinicia y el segundo intento anda, pero queda el traceback y un arranque doble).

**Causa.** La de ADR-053, en otra función. `CREATE TABLE IF NOT EXISTS` no es atómico en PostgreSQL: dos transacciones que lo corren juntas sobre una tabla que no existe pasan las dos el «si no existe» y chocan al insertar el tipo/la relación en el catálogo. `repuntar_fk_ventas_pagos` es un «si no apunta a `sales`, repuntar» (`SELECT` de `pg_constraint` + `ALTER`) con la misma ventana. Sobre una base armada `init_schema()` **no** falla (medido: 0/60): no escribe nada, sólo mira, así que no hay `ROW EXCLUSIVE` que se cruce con un `SHARE` como en el core. Contalibra y Restolibra no lo sufrían por casualidad de composición: `init_core_schema()` toma su candado, no hace `commit`, y `init_schema()` corre dentro de esa misma transacción hasta el `commit` de `run_migrations()`; además envuelven todo `init_db()` en `una_vez_al_arrancar`. VentaLibra, LibraDesk y LibraClub llaman a `init_schema()` sin nada que lo cubra.

**Qué ya existía** (se miró antes): `libracore.candados` (ADR-044) y `esperar_candado_de_transaccion` (ADR-053, v1.168.0), la costura que el motor expone para «una transacción larga que dos procesos no pueden correr a la vez». Este repo ya tenía sus propios `pg_advisory_xact_lock` (`erp/compras.py` y `erp/reposicion_ordenes.py`, con la misma clave SHA-256 de 56 bits que luego generalizó el motor): son candados de **negocio** por clave y no se tocan; esto es otra cosa (el DDL).

**Decisión.**
- **`libracommerce/db/candado.py`**: `CANDADO_DEL_ESQUEMA = "libracommerce.esquema"` y `serializar_esquema(conn)`, que **llama a `libracore.candados.esperar_candado_de_transaccion`** y no reimplementa nada (ni la clave, ni el reintento, ni el tope de 120 s). Sobre `sqlite3.Connection` devuelve `False` **sin importar libracore** (SQLite serializa a sus escritores solo, y el núcleo sigue con `dependencies = []`); sobre cualquier otra conexión importa `libracore.candados` tarde —como ya hace `db/migrations.py`— y, si el libracore instalado es anterior a v1.168.0, levanta un `ImportError` que dice qué instalar en lugar de seguir sin candado en silencio.
- **Primera sentencia** de `init_schema()` (se suelta con el `commit` de `run_migrations()`, el del final), de `crear_venta_links`, `crear_cliente_lista_precio`, `crear_promociones` y de la rama PostgreSQL de `repuntar_fk_ventas_pagos`. Un solo nombre para todo el DDL del motor: las funciones corren una detrás de otra en la misma conexión y tomarlo de nuevo no espera (los candados asesores son reentrantes por sesión).
- **Distinto nombre que el del motor** (`init_core_schema`), a propósito: no son la misma sección crítica. Un producto toma siempre los dos en el mismo orden y no hay espera circular entre procesos del mismo producto; el candado del motor sigue siendo necesario (medido: sin él, con este arreglo, Contalibra y VentaLibra dan 30/60 sobre base vacía y 5/60 y 1/60 de `deadlock` sobre una armada).
- **Un candado dentro de la función y no una costura que cada producto envuelva**, por el mismo motivo que ADR-053: el defecto está en la función, y un producto que no la envuelva (hoy VentaLibra, LibraDesk y LibraClub no lo hacen) lo reintroduce.
- **No cambia ninguna tabla, columna ni índice**: `test_schema_congelado` sigue verde con las dos fixtures sin tocar. Es una guarda de ejecución.
- `pyproject.toml`: los extras `migrations` y `erp` piden `libracore>=1.168` (antes 1.148 y 1.137); `uv.lock` regenerado. Los cinco consumidores ya están en v1.170.0.

**Tests.** `tests/test_esquema_en_paralelo.py` (se saltea sin `LIBRACORE_POSTGRES_URL`; el subproceso es `tests/esquema_en_paralelo_proceso.py`, que hace 4 vueltas sobre 4 bases distintas con 2 procesos a la vez): `init_schema()` sobre bases vacías, el arranque de producto como VentaLibra sobre bases vacías, y las tablas de cruce sobre bases con los dos motores; más, por función, el mecanismo sin carreras (con el candado tomado por otra conexión la función no avanza hasta que ésa termina: `init_schema`, las tres `crear_*` y `repuntar_fk_ventas_pagos`), SQLite sin importar libracore y el `ImportError` con un motor sin la API. Sin las llamadas a `serializar_esquema` fallan 8 de los 10; con ellas pasan los 10.

**No cubre.**
- **El DDL propio de cada producto**, que corre en el mismo arranque: en VentaLibra `app/db.py::connect()` → `init_schema_propio` (`users`, `sequences`, `party_billing`, `party_roles`, `sale_mp_orders`) dio los 11 de 12 arranques rojos que quedaron con este arreglo puesto, y con ese bloque dentro de un `una_vez_al_arrancar` dio 0 de 15 (sin el arreglo de acá y con ese `una_vez_al_arrancar`: 15 de 15 rojos por `parties`). Cada producto serializa lo suyo; el motor ya expone `una_vez_al_arrancar`.
- Las semillas «si no existe, crear» (el depósito por defecto, la caja por defecto): son del producto y van en `una_vez_al_arrancar`, ver `docs/varios-procesos.md` del motor.
- Dos migraciones de Alembic corridas a la vez (`libracommerce-migrar upgrade` desde dos contenedores): `init_schema()` toma el candado a mitad de la transacción de Alembic, así que no protege lo que Alembic ya tocó. En el compose corre una sola vez por contenedor, antes de `uvicorn`.

**Cómo se adopta.** Subir el pin (**v0.62.0**, menor: módulo nuevo y piso de libracore), **sin migración**. Contalibra, Restolibra y VentaLibra (v0.60.0): nada que tocar en el código. **LibraDesk y LibraClub tienen el pin en v0.16.4**: no reciben este arreglo hasta que lo suban (el salto es grande; la suite de cada uno decide). Con un solo worker nadie nota nada (el candado se toma sin espera).

## ADR-063 — La venta anota su sucursal y una empresa de un solo local adopta la suya: `registrar_venta`, `adoptar_sucursal_unica`, `completar_sucursal_de_ventas` y `OpcionesDepositos.sucursal_por_defecto` (2026-10-16)

**Problema.** El motor ya tiene la jerarquía sucursal → depósitos (ADR-012/013), pero un producto de un solo local (Contalibra) la deja en NULL entera: sus depósitos no tienen sucursal, y **ninguna venta escribe `sales.branch_id`** en ningún producto (`crear_venta` no lo escribía; sólo lo llena `db.repository.save_sale` con un `Sale` del dominio). La sucursal de una venta se *deducía* cada vez del depósito del ledger (`erp.margen._sucursal_de_las_ventas`). El día que un cliente pida multisucursal habría que migrar datos con el cliente mirando. Plan: `wiki/analyses/plan-sucursales-contalibra-2026-10-16.md`, F0 (motor) y F1 (Contalibra). Decisiones del humano (2026-10-16): la sucursal única se llama «Casa central»; las ventas históricas se completan desde el depósito con el criterio de `margen`; la multisucursal visible (F2) no se expone; al cotizar gana el precio de la sucursal (lo que ya hace `resolve_price`, no se toca).

**Qué ya existía.** `branches` con predeterminada única (`idx_branches_one_default`) y `default_location_id`; `create_sucursal`/`set_default_sucursal`/`_asegurar_deposito_predeterminado`; `locations.branch_id` y `sales.branch_id` (columnas sueltas, sin FK); `erp.margen._sucursal_de_las_ventas` (el criterio: `sales.branch_id` si lo trae, si no `MIN(locations.branch_id)` del ledger `movement_type='sale'`, `source_type IN ('venta','sale')`); `web.ventas_router.sucursal_del_deposito` (la sucursal del depósito efectivo, para validar precios); `db.candado.serializar_esquema` (ADR-062); el molde de un producto que adopta sucursales (`ventalibra/app/sucursales_migracion.py`).

**Decisión.**
- **La venta nace con su sucursal.** `registrar_venta` pasa `sucursal_id=catalogo.sucursal_del_deposito(conn, deposito_id)` a `crear_venta`, que lo escribe en `sales.branch_id`. El depósito efectivo es `deposito_id` o, sin él, el depósito por defecto (`get_default_deposito_id`): **el mismo que ya usaba `sucursal_del_deposito` del router de ventas, que ahora delega en la función del motor** (una sola fuente). NULL si el depósito no tiene sucursal, no existe o no hay ninguno. Se anota aunque la venta no descuente stock (`stock_habilitado=False`): es la sucursal del depósito de la venta, no del movimiento. `crear_venta(..., sucursal_id=None)` es aditivo: con `None` el `INSERT` es carácter por carácter el de siempre. VentaLibra también la escribe desde este cambio (la deducía del depósito; el valor es el mismo).
- **`catalogo.SALIDA_DE_VENTA_SQL`**: el criterio del ledger («la salida de una venta») en un solo texto, que usan `margen` (su `JOIN`) y `completar_sucursal_de_ventas`. Un test fija que lo escrito es lo que `margen` ya deducía.
- **`catalogo.adoptar_sucursal_unica(conn, nombre="Casa central") -> int`.** Sin sucursales: crea la predeterminada con el depósito por defecto como `default_location_id` (NULL si no hay depósito: el primero que se cree lo ocupa) y le asigna todos los depósitos sin sucursal, también los inactivos. Con una predeterminada: no crea otra, devuelve su id y sólo asigna huérfanos. **Con sucursales y ninguna predeterminada: `ValueError`** (`create_sucursal` no marca la predeterminada, así que una base multisucursal puede estar así; elegir una por el dueño de los datos no es de esta función). Idempotente. **No toca `cajas`** (es de LibraCore) **ni las ventas**. No commitea.
- **Serializada con `serializar_esquema(conn)` como primera sentencia.** No es DDL, pero es un «si no existe, crear» sobre el índice único de la predeterminada: dos procesos que arrancan juntos (dos workers, dos contenedores) chocarían con `UniqueViolation … idx_branches_one_default`. El candado se suelta con el `commit` del caller; el segundo espera y encuentra la sucursal. Se evaluó y se descartó atrapar la `UniqueViolation` y reintentar: en PostgreSQL deja la transacción abortada y el caller (una revisión de Alembic, un `init_db`) tendría que saberlo. Reusa el nombre del candado del esquema (reentrante: dentro de `init_db`, que ya lo tiene, no espera).
- **`catalogo.completar_sucursal_de_ventas(conn) -> int`.** Un `UPDATE` de las ventas con `branch_id IS NULL` cuyo ledger de salida toca un depósito con sucursal: `MIN` de esas sucursales (el desempate de `margen`). Una venta que no movió stock o cuyo depósito no tiene sucursal queda en NULL; lo que la venta ya declara no se pisa. Devuelve cuántas actualizó; idempotente. **Una sola pasada** (`UPDATE … FROM` de un `GROUP BY` del ledger) y no una subconsulta correlacionada por venta: medido sobre una base de ensayo de 100.000 ventas y 195.000 movimientos, la correlacionada no terminó en 7 minutos (una lectura del ledger por venta, sin índice por `source_id`) y la de una pasada tarda ~0,2 s (la revisión entera de Contalibra, 2 s). Pide SQLite ≥ 3.33 (`UPDATE … FROM`).
- **`OpcionesDepositos.sucursal_por_defecto` (opt-in, `False`).** `POST /api/depositos` sin `branch_id` usa `get_default_sucursal_id`; quien manda `branch_id` lo conserva; sin predeterminada queda sin sucursal. Apagada, el alta es la de siempre.
- **Sin migración de esquema** (las columnas ya existen): `test_schema_congelado` no se mueve.

**Tests.** `tests/test_sucursal_unica.py` (SQLite y PostgreSQL): la venta sin sucursales → NULL, con sucursal → su id, con depósito explícito, sin depósito, sin stock; `crear_venta` aditivo; adoptar en base vacía, con huérfanos (activos e inactivos), con una predeterminada que ya existe (y lo ajeno intacto), dos veces, sin ningún depósito, con sucursales sin predeterminada (se niega sin escribir), sin tocar `cajas`; **dos procesos y seis procesos a la vez en PostgreSQL** (una sola sucursal, sin error: sin el candado fallan con `idx_branches_one_default`); `sucursal_por_defecto` (cae, respeta la que manda el pedido, sin predeterminada, apagada); completar las históricas (con y sin depósito, idempotente, igual a lo que deducía `margen`, no pisa lo declarado, desempate por la menor, depósito sin sucursal). `tests/test_reposicion.py` dejaba dicho que la venta no guardaba la sucursal: ahora afirma que la guarda y el reporte da lo mismo.

**No cubre.**
- **Las cajas** (`cajas.sucursal_id`): son de LibraCore; las asigna el producto (Contalibra, en su revisión de Alembic).
- **Ventas por caminos que no pasan por `registrar_venta`** (un producto que llama a `crear_venta` directo sin `sucursal_id`, p. ej. el cobro de pedido de Restolibra): siguen en NULL. Se pueden completar después con `completar_sucursal_de_ventas`.
- **Un depósito con `branch_id` que apunta a una sucursal que no existe** (columnas sueltas, sin FK): no es «sin sucursal» y `adoptar_sucursal_unica` no lo toca.
- **Un depósito que cambia de sucursal después**: las ventas ya anotadas conservan la sucursal del momento (es lo que se quiere: la historia no se reescribe).
- **F3/F4** (precio por sucursal al cotizar, filtros de reportes, punto de venta por sucursal): siguen como en el plan.

**Cómo se adopta.** Subir el pin (**v0.63.0**, menor: función nueva, opción nueva, la venta escribe una columna que antes dejaba en NULL), **sin migración**. **Un producto de un solo local** (Contalibra): en una revisión de Alembic propia y en su `init_db`, `adoptar_sucursal_unica(conn, "<nombre>")`, asignar las cajas sin sucursal a la que devuelve y `completar_sucursal_de_ventas(conn)`; y montar `build_depositos_router(opciones=OpcionesDepositos(sucursal_por_defecto=True))`. Con una sola sucursal no cambia nada de lo que ve un cliente: el kit sólo ofrece sucursal con dos o más. Único cambio de conducta: los depósitos pasan a tener las guardas de la jerarquía (`_verificar_no_es_el_ultimo_deposito`); el depósito por defecto ya no se podía desactivar ni borrar, así que en la práctica no se nota. **Restolibra, VentaLibra, LibraDesk y LibraClub:** nada que tocar (VentaLibra y LibraDesk ya tienen sucursales; la venta de VentaLibra ahora anota la que antes se deducía).

## ADR-064 — Anular una venta no devuelve dos veces lo que una nota de crédito ya devolvió o dejó a favor: `descontar_reintegro_de_notas` en `anular_venta` (2026-10-17)

**Problema.** Desde libracore 1.176 (ADR-065 del motor) la nota de crédito de una factura **al contado** ya cobrada mueve la plata al emitirse: el que emite elige «devolver» (un egreso de caja con `referencia = nc:factura:<id>:<nota>` y `factura_id`) o «dejar a favor» (un abono en la cuenta corriente del cliente, con la misma marca). `anular_venta` no lo sabía: revertía con `revertir_cobro_venta` **un egreso por cada pago acreditado, por su monto entero**. Nota que devolvió 200 + anular la venta de 200 = **400 fuera de la caja** (y, con «a favor», el cliente con 200 en su cuenta *y* 200 devueltos en el cajón). Es la hermana del defecto que el ADR-032 cerró para la cuenta corriente a crédito (`cc_acreditada_por_nota`: «anular una venta no acredita la misma deuda por segunda vez»); el motor lo dejó dicho en `notas_de_credito.devuelto_por_notas`: *quien anula la venta de esa factura tiene que restarlo de lo que revierte de la caja*.

**Qué ya existía.** `libracore.notas_de_credito.cc_acreditado_por_notas(conn, factura_id)` (lo abonado a la cuenta con la marca de la nota) y, desde 1.176, `devuelto_por_notas(conn, factura_id)` (los egresos con la marca, sin los anulados). `anular_venta` ya usaba la primera como booleano (`cc_ya_acreditada`) para no acreditar la deuda de un pago fiado dos veces. Se extiende ese mismo criterio; no se inventa otro.

**Decisión.**
- **`erp.ventas.descontar_reintegro_de_notas(pagos, devuelto_por_medio, a_favor) -> (pagos, a_favor_sin_caja)`** (pura, sin base): resta de los pagos a revertir lo que las notas ya reintegraron, **sólo de los pagos que fueron plata de la caja**. El pago a `cuenta_corriente` nunca se toca: no entró al cajón y su deuda tiene su propio criterio.
- **Criterio de a qué pago se descuenta**, en este orden: (1) lo **devuelto por caja** sale del pago **del mismo medio** con el que se devolvió (la nota devolvió en tarjeta: el efectivo sigue en el cajón y es lo único que sale al anular; se compara con `medios_pago.canonico`, así `tarjeta` y `tarjeta_credito` son el mismo medio); (2) lo que no cupo en su medio, lo devuelto sin medio conocido y lo **dejado a favor** salen de los demás pagos de caja **en el orden en que se cobraron** (por id). Un pago que queda en cero no se revierte (no hay egreso por 0); uno que queda en parte se revierte por el resto.
- **Lo «a favor» que no cabe en la caja es deuda ya abonada.** Lo dejado a favor se imputa primero a lo cobrado en caja; si sobra, es el abono de una factura a cuenta corriente y vale el criterio de siempre: `cc_ya_acreditada = True` (la anulación no acredita otra vez la deuda del pago fiado). Con la venta toda fiada pasa lo de antes; con una venta mixta (120 en efectivo + 80 fiados, nota que deja a favor los 120) el efectivo no se revierte de nuevo y la deuda fiada **sí** se acredita.
- **`_devuelto_por_notas(conn, factura_id)`**: el total sale de `devuelto_por_notas` y el reparto por medio, de los mismos egresos (marca + `factura_id` + `sql_no_anulado()`); si no suman lo mismo, la diferencia queda como «sin medio». Un egreso de la nota que se anuló no cuenta: esa plata volvió a la caja y la anulación de la venta la saca como siempre.
- **Compatibilidad con un libracore anterior a 1.176.** `devuelto_por_notas` no existe y las notas de una factura al contado **no movían plata**: el import es tolerante (`ImportError` → no hay nada que restar) y `anular_venta` se comporta como siempre. `cc_acreditado_por_notas` ya está en el piso del extra `erp` (>=1.168). **El piso de `pyproject.toml` no sube**: el arreglo es inerte hasta que el producto tenga libracore 1.176.
- **Sin migración, sin cambio de firma**: `anular_venta` devuelve y levanta lo mismo; sólo cambia cuánto revierte de la caja cuando hay notas que movieron plata.

**Tests.** `tests/test_anular_venta_con_notas_que_devolvieron.py` (SQLite y PostgreSQL; los movimientos se siembran con `reintegrar_devolucion`, que es lo que escribe `reintegro_de_nota.aplicar`; se saltean con libracore < 1.176): nota total que devolvió → anular no saca de nuevo; nota parcial que devolvió → saca sólo el resto; varias notas; devuelto por tarjeta con la venta cobrada en efectivo y tarjeta; lo que no cupo en su medio; egreso de la nota anulado; nota que dejó a favor (total, parcial, y venta mixta con fiado); y lo que no cambia (nota sin destino, reintegro de otra factura, libracore sin `devuelto_por_notas`); más la cuenta pura. Rojos sin el cambio (20 de 28 corridas: todos los de comportamiento; los 8 de «no cambia» pasan en los dos lados).

**No cubre.**
- **Una nota «a favor» sobre una factura a cuenta corriente mixta** (parte efectivo, parte fiado): el motor la trata como crédito (`condicion_venta == "Cuenta Corriente"` abona *todo* el importe en la cuenta, también la parte que entró en caja). La imputación «primero a la caja» deja ese caso sin doble egreso, pero la cuenta del cliente es del motor.
- **Pagos que ya se anularon a mano** o una nota emitida **sin CAE** (no mueve plata, no resta nada).
- **Revertir la devolución de la nota** si después se anula la nota: no existe (una nota autorizada no se deshace).

**Cómo se adopta.** Subir el pin (**v0.64.0**, menor: función nueva y `anular_venta` revierte menos cuando una nota ya devolvió), **sin migración**. Hace efecto cuando el producto tenga libracore >= 1.176 (con uno anterior es inerte, sin romper nada). Los productos que anulan ventas con `erp.ventas.anular_venta` (Contalibra, Restolibra, VentaLibra): nada que tocar en el código.

## ADR-065 — `libracommerce.hora` toma el reloj de `libracore.fechas`: un solo «hoy» y «ahora» de Argentina en la familia (2026-10-18)

**Problema.** LibraCore v1.182.0 (ADR-070 del motor, ítem 14d de la cola autónoma) dejó **una** fuente de «hoy» y «ahora» de Argentina: `libracore.fechas.hoy()`, `ahora()`, `ZONA_AR` (UTC-3 fijo) y `_ahora_utc()`, lo único que lee el reloj del sistema y lo que un test pisa. Este motor tenía la suya: `libracommerce/hora.py` (ADR-052) con su propio `ZONA_LOCAL` y su propio `ahora_utc()`. La regla era la misma, así que no daban días distintos, pero eran **dos**: fijar el instante de LibraCore con `fechas._ahora_utc` no movía las vigencias de precio, las promociones ni los vencimientos de este motor, y un cambio de regla (huso, reloj) había que hacerlo en dos lugares.

**Relevado (`git grep "datetime.now\|date.today\|timezone(timedelta\|utcnow"` sobre `develop` v0.64.0).** El relevo previo (ADR-052, 2026-10-13) ya había cerrado los `datetime.now()` sin zona de `resolve_price`, `set_precio_vigente`, `promociones` y `set_setting`: no queda ninguno en el paquete. Lo que quedaba:
- `hora.py`: `ZONA_LOCAL = timezone(timedelta(hours=-3))` y `datetime.now(UTC)`, una segunda definición del huso y del reloj.
- `web/compras_router.py` (dos veces) y `erp/precio_de_venta.py`: `datetime.now(UTC)` suelto, que el guard toleraba («lleva zona») pero que ningún test podía fijar.

**Qué ya existía.** `hora.py` y su gancho `ahora_utc` (ADR-052): `ahora_local`, `hoy_local`, `vencimientos.hoy_argentina`, `listas_precio.a_hora_local` y `_ZONA_LOCAL` ya salen de ahí. No hay que tocar ninguno de sus llamadores.

**Decisión.**
- **`hora.py` es el adaptador de `libracore.fechas`**: `ZONA_LOCAL = fechas.ZONA_AR` y `ahora_utc() = fechas.ahora().astimezone(UTC)`. Se usa la API pública (`ahora()`), no `_ahora_utc`; el instante sigue saliendo de `_ahora_utc`, así que pisarlo mueve todo el motor. `ahora_utc` se queda como gancho: los tests de ADR-052 (`monkeypatch.setattr(hora, "ahora_utc", ...)`) siguen valiendo sin tocarlos.
- **El núcleo sigue sin depender de LibraCore** (ADR-030, `dependencies = []`; `db/repository.py` importa `hora`): `import libracore.fechas` es opcional. Sin LibraCore, o con uno anterior a 1.182 (sin `fechas.ahora`), `hora` cae a la misma regla (UTC-3 fijo, su propia lectura de `datetime.now(UTC)`). Es el **único** `datetime.now` que queda, y está en `EXCEPCIONES` del guard con su motivo.
- **El piso sube a `libracore>=1.182`** en los extras `erp` y `migrations` (los de un producto): con ellos el reloj es siempre el de LibraCore, nunca el respaldo. `web` queda en `>=1.125`: lo trae el producto con `erp`. El pin del extra `dev` pasa a `v1.182.0` (con `uv lock --upgrade-package libracore`; sin migración).
- **`compras_router` (recibir y confirmar) y `precio_de_venta` leen `hora.ahora_utc()`** en vez de `datetime.now(UTC)`: el **mismo valor y el mismo formato** (instante con zona UTC, `...+00:00`), pero ahora del reloj único.
- **El guard (`test_sin_reloj_del_sistema`) se aprieta:** marca todo `.now(...)`, también con zona. Antes dejaba pasar `datetime.now(UTC)` («no depende de la zona del proceso»): cierto, pero era justo la lectura que un test no podía fijar.

**Lo que NO se cambió: el `occurred_at` de la recepción de mercadería.** `compras_router` estampa la recepción (`received_at`) y sus movimientos de stock (`occurred_at`) como un **instante UTC con zona** (`2026-10-18T02:30:00+00:00`), mientras que el resto de los movimientos (`erp.stock.add_movimiento_stock`, ajustes, transferencias) se estampa como el **día de Argentina sin zona** (`2026-10-17T00:00:00`). Alinearlos tocaría datos y reportes, así que queda como estaba, a propósito:
- El reporte por día leía `substr(occurred_at, 1, 10)` (`erp.actividad`, `erp.mermas`, el listado de movimientos de `erp.stock`) y `reposicion` leía `str(occurred_at)[:10]`. Una recepción confirmada entre las 21:00 y las 24:00 de Argentina quedaba con el día **siguiente** (UTC): hoy se contaba mañana. **Resuelto en ADR-066** sin tocar el formato: se sigue guardando el instante en UTC y los reportes por día calculan la fecha en hora de Argentina (`hora.dia_local` / `hora.dia_local_sql`).
- Pasar a hora local sin zona **cambia el contrato de `received_at`** que la API le devuelve al cliente (de `...+00:00` a naive), el orden de `ORDER BY occurred_at` contra las filas ya guardadas con offset (el texto no ordena igual entre `+00:00` y naive) y a qué día cae esa recepción en los reportes de períodos ya cerrados. Las filas que ya están no se reescriben solas. Por eso ADR-066 eligió no cambiarlo.
- El formato sigue siendo una decisión aparte, con dato y con los consumidores de la API a avisar (quien lea `received_at`). Queda abierta; cuando se tome, el único lugar a cambiar es `compras_router` (leer `hora.ahora_local()`) y el assert del formato en `tests/test_un_solo_reloj.py`.

**Tests.** `tests/test_un_solo_reloj.py`: con `TZ=UTC` (`time.tzset()`) y `libracore.fechas._ahora_utc` pisada a las 02:30Z del 18/10/2026 (23:30 del 17 en Argentina): «hoy» y «ahora» del motor, `hoy_argentina`, el precio vigente sin `desde`, `resolve_price` sin `at` (vigencia hasta 23:45), la promoción creada y evaluada (vigente de 23:00 a 23:45), el ajuste de stock sin fecha, el `hoy` de los vencimientos, y la recepción (`received_at` y movimiento con el instante de `fechas`, formato `+00:00`). Más: `ZONA_LOCAL` es `fechas.ZONA_AR`, pisar `hora.ahora_utc` sigue valiendo y, sin LibraCore, `hora` anda con la misma regla. Rojos sin el cambio (8 de 10; los otros dos fijan que pisar `hora.ahora_utc` y el reloj real ya andaban). `test_sin_reloj_del_sistema` suma los casos con zona al guard y también se pone rojo.

**No cubre.**
- Las filas ya escritas con la hora del sistema (ADR-052) o con offset (recepciones) no se corrigen.
- Los tests que fijan sus fixtures con `date.today()` (`HOY`) siguen igual; el guard recorre sólo el paquete.

**Cómo se adopta.** Subir el pin (**v0.65.0**, menor: el piso de `libracore` sube a 1.182). Sin migración; sin cambio de valores para quien despliega con `TZ` de Argentina, ni para quien corre en UTC (la regla ya era UTC-3 fija).

## ADR-066 — Los reportes por día cuentan la recepción de mercadería en el día de Argentina: `hora.dia_local`, `dia_local_sql` y `filtro_de_dias_sql` (2026-10-18)

**Problema.** La recepción de mercadería (`compras_router`, recibir y confirmar) estampa `received_at` y el `occurred_at` de sus movimientos de stock como un **instante UTC con zona** (`2026-10-18T02:30:00+00:00`); el resto de los movimientos son **hora de Argentina sin zona** (`2026-10-17T00:00:00`). Los reportes por día cortaban el día con `substr(occurred_at, 1, 10)` o `str(occurred_at)[:10]`, así que una recepción confirmada entre las 21 y las 24 de Argentina contaba **al día siguiente**. Era el defecto que ADR-065 dejó medido en `tests/test_un_solo_reloj.py` (23:30 AR del 17 → del 18).

**Decisión del humano (2026-10-18).** Se sigue guardando el instante en UTC: **no cambia** `received_at` de la API ni ningún dato guardado. Lo que cambia es cómo los reportes por día calculan la fecha: en hora de Argentina.

**Relevado (`git grep "substr(.*occurred_at\|occurred_at)\[:10\]\|\[:10\]\|received_at"` sobre `develop`).** Cortan el día de un `occurred_at` de `stock_movements`: `erp.actividad` (`PARTE_STOCK_COMERCIO`, la `fecha` de la línea de tiempo), `erp.mermas.reporte_mermas` (el rango), `erp.stock.get_movimientos_stock` (el rango y la `fecha` de cada fila) y `erp.reposicion.sugerencia_reposicion` (el día de cada movimiento de la ventana). `received_at` no se corta por día en el motor: sólo se serializa. Los demás `[:10]` son de `sales.occurred_on` (las ventas, que se estampan en hora local) o de fechas de entrada, y no se tocan.

**Qué ya existía.** `libracommerce.hora` (ADR-052, ADR-065) con `ZONA_LOCAL` (= `libracore.fechas.ZONA_AR`, UTC-3 fijo). `libracore.fechas` no tiene una función que lleve un texto guardado al día de Argentina en SQL; la familia ya resolvía «el día» por texto en `margen.rango_por_dia` (para `sales.occurred_on`, que no tiene zona).

**Decisión.**
- **Una sola regla, en `hora.py`** (el núcleo; sin importar LibraCore):
  - `dia_local(valor) -> 'AAAA-MM-DD' | None`: con zona (`+00:00`, `Z`, cualquier `±HH:MM`, o un `datetime` con `tzinfo`), el día de ese instante en `ZONA_LOCAL`; sin zona, su fecha tal cual (**no cambia el día de ninguna fila sin zona**); un texto ilegible, sus 10 primeros caracteres como antes.
  - `dia_local_sql(conn, columna)`: la misma regla en SQL, un `CASE` cuya condición («más de 19 caracteres y termina en `Z` o en `±HH:MM`») es SQL común. La conversión depende del motor (por el tipo de `conn`, como `db.candado`): SQLite `date(instante, '-180 minutes')` (`date()` ya lleva a UTC un texto con offset); PostgreSQL `to_char((CAST(instante AS timestamptz) AT TIME ZONE 'UTC') + interval '-180 minutes', 'YYYY-MM-DD')`, sin `date(...)` porque el traductor de LibraCore lo reescribe. En los dos se descartan las fracciones de segundo antes de convertir (SQLite redondea a milisegundos: las `02:59:59.9999Z` serían las 03:00Z, otro día). El desfase sale de `ZONA_LOCAL`. `dia_local_sql_de_motor(columna, postgres=...)` es lo mismo para una parte de SQL armada sin conexión.
  - `filtro_de_dias_sql(conn, columna, desde, hasta)`: las condiciones de «día de Argentina entre `desde` y `hasta`», inclusive.
- **Los cuatro lugares la usan.** `actividad` arma su parte de stock para cada motor (`PARTE_STOCK_COMERCIO` sigue siendo la de SQLite; `PARTE_STOCK_COMERCIO_PG` la de PostgreSQL) y `partes_de_comercio(conn=None)` elige por la conexión (sin ella, por `libracore.db.core.is_postgres()`). `mermas` y `get_movimientos_stock` filtran con `filtro_de_dias_sql` y dan la `fecha` con `dia_local_sql`. `reposicion` usa `dia_local` y **descarta** el movimiento cuyo día de Argentina es anterior a la ventana aunque su texto UTC sea `>= desde` (si no, se restaba del saldo de partida sin volver a sumarse en ningún día).

**Rendimiento.** `stock_movements.occurred_at` **no tiene índice** (el único índice de la tabla es `(item_id, location_id, lot_code)`), y el filtro de antes (`substr(occurred_at, 1, 10) >= ?`) ya barría la tabla: una función sobre la columna no usa índice. Con el `CASE` sigue igual de barrido, así que no hay regresión. `filtro_de_dias_sql` suma además una **cota cruda** sobre la columna (`occurred_at >= 'desde'` y `occurred_at < 'hasta + 2 días'`, que toda fila del rango cumple con zona o sin ella), que una base con un índice sobre `occurred_at` resuelve por rango antes del `CASE`. `reposicion` ya filtraba por `occurred_at >= ?` crudo y sigue igual. El costo extra del `CASE` es por fila leída, sin consultas de más.

**Tests.** `tests/test_dia_de_argentina.py` (SQLite y PostgreSQL): `dia_local` con los dos formatos, con `Z`, con otra zona, la medianoche de Argentina (02:59:59.999999Z → el día anterior; 03:00Z → el mismo), cambio de mes, de año y 29 de febrero; `dia_local_sql` da exactamente lo mismo que `dia_local` en los dos motores (y `NULL` → `NULL`); `filtro_de_dias_sql` con los bordes; y `reporte_mermas` y `get_movimientos_stock` con mermas con y sin zona. `tests/test_un_solo_reloj.py`: el test de la recepción deja de documentar el defecto y fija lo correcto: la recepción de las 23:30 AR del 17 se guarda igual (`2026-10-18T02:30:00+00:00`) y es del **17** en el listado de movimientos, en la actividad y en la reposición. Rojos sin el cambio.

**No cubre.**
- **El formato guardado** (instante con zona contra hora local): sigue siendo la decisión abierta de ADR-065. Tampoco cambia el `ORDER BY occurred_at` de los listados, que mezcla los dos textos (dentro de un mismo día puede ordenar distinto que el instante).
- **Los productos que cortan el día por su cuenta.** Restolibra (`app/db_recetas.py`) filtra `substr(m.occurred_at, 1, 10)` sobre `stock_movements`; con el pin nuevo puede usar `hora.filtro_de_dias_sql`. Un producto que muestre `received_at` como fecha (LibraDesk, `services/compras.py`, lo devuelve en ISO) puede usar `hora.dia_local`.

**Cómo se adopta.** Subir el pin (**v0.66.0**, menor: funciones nuevas en `hora` y los reportes por día cambian el día de las recepciones de 21 a 24 AR). Sin migración y sin cambio de API.

## ADR-067 — Las ventas de la capa ERP en un nodo offline: ganchos `al_registrar_venta`, `al_acreditar_pago` y `al_devolver_items`, `erp.offline.con_outbox`, cuatro operaciones y sus aplicadores (2026-10-19)

**Problema.** El humano decidió llevar LibraEdge a toda la suite; la fase 1 es «vender sin internet en RestoLibra, VentaLibra y ContaLibra», que venden por `erp.ventas` (`wiki/analyses/libraedge-toda-la-suite-2026-10-18.md`, pieza A5/E2). Lo único que había era el piloto de RestoLibra (`pedido.cobrado`, sólo ventas cobradas, sin anulaciones ni devoluciones) y `integrations/libraedge` (ADR-005), sobre la capa vieja `Sale`, que no usaba nadie. `devolver_items` no tenía gancho, una venta parcial no subía nunca, y los números `V-`/`POS-` de dos sucursales chocan en el central.

**Decisiones del humano que aplica.** 4: numeración por nodo con **prefijo de sucursal** (`S02-POS-000123`). 5: **gancho al registrar cualquier venta**, más un evento de pago acreditado, para que la venta parcial suba y se complete. Y las recomendadas de la fase 2 que este trabajo necesita: anulaciones y devoluciones **como eventos**, y **prohibido anular una venta facturada sin conexión**.

**Qué ya existía.** En libraedge (v0.7.0): `OutboxOperation`, `NodeRepository.next_sequence(commit=False)` y `enqueue_operation(commit=False)` (encolar en la transacción del producto), `SyncReceiver` (deduplica por `operation_id`; un `ValueError` del manejador va a revisión manual y el nodo recibe `rejected`), el `Despachador` por `(operation_type, schema_version)` de ADR-007 de libraedge (un tipo sin manejador va a revisión, no se marca aplicado) y el archivo de estado del proceso del nodo (`libraedge.nodo.EstadoNodo`, `en_linea` sale del resultado real de sincronizar). En este motor: `al_confirmar_venta`, `al_anular_venta` y `numerador`; `devolver_items` con `clave_operacion` (ADR-041). En RestoLibra: el aplicador con la colisión de número a revisión manual (no dejarla llegar al `UNIQUE`, que el receptor leería como duplicado), que se trae tal cual.

**Decisión.**
- **Tres ganchos nuevos en `Hooks`**, no-op por default, con la misma conexión y dentro de la transacción:
  - `al_registrar_venta(conn, venta)`: al final de `registrar_venta`, para **toda** venta (cobrada, parcial, pendiente), antes de `al_confirmar_venta`. Con el default ni se lee la venta.
  - `al_acreditar_pago(conn, venta, pagos)`: en `acreditar_pago_qr`, después de acreditar y recalcular el estado; `pagos` son las filas que esta llamada pasó a `aprobado`, con `payment_id`. Antes de `al_confirmar_venta`.
  - `al_devolver_items(conn, venta, devolucion)`: al final de `devolver_items` (`lineas`, `deposito_id`, `medio_pago`, `importe`, `clave_operacion`, `usuario_id`); un reintento `repetida` no lo llama.
- **Cuatro operaciones** (`aggregate_type = "venta"`, `aggregate_id = "<node_id>:venta:<id local>"`, `operation_id = "<node_id>:<secuencia>"`, `schema_version = 1`): `venta.registrada` (la venta entera: ítems con su costo, pagos con su estado, promociones, sucursal, el ledger que descontó —receta y lotes ya resueltos— y `extra` del producto), `venta.pago_acreditado` (los pagos por posición), `venta.anulada` y `venta.devolucion` (las líneas por posición, el importe). **No hay `venta.cobrada`**: una venta que nace cobrada es una `venta.registrada` con ese estado, y encolar también en `al_confirmar_venta` la subiría dos veces. Un hecho por cosa que pasó.
- **`erp.offline.con_outbox(hooks, Outbox(nodo, en_linea, extra_de_venta))`** compone sin pisar: cada gancho corre el del producto y después encola; el numerador antepone `NodoLocal.prefijo` al del producto. Fuera de un nodo (`nodo()` es `None`) es exactamente `hooks`, así que el producto lo arma igual en las dos puntas. `nodo_desde_entorno(var_id, var_prefijo)` y `en_linea_segun_estado(ruta, vigencia)` son las implementaciones de referencia (E3 traerá `nodo_actual()` a libraedge: se enchufa en `Outbox.nodo`).
- **Anular lo facturado sin conexión** levanta `hooks.RequiereConexion` (un `ValueError`) desde el `al_anular_venta` compuesto, adentro de la transacción; el router la contesta 409. «Sin conexión» es lo que dice el archivo de estado del proceso del nodo: `en_linea` **y** una sincronización buena hace menos de `vigencia` (5 min); sin archivo o viejo, sin conexión. El default de `Outbox.en_linea` es `False`: sin cablearlo, un nodo nunca anula una venta facturada.
- **Aplicadores del central** `(conn, operacion) -> None`, uno por tipo: `registrar_en(despachador, hooks, al_materializar=...)` los registra en un `Despachador` de libraedge v0.7.0 en `SCHEMA_VERSION` (el camino recomendado); `aplicadores(...)` los da como `{tipo: función}` para cualquier otro despachador, y `despachar(...)` los junta en un solo `operation_handler` (la API vieja de un manejador por router) que **rechaza** un tipo desconocido:
  - `venta.registrada` crea la venta con `crear_venta`, `agregar_pago` (el fiado entra al libro de clientes como en el nodo) y la caja de lo acreditado **sin turno** (el turno es del nodo), y repite el ledger del nodo con `sobre_existencias`. Marca la venta con `source_type = 'libraedge:<node_id>'` y `source_id = <id local>`: es su clave de idempotencia por agregado, **sin tabla nueva ni migración**. Un número que ya tiene otra venta es `ColisionDeNumero` (revisión manual).
  - `venta.pago_acreditado` llama a `acreditar_pago_qr` (idempotente por la condición) y verifica que lo acreditado y el estado coincidan con el nodo; `venta.anulada` llama a `anular_venta` (no-op si ya estaba); `venta.devolucion` llama a `devolver_items` con una `clave_operacion` sacada del `operation_id` (`repetida` al reaplicar) y verifica el importe. Una operación sobre una venta que el central no tiene es `VentaNoSincronizada` (revisión manual detrás de la suya). Los tres usan los `hooks` del central (`cliente_cc_de`).
- **`siguiente_numero` ignora las ventas que subieron de un nodo** (`source_type NOT LIKE 'libraedge:%'`): sin eso el `V-` del central saltaba al número del nodo. En una base sin nodos devuelve la misma fila de siempre.
- **`integrations/libraedge` se retira** (y `tests/test_offline_sync.py`): verificado con `git grep` en los ocho productos, nadie la importa. ADR-005 queda reemplazada.
- **El extra `offline`/`offline-server` se pinea a `v0.7.0`** (el último tag de libraedge, con el `Despachador`), no a `@develop`.

**Tests.** `tests/test_erp_offline.py`, con dos bases (nodo y central) y el `SyncReceiver` de verdad con el `Despachador` (`registrar_en`), en tres combinaciones (las dos SQLite; el nodo en PostgreSQL; el central en PostgreSQL): la venta parcial sube y el central la completa con el QR (una sola venta, la caja de los dos pagos, el mismo stock); reintentos duplicados y reaplicación directa sin efecto; devolución y anulación dejan el central igual que el nodo; la colisión de número se rechaza; una operación sin su venta se rechaza; el central no numera desde las ventas del nodo; anular una venta facturada sin conexión levanta `RequiereConexion` y no deja nada, y con conexión anula y sube; un gancho del producto que falla se lleva venta, stock y outbox; `extra` viaja y `al_materializar` lo recibe. Más las piezas puras (composición, prefijo, entorno, archivo de estado, despachador, clave). `tests/test_web_ventas.py`: `RequiereConexion` es 409 y no anula. `tests/test_erp_hooks.py`: los defaults nuevos.

**No cubre.**
- **Caja y turnos del nodo** (A6): la caja del central se escribe sin turno y el turno del nodo viaja sólo como dato (`turno_id_nodo`).
- **Los cobros de cuenta corriente** (recibos de LibraCore): no pasan por `erp.ventas`; el fiado sube con la venta, el cobro posterior no.
- **Facturar en el nodo y la bandeja al volver** (E4): la factura del nodo no sube; anular en el central una venta que ahí ya tiene CAE va a revisión manual (`VentaConFacturaCAE`).
- **Quién anuló**: `al_anular_venta` recibe la venta y no el usuario, así que el central anula sin usuario.
- **Dos nodos con el mismo prefijo, o una PC reemplazada que reinicia la numeración:** chocan; lo atrapa `ColisionDeNumero`, no lo evita. El prefijo tiene que ser único por nodo, o la numeración nueva tiene que arrancar después de la vieja.
- **El cobro de un pedido de RestoLibra** (`cobrar_pedido`) no pasa por `registrar_venta`: hasta que migre (E5) no llama a `al_registrar_venta`.

**Cómo se adopta** (el próximo tag, v0.67.0: menor, ganchos y módulo nuevos, sin migración). En el producto: `GANCHOS = con_outbox(GANCHOS_DEL_PRODUCTO, Outbox(nodo=..., en_linea=en_linea_segun_estado(ruta), extra_de_venta=...))`, el mismo en el nodo y en el central; en el central, `create_sync_router(abrir, despachador=registrar_en(Despachador(), GANCHOS))`, sumando al mismo despachador los tipos propios del producto. RestoLibra, al migrar, deja de encolar `pedido.cobrado` desde `al_confirmar_venta` (sino la venta sube dos veces) y manda el pedido en `extra`.

## ADR-068 — `venta.facturada`: la factura que emite un nodo offline sube al central y se vincula a su venta (2026-10-19)

**Problema.** Fase 1 de LibraEdge, pieza E4b. El humano decidió que el nodo factura cuando tiene internet, con el ticket de ARCA que le presta el central (ADR-074 de libracore, libracore #418; ADR-009 de libraedge). La factura nace en la base del nodo con su número y CAE, y ADR-067 dejaba anotado que no subía: el central quedaba con la venta y sin la factura (libro IVA, cobros, reportes) y una anulación posterior en el central no sabía que hacía falta la nota de crédito.

**Qué ya existía.** En libracore #418: `PuertoDeVentas.al_facturar(venta, factura)` (corre después de vincular la factura, **fuera** de la transacción de la venta; un error se registra y no se propaga), `facturas_del_nodo.payload_factura(factura)` (la factura en JSON, con `version`) y `aplicar_factura_del_nodo(payload, conn=)` (la escribe en el central con el mismo número y CAE, idempotente por la clave ante ARCA, `FacturaEnConflicto` si choca). En este motor: `con_outbox`, `encolar`, los aplicadores de ADR-067, `vincular_factura` y `vincular_cobros_de_venta`.

**Decisión.**
- **Quinta operación, `venta.facturada`** (mismo agregado que la venta, `schema_version = 1`), con `{"venta_id", "numero", "factura": payload_factura(factura)}`.
- **Se encola con `al_facturar_con_outbox(outbox, abrir_conexion, base=None)`**, que el producto pone en `PuertoDeVentas.al_facturar`. Como ese gancho corre fuera de la transacción de la venta, el encolado abre su conexión y confirma. Fuera de un nodo sólo corre `base`.
- **Aplicador `aplicar_venta_facturada`**: escribe la factura con `aplicar_factura_del_nodo` (inyectable: `registrar_en(..., aplicar_factura=)`), la vincula a la venta del central y marca los cobros de caja que creó `venta.registrada` como cobro de esa factura. Idempotente: si la venta ya tiene esa factura no cambia nada. Si tiene **otra**, `VentaConOtraFactura` (revisión manual); sin la venta, `VentaNoSincronizada`.
- **libracore se importa recién al usarlo**: este motor pide `libracore>=1.182` y `facturas_del_nodo` todavía no está en un tag. Sin ella, encolar levanta `RuntimeError` y aplicar levanta `ValueError` (revisión manual, se reintenta con `libraedge-central revision reintentar` cuando el central tenga el motor al día).
- **Reconciliación en el nodo**: `al_facturar` corre después de emitir y libracore no lo reintenta si falla, así que una factura podía quedar en el nodo para siempre. `reconciliar_ventas_facturadas(conn, outbox, payload_factura=None)` encola `venta.facturada` para cada venta con factura **con CAE** que ya subió (`venta.registrada` en el outbox) y no tiene su `venta.facturada` en el outbox en ningún estado; devuelve los ids. `reconciliador(outbox, abrir_conexion)` la envuelve en su propia transacción, para llamarla al arrancar el producto y en cada vuelta de la facturación al volver. `al_facturar_con_outbox` también mira el outbox antes de encolar. Un duplicado por carrera (dos workers) es inofensivo: el aplicador es idempotente.
- **El extra `offline`/`offline-server` se pinea a libraedge `v0.9.0`** (ADR-008 y ADR-009 de libraedge: aprovisionamiento genérico y ticket de ARCA prestado).
- **`en_linea_segun_estado` se queda acá**: la de `libracore.nodo_offline` hace lo mismo y es la que tiene que quedar, pero no está en un tag. Pasa a importarse de libracore (y se sube el piso de `libracore`) cuando se corte el tag de #418.

**Tests.** `tests/test_erp_offline.py`, en las tres combinaciones de motores, con dobles de `payload_factura` y `aplicar_factura_del_nodo`: la factura sube, queda vinculada con sus cobros y reaplicar no duplica; venta con otra factura en el central y factura de una venta que no subió, a revisión; fuera de un nodo no encola; sin `facturas_del_nodo`, revisión manual en vez de `ImportError`; falla `al_facturar` y la reconciliación la encola una sola vez (ni una segunda pasada, ni un `al_facturar` tardío, ni una vez enviada la duplican) y sube; la reconciliación no toca ventas sin CAE, sin factura o que nunca subieron. Verificado además a mano contra libracore #418 instalado (sin dobles), en las tres combinaciones.

**No cubre.** Notas de crédito y débito emitidas en el nodo: no hay operación todavía.

**Cómo se adopta.** En el nodo: `PuertoDeVentas(..., al_facturar=al_facturar_con_outbox(OUTBOX, get_connection), numerar_comprobante=numerador_con_ta(cliente_de_ticket.para_numerador(sin_conexion=SinConexion)))` y `reconciliar = reconciliador(OUTBOX, get_connection)`, llamado al arrancar y en cada vuelta de `arrancar_facturacion_al_volver`. En el central: nada nuevo si ya usa `registrar_en` (registra los cinco tipos). Requiere libracore con ADR-074 en las dos puntas.

## ADR-069 — La caja y los turnos del nodo suben al central: `caja_con_outbox`, cinco operaciones más y sus aplicadores (2026-10-21)

**Problema.** Fase 2 de LibraEdge. Un nodo vende y factura sin conexión (ADR-067, ADR-068) pero abre y cierra la caja sin que el central se entere: turnos, ingresos y egresos a mano, anulaciones y cierre del día quedan sólo en su base. El humano decidió el 2026-10-21 que **la caja y los turnos de caja del nodo suben al central** (apertura, cierre, ingresos, egresos y retiros, anulaciones, cierre del día y arqueos). El motor de caja (libracore v1.188.0, ADR-076) dejó los cinco ganchos `GanchosDeCaja` y las columnas `origen`/`origen_id` con su índice único parcial; faltaba la traducción al outbox y la vuelta en el central. Es la misma tarea que ADR-067 hizo con las ventas.

**Qué ya existía.** `erp.offline` (`con_outbox`, `Outbox`, `NodoLocal`, `registrar_en`, los aplicadores y su patrón de idempotencia), `SyncReceiver` y el `Despachador` de libraedge, y desde libraedge v0.17.0 (ADR-020) el orden causal del outbox: un fallo de transporte corta la tanda y un rechazo definitivo (`rejected`, revisión manual) no traba la cola. En libracore: `GanchosDeCaja`, `turnos.get_turno_de_origen`, `caja.get_movimiento_de_origen` y `cierre_diario.get_cierre_de_origen`.

**Decisión.**
- **Módulo nuevo `erp.offline_caja`** (en vez de seguir engordando `erp.offline`, que ya tiene 850 líneas de ventas). `erp.offline.aplicadores()` suma sus cinco aplicadores, así que `registrar_en(Despachador(), hooks)` registra **los diez tipos** con la misma línea de siempre.
- **`caja_con_outbox(ganchos, outbox)`** devuelve un `GanchosDeCaja` que corre el gancho del producto y después encola, en la transacción del cambio (`next_sequence(commit=False)` y `enqueue_operation(commit=False)`). Fuera de un nodo (`outbox.nodo()` es `None`) es el `ganchos` que recibió. Usa el `Outbox` de las ventas, así que el nodo, el `node_id` y la secuencia son los mismos: **una sola secuencia por nodo**, y el orden `turno.abierto` → ventas y movimientos → `turno.cerrado` → `dia.cerrado` es el de los hechos.
- **Cinco operaciones**, `schema_version = 1`, con el payload de `libracore.ganchos_de_caja` tal cual (`version: 1`; este módulo no lo toca): `turno.abierto` y `turno.cerrado` (`aggregate_type = "turno"`, `<node_id>:turno:<id>`), `caja.movimiento` y `caja.movimiento_anulado` (`"caja_movimiento"`, `<node_id>:caja_movimiento:<id>`), `dia.cerrado` (`"cierre_diario"`, `<node_id>:cierre_diario:<id>`).
- **Aplicadores del central**, idempotentes con la pregunta que dejó el motor (`get_*_de_origen`) y con `origen = node_id` y `origen_id` = el id del nodo (así lo define ADR-076; distinto del `source_type = 'libraedge:<node_id>'` de las ventas, que es otra columna): `turno.abierto` → `create_turno(apertura=)`; `turno.cerrado` → `cerrar_turno_caja(cierre=, monto_esperado=)`; `caja.movimiento` → `create_caja_movimiento(turno_id=<el del central>)`; `caja.movimiento_anulado` → `anular_caja_movimiento`; `dia.cerrado` → `cerrar_dia`. Todo `conn=`: en la transacción del receptor.
- **Traducción de ids.** `turno_id` al del central con `get_turno_de_origen`. `caja_id` y `usuario_id` **tal cual**: las cajas y los usuarios son datos de referencia que el nodo espeja con la misma clave (`cajas` y `usuarios` están en `TABLAS_DE_REFERENCIA` de VentaLibra; se verificó), y una caja que no está en el central es `CajaNoSincronizada`. `factura_id` y `cc_pago_id` de un movimiento **no se copian** (quedan en `NULL`): son ids de otra base, y copiarlos ataría el movimiento a otra factura y, siendo un ingreso con factura, lo llevaría al libro de clientes.
- **Lo que falta o choca es rechazo, no reintento.** Un movimiento, una anulación, un cierre del día cuyo turno o movimiento no subió: `TurnoNoSincronizado` o `MovimientoNoSincronizado`. Lo que el motor de caja del central no acepta (abrir un turno en un día ya cerrado, cerrar el día con turnos abiertos o ya cerrado): el motor levanta `RuntimeError`, que el receptor no lee como rechazo sino como falla (la tanda se corta y nada sale detrás de ella), así que **se traduce a `CajaRechazadaPorElCentral`**. Todas son `ValueError`: revisión manual en el central, `manual_review` en el nodo y la cola sigue (ADR-020 de libraedge). Reintentarlas, una vez resuelta la que faltaba, en el orden en que se crearon. Un `caja.movimiento` cuya referencia ya existe en el central también se rechaza: `create_caja_movimiento` la descarta por repetida (no hay fila nueva) y darla por aplicada sería perder el movimiento.
- **`turno.cerrado` se basta solo**: su payload es el de la apertura más el arqueo, así que si el turno no está lo crea (un turno abierto antes de actualizar el nodo, o con su `turno.abierto` en revisión) y lo cierra.
- **El arqueo del nodo manda; el central lo recalcula y lo marca.** El esperado es el que vio el cajero: se guarda tal cual. El central lo recalcula con lo que tiene (`get_resumen_turno_caja`) y, si difiere en más de medio centavo, agrega `[REVISAR ARQUEO] ...` al final de las `notas` del turno y un `logger.warning`; no rechaza. Igual con los totales de un cierre diario (el central cierra con la foto de sus turnos). `arqueos_a_revisar(conn)` los lista. Se marca en las `notas` y no en una tabla para no tener migración. El número correlativo del cierre lo asigna cada base y puede diferir (el central ya cerraba días antes del nodo): sólo se registra, no se marca.
- **Venta y turno (`venta.registrada`).** El central traduce `turno_id_nodo` al turno que subió: ata la venta (`venta_links`) y, si el producto escribe el turno en la caja de la venta, también su cobro. Para eso `Outbox` suma `caja_con_turno` (default `False`, el comportamiento de hoy; VentaLibra lo prende, igual que lo pasa a `registrar_venta`) y viaja como `caja_con_turno` en `venta.registrada` y `venta.pago_acreditado` (aditivo: un payload sin él es `False`); el pago acreditado lo reproduce con `acreditar_pago_qr(caja_con_turno=)`. **Si el turno no está en el central —la venta sin turno, o un turno que no subió—, la venta se aplica sin turno, como antes**: rechazarla dejaría sin subir todas las ventas de un turno por un problema de la caja.
- **Sin duplicados.** Los ganchos de caja se pasan **por llamada**, no globales, y este motor no se los pasa a nada: `registrar_venta`, `anular_venta`, `devolver_items` y `acreditar_pago_qr` llaman a `create_caja_movimiento` sin `ganchos=`. Los movimientos que nacen de una venta los reproduce el central al aplicar la venta; sólo suben los que anota una persona en los routers de caja. Verificado con `git grep` y con un test (la venta del nodo deja dos movimientos y sube uno solo).
- **Pisos y pines.** `libracore>=1.188` en los extras `migrations` y `erp` (usan `GanchosDeCaja` y los `get_*_de_origen`), pin de la suite a v1.188.0, y los extras `offline` y `offline-server` a libraedge **v0.17.0** (el orden causal del outbox es lo que garantiza que el turno llegue antes que sus movimientos). Sin migración propia; la `0028` de libracore es aditiva.

**Tests.** `tests/test_erp_offline_caja.py`, con el nodo y el central de verdad, el `SyncReceiver` y el `Despachador`, en las tres combinaciones de motores y con los ids del central desplazados a propósito: el encolado dentro de la transacción (un `rollback` no deja operación; un gancho del producto que falla se lleva el cambio y la operación; fuera de un nodo no encola); el recorrido completo (abrir, ingreso, venta, egreso, anulación, cerrar turno y día) sube en orden con una sola secuencia, y el central queda igual, sin movimientos duplicados y con la venta y su cobro en el turno traducido; aplicar dos veces es una fila (por el receptor y por los aplicadores solos, con un movimiento sin referencia); la venta con un turno que no subió, sin turno en el nodo y sin `caja_con_turno`; el pago acreditado después lleva el turno; cada rechazo (turno, movimiento, turno abierto en el central, día ya cerrado, caja inexistente, referencia repetida); `factura_id`/`cc_pago_id` no se copian; el arqueo distinto se guarda y se marca sin rechazar, el número de cierre distinto no, los totales distintos sí. Se comprobó que se ponen rojos al sacar la traducción del turno, la idempotencia del movimiento, la marca, el encolado de las anulaciones y la traducción del error del día cerrado.

**No cubre.**
- **Los movimientos que nacen en otros flujos del motor** (cobro de una factura, pago a cuenta de cuenta corriente, pago de un egreso, reintegro de una nota, MercadoPago) no pasan `ganchos=` y no suben como movimientos (abierto (a) de ADR-076). El arqueo del central los extraña y lo marca.
- **Los egresos de una anulación o una devolución hechas en el nodo** se reproducen en el central **sin turno** (`venta.anulada` y `venta.devolucion` no llevan el turno del nodo): también diferencia el arqueo, que queda marcado.
- **Turnos que ya estaban abiertos al actualizar el nodo**: no tienen `turno.abierto`; sus movimientos posteriores se rechazan hasta que `turno.cerrado` crea el turno (reintentarlos después). Conviene cerrar los turnos antes de actualizar.
- **`reabrir_dia`** no tiene gancho (abierto (b) de ADR-076): el producto del nodo no debe montar `autorizar_reabrir`.
- **La sucursal con nodo se cierra sólo en el nodo** (abierto (c)): un turno propio del central en esa sucursal el mismo día deja el cierre marcado o rechazado.
- **Un movimiento con `factura_id`** sube sin su factura (queda `NULL`): el cobro de la factura que hizo `transaccion_de_cobro` en el nodo no se reproduce.

**Cómo se adopta** (el próximo tag, v0.69.0: menor, módulo nuevo y campos aditivos en dos payloads, sin migración). En el producto, en las dos puntas: `OUTBOX = Outbox(nodo=..., en_linea=..., caja_con_turno=True)` (si ya existe, sólo suma el campo), `GANCHOS_DE_CAJA = caja_con_outbox(GanchosDeCaja(), OUTBOX)` y pasarlo a `build_caja_router`, `build_turnos_router` y `build_cierre_diario_router` (`ganchos_de_caja=`) y a las llamadas directas a `libracore.db.turnos`/`caja`/`cierre_diario` (`ganchos=`). Nada más en el central: `registrar_en` ya registra los diez tipos. Requiere libracore >= 1.188 y libraedge >= 0.17 en las dos puntas.

## ADR-070 — Los clientes que se dan de alta en un nodo offline suben como `cliente.alta`: `altas_en_nodo`, el aplicador del central y la traducción del `cliente_id` de la venta (2026-10-21)

**Problema.** Fase 2 de LibraEdge. Un nodo espeja los clientes del central pero el POS da de alta clientes en el nodo, con el siguiente id de la secuencia **de la base del nodo**: el cliente chocaba por id con uno del central (o lo pisaba el espejo) y `venta.registrada` subía con un `cliente_id` que en el central es de **otro cliente**, sin error. El humano decidió el 2026-10-21 que **las altas de referencia en el nodo se permiten y suben como operación; el central asigna su id y reescribe las referencias** (sólo clientes; productos va después). Las tres capas: `libracore` v1.189.0 (ADR-077) guarda al cliente con un **id provisorio negativo** y avisa a un gancho dentro de la transacción del alta; `libraedge` v0.18.0 (ADR-021) tiene el id provisorio, el mapeo `libraedge_mapeo` y el reapuntado cuando el id definitivo baja por el espejo; esto es lo que une las puntas.

**Qué ya existía.** `erp.offline` y `erp.offline_caja` (el patrón: encolar en la transacción del cambio con `next_sequence(commit=False)`, aplicadores `(conn, operacion)` idempotentes que levantan `ValueError` para ir a revisión manual), `SyncReceiver` y el `Despachador`, y desde libraedge v0.17.0 (ADR-020) el orden causal del outbox. En libracore, `crear_cliente`/`get_client_by_cuit` (que abrían su propia conexión) y `libro_de_clientes`. En el esquema, `sales.customer_party_id → parties(id)` y `parties.id = clients.id` por convención de la familia (`_espejar_party`): **el cliente de una venta es su `parties`**, gemelo del `clients` del motor.

**Decisión.**
- **Módulo nuevo `erp.offline_clientes`**. `erp.offline.aplicadores()` suma su aplicador, así que `registrar_en(Despachador(), hooks)` registra **los once tipos** con la misma línea de siempre.
- **`altas_en_nodo(outbox) -> AltasEnNodo`** (de `libracore.altas_en_nodo`): la identidad del nodo es la del `outbox` (`outbox.nodo()`), el id provisorio sale de `libraedge.mapeo.siguiente_id_provisorio(conn)` y el gancho `al_crear_cliente(conn, datos)` encola `cliente.alta` **en la transacción del alta** (`aggregate_type = "cliente"`, `aggregate_id = <node_id>:cliente:<id provisorio>`, `schema_version = 1`). Se arma igual en el nodo y en el central (fuera de un nodo no pide ids ni avisa y no necesita `libraedge`). Comparte la secuencia del outbox con las ventas y la caja: el alta sale **antes** de la venta que se le hace.
- **Contrato de `cliente.alta`.** Payload (el de `libracore.altas_en_nodo.datos_de_cliente`, `version: 1`): `{"version": 1, "cliente_id": <negativo>, "name", "address", "cuit_dni", "email", "phone", "iva_condition", "external_ref", "activo"}` más `empresa`, `ciudad`, `observaciones` y `tipo_facturacion` si la instancia tiene la revisión `0002`. No viajan `id`, `created_at` ni `auto_facturar`.
- **`aplicar_cliente_alta(conn, operacion)`**, en la transacción del receptor y por este orden: (1) si el mapeo de `(nodo, id provisorio)` ya está, no hace nada **si el cliente mapeado es el mismo** (mismo `external_ref`, o mismo CUIT/DNI, o —si ninguno trae documento— mismo nombre); si no, el nodo reusó un id provisorio y es `ClienteDelNodoError` (una venta a ese id iría a otro cliente); (2) **busca al cliente** por `external_ref` y, si no, por CUIT/DNI (`get_client_by_cuit(conn=)`, que normaliza guiones y prefiere al activo): si existe, **lo usa y no toca sus datos** (el central es la autoridad); sin ninguna de las dos claves **no deduplica** —dos «Juan Pérez» sin documento son dos clientes: fusionar por nombre es peor que duplicar—; (3) si no existe, lo crea con `crear_cliente(..., conn=)` (con su `parties`, el libro y la auditoría, y **el id de su secuencia**); (4) anota el mapeo de **`clients` y de `parties`** (`anotar_mapeo`), o los dos o ninguno junto con el cliente.
- **Traducción: un solo punto, `cliente_del_central(conn, node_id, id)`**. `None` y un id no negativo pasan igual (el nodo vendió a un cliente que ya existía en el central y espejó); uno negativo se busca en el mapeo de `parties` y, sin mapeo, `ClienteNoSincronizado`. Lo usa `aplicar_venta_registrada` (`cliente_id`). Las otras operaciones **no llevan un cliente**: `venta.pago_acreditado`, `venta.anulada` y `venta.devolucion` trabajan sobre la venta del central, que ya quedó a nombre del traducido (y `cliente_cc_de` lee el del central); `venta.facturada` lleva el CUIT y la razón por valor; los movimientos de caja no llevan cliente. Un producto cuyo `extra_de_venta` lleve un id de cliente lo traduce con esta función en su `al_materializar`.
- **Lo que falta o choca es rechazo, no reintento.** Alta mal formada (versión, `cliente_id` no negativo, sin nombre: `AltaDeClienteInvalida`), un `parties` ausente para el cliente que ya existía, o un id provisorio reusado por otro cliente: todas `ValueError` → revisión manual en el central, `manual_review` en el nodo y la cola sigue (ADR-020). Una venta a un cliente provisorio **cuyo alta no se aplicó** se rechaza a su vez con el mensaje de qué alta falta (`ClienteNoSincronizado`), en vez de quedar a nombre de nadie o de otro: al resolver el alta, `reintentar` la venta, en el orden en que se crearon.
- **Pisos y pines.** `libracore>=1.189` en los extras `migrations` y `erp` (usan `altas_en_nodo`, `crear_cliente(conn=)` y `get_client_by_cuit(conn=)`), pin de la suite a v1.189.0 y los extras `offline` y `offline-server` a libraedge **v0.18.0** (el mapeo y el id provisorio). Sin migración propia (ni de libracore ni de libraedge).

**Tests.** `tests/test_erp_offline_clientes.py`, de punta a punta con el nodo y el central de verdad, el `SyncReceiver`, el `Despachador` y el `MirrorApplier`, en las tres combinaciones de motores y con los ids del central desplazados a propósito: el cliente nace con id negativo (y su party), sube **en orden** antes que su venta y el central lo crea con su propio id, anota el mapeo y deja la venta a su nombre; la segunda venta al provisorio también se traduce; el cliente y su mapeo bajan y el nodo reapunta la venta y borra la provisoria; el **fiado** llega al libro de cuenta corriente en las dos puntas (el `tercero_id` sin FK); **dedupe por CUIT** (escrito de otra forma) sin tocar los datos del central; **dos nodos con el mismo CUIT** terminan en un cliente y cada uno reapunta lo suyo (con el mapeo del otro ignorado y los dos con `-1`); dos clientes sin documento no se fusionan; **un negativo sin mapeo se rechaza** con un mensaje claro y se recupera con `reintentar`; el alta aplicada dos veces es un cliente; un id provisorio reusado por otro cliente se rechaza; las altas mal formadas; **fuera de un nodo nada cambia** (ids de la base, nada en el outbox, sin mapeo) y un cliente del central que el nodo espejó viaja con su id y no se traduce; el gancho que falla se lleva el alta; y, con el central en PostgreSQL, el changelog por **trigger** baja cliente, party y mapeo en ese orden. Se comprobó que se ponen rojos al sacar la traducción de `cliente_id`, el id provisorio de libracore, el reapuntado de libraedge, el dedupe y la referencia declarada del libro.

**No cubre.**
- **La edición y la baja de un cliente en el nodo no suben** (la decisión es de altas): lo que se corrija en un cliente provisorio antes de que baje el del central se pierde.
- **El cobro de cuenta corriente en el nodo** (`cc_pagos`, `recibos`) no sube como operación (abierto (a) de ADR-076/069): si se cobra a un cliente provisorio, el cobro vive en el nodo con su id reapuntado pero el central no se entera. El **fiado** (la venta a cuenta corriente) sí sube con la venta.
- **Un `extra_de_venta` que lleve un cliente** (RestoLibra: el pedido y la mesa) tiene que traducirlo el producto con `cliente_del_central`.
- **Dos nodos que dan de alta al mismo cliente sin documento** quedan como dos clientes: se unifican a mano en el central.
- **La uv.lock** de este repo sigue en los tags viejos hasta que existan v1.189.0 y v0.18.0.

**Cómo se adopta** (el próximo tag, v0.70.0: menor, módulo nuevo y un campo traducido en `venta.registrada`, sin migración). En el producto, en las dos puntas, junto a su `OUTBOX`: `altas_en_nodo.configurar(offline_clientes.altas_en_nodo(OUTBOX))` (`libracore.altas_en_nodo`), y en su `Aprovisionamiento`, con `clients` y `parties` ya entre las tablas de referencia: `altas_en_nodo=[("clients", REFERENCIAS_DE_CLIENTES), "parties"]` (suma `libraedge_mapeo` al espejo solo). Nada más en el central: `registrar_en` ya registra `cliente.alta`. Orden de despliegue: libracore, libraedge, libracommerce y los productos; el central antes que los nodos (un nodo que da de alta sube un tipo que un central viejo manda a revisión manual). Requiere libracore >= 1.189 y libraedge >= 0.18 en las dos puntas.

## ADR-071 — El stock de la sucursal de un nodo offline sube como `stock.movimiento` y `compra.recibida`; el central se niega a moverlo, y la carga inicial lo trae (2026-10-21)

**Problema.** Fase 2 de LibraEdge. Un nodo vende (ADR-067), maneja la caja (ADR-069) y da de alta clientes (ADR-070), pero el resto de su stock —ajustes, mermas, entradas y salidas a mano, transferencias, recepciones de compra— quedaba en su base sin llegar al central, y el central seguía moviendo el stock de la misma sucursal desde la web: dos bases escribiendo lo mismo, sin merge posible. El humano decidió el 2026-10-21: **(1) carga inicial:** al vincular, el nodo trae las existencias de SU sucursal del central, y desde ahí manda el nodo; **(2)** el stock de una sucursal con nodo lo mueve **sólo el nodo**: el central contesta 409 a todo movimiento sobre sus depósitos (ventas web, anular desde la web una venta del nodo, recibir desde la web); **(3)** las **transferencias** son desde el nodo y sólo entre depósitos de su sucursal; **(4) compras:** órdenes y proveedores en la web, en el nodo sólo se **recibe** contra esas órdenes y sube como documento `compra.recibida`; **(5)** los depósitos sin sucursal se asignan **antes** de vincular.

**Qué ya existía.** El patrón de `erp.offline_caja` y `erp.offline_clientes` (encolar en la transacción con `next_sequence(commit=False)`, aplicadores `(conn, operacion)` idempotentes por `origen/origen_id`, rechazos con `ValueError` que no traban la cola, ADR-020 de libraedge); `libracore.altas_en_nodo.configurar` como molde de un gancho global registrado una vez; `erp.compras.recibir_mercaderia`, ya idempotente por `clave_operacion` (ADR-046); `erp.lotes.saldos_por_bucket`; `catalogo.adoptar_sucursal_unica`; y la tabla `node_identity` de libraedge, con la sucursal del nodo.

**Decisión.**
- **Dos puntos de extensión en el ledger, a nivel de módulo** (`db/ganchos_de_movimientos.py`, reexportados en `erp.stock`): `configurar_antes_de_mover(fn)` (`fn(conn, deposito_id)`, con el depósito ya resuelto y antes del `INSERT`) y `configurar_al_mover(fn)` (`fn(conn, movimiento: dict)`, después del `INSERT`, con la misma conexión y dentro de la transacción). Los llaman **los dos escritores físicos** del ledger: `erp.stock.add_movimiento_stock` y `SqliteCommerceRepository.append_stock_movement` (transferencias y recepciones). Sin gancho registrado no hacen nada ni leen nada. Si el gancho de después levanta, el movimiento no queda (en el repositorio fuera de `transaction()` revierte él, para que el próximo commit de la conexión no lo confirme sin su operación). Es global y del proceso, como `altas_en_nodo`.
- **Migración `0008_origen_del_stock`**: `stock_movements.origen TEXT` (el `node_id`), `origen_id INTEGER` (el id de la fila en el nodo) e índice único parcial `(origen, origen_id) WHERE origen IS NOT NULL`. Aditiva, idempotente por introspección, como la `0006`: las columnas son de la revisión y **no** de `init_schema()` (el schema congelado no cambia). Sin ella, el central rechaza `stock.movimiento` con un mensaje que pide `libracommerce-migrar upgrade` (`StockSinRevision`, un `ValueError`: revisión manual, no traba la cola). Baja quitando índice y columnas: se pierde la idempotencia, no bajarla con nodos activos.
- **Módulo nuevo `erp.offline_stock`.** `configurar(OUTBOX)` (una vez, al armar la app, **en las dos puntas**) arma las dos mitades, que leen `outbox.nodo()` en cada movimiento: en un nodo encola; en el central guarda.
  - **`stock.movimiento`** (`aggregate_type = "stock_movimiento"`, `aggregate_id = <node_id>:stock_movimiento:<id>`, `schema_version = 1`, payload `version: 1`): `{movimiento_id, producto_id, variante_id, deposito_id, movement_type, reason_code, cantidad, fecha, lote, vence, costo, nota, usuario_id, ref_tipo, ref_id}`, ids del nodo y cantidades como texto. **Una por fila del ledger, EXCEPTO** las filas de una venta (`source_type` `venta`, `sale`, `sale_return`: ya viajan con `venta.registrada` o las reproduce el central con `anular_venta` y `devolver_items`; subirlas las duplicaría), las de una recepción (`purchase_receipt`: suben juntas como `compra.recibida`), la carga inicial (`reason_code = "carga_inicial"`) y lo que escribe un aplicador.
  - **Aplicador `stock.movimiento`**: inserta la fila **tal cual** (lote, vencimiento, `unit_cost`, nota, usuario) con su `origen`/`origen_id`, idempotente por ese par. No pasa por `add_movimiento_stock` (ni guarda ni encolado). Verifica a mano, antes de escribir, lo que si no sería un `IntegrityError` —que el receptor lee como «duplicado» y la operación se perdería—: el nodo (`NodoDesconocido`), que el depósito exista y **sea de la sucursal del nodo** (`node_identity.branch_id` contra `locations.branch_id`; sin sucursal o de otra, `DepositoFueraDeLaSucursal`), el producto, la variante, el tipo y la cantidad. **La entrada de una transferencia** apunta (`source_id`) a su salida del nodo: el central la traduce a **su** id de esa salida por el origen (`SalidaNoSincronizada` si la salida no subió). Una entrada a un depósito de otra sucursal se rechaza por lo mismo; y como eso dejaría la mercadería descontada en el origen y sin entrar, **el nodo ni escribe una transferencia entre sucursales** (`TransferenciaEntreSucursales`, al encolar la entrada: el movimiento entero se revierte). Todo es `ValueError`.
  - **La guarda del central**: antes de escribir, en los dos escritores, si `locations.branch_id` del depósito tiene un nodo **activo** en `node_identity` —comparando como texto— levanta `DepositoDeSucursalConNodo`. Una base sin la tabla `node_identity` no tiene nodos: sin guarda. 🔴 **Es una `Exception`, no un `ValueError`**: varios routers pasan los `ValueError` a 422 y esto no es un dato inválido. El producto la convierte en **409** con `offline_stock.instalar_manejador_409(app)` (o su propio `exception_handler`). Para que **los aplicadores no la disparen** (`venta.registrada` descuenta stock en los depósitos del nodo, `compra.recibida` llama a `recibir_mercaderia`), `offline.aplicadores()` envuelve **cada** aplicador con `aplicando_del_nodo`, un `contextvars.ContextVar` que la guarda y el encolado leen.
  - **Carga inicial.** `carga_inicial_del_central(conn, node_id, branch_id) -> {version, depositos, saldos: [{item_id, variant_id, location_id, lote, vence, cantidad}], corte: {movimiento_id, a}}`: los saldos de `erp.lotes.saldos_por_bucket` sobre todos los depósitos de la sucursal, JSON puro. `aplicar_carga_inicial(conn, traer) -> dict` en el nodo —la firma que libraedge llama (`LIBRAEDGE_CARGA_INICIAL=libracommerce.erp.offline_stock:aplicar_carga_inicial`)— escribe por bucket **un `ajuste` con `reason_code = "carga_inicial"` y delta = saldo del central − saldo local**: una venta del nodo que ya subió está en las dos puntas y no se cuenta dos veces, y volver a correrla da delta 0. **Espera sin escribir nada** (`hecha: False`, `motivo: ...`; libraedge hace rollback y reintenta en el ciclo siguiente) si hay operaciones sin confirmar (`pending`, `sending`, `retryable_error`) o una de stock en `manual_review` —el delta supone que el central ya aplicó todo lo del nodo—, si el stock del nodo o su outbox cambiaron mientras se traía la carga (toma el saldo local y el último id **antes** de `traer()` y verifica **después**), o si el espejo todavía no tiene algún producto, variante o depósito. No commitea: libraedge (v0.20.0) commitea las escrituras junto con la marca de «ya hecha» (cualquier resultado que no sea `hecha: False` cuenta como hecha) y hace rollback si espera o levanta; `--forzar` la vuelve a correr, y por eso es idempotente. `carga_inicial_del_central(conn, node_id, branch_id)` es la `f(conexion, node_id, branch_id) -> dict` que libraedge llama en el central: `branch_id` llega como texto (el de `node_identity`) y se convierte al id de la sucursal; el dict sale por JSON.
  - **`compra.recibida`** (`aggregate_type = "compra_recepcion"`, `aggregate_id = <node_id>:compra_recepcion:<id>`): `{recepcion_id, proveedor_id, orden_id, remito, deposito_id, fecha, lineas: [{producto_id, cantidad, costo, lote, vence}]}`. La encola `recepcion_con_outbox(OUTBOX, al_recibir=None)`, que va en el nuevo `OpcionesCompras.al_recibir`; `erp.compras.recibir_mercaderia` y `confirmar_recepcion` reciben `al_recibir=` y lo llaman **dentro de la transacción** con la recepción ya confirmada (`recibir_mercaderia`, sólo cuando escribió una nueva: un reintento no lo vuelve a llamar; `confirmar_recepcion`, con `al_recibir`, pasa a ser una transacción —sin él sigue confirmando cada escritura, como siempre—). El aplicador llama a `recibir_mercaderia(clave_operacion=clave_de_operacion(op))`, idempotente: el stock entra, el último costo se actualiza y la orden avanza en el central. `OrdenNoEncontrada`, `EstadoInvalido` (la orden se canceló en la web) y `SinRevision` son `CompraRechazadaPorElCentral`. El proveedor y la orden se crean en la web y bajan por el espejo con los mismos ids.
  - **Depósitos sin sucursal**: `depositos_sin_sucursal(conn) -> list[str]` devuelve un problema legible por depósito (`"El depósito 'X' no tiene sucursal"`) y es el `Aprovisionamiento(validar_registro=...)` de libraedge; `listar_depositos_sin_sucursal(conn)` da los datos y `exigir_depositos_con_sucursal(conn, adoptar_si_hay_una=False)` levanta `DepositosSinSucursal` para que el registro de un nodo se niegue; con una sola sucursal, `adoptar_si_hay_una=True` llama a `catalogo.adoptar_sucursal_unica` (no commitea).
- `registrar_en` registra **trece** tipos. Sin pisos nuevos en libracore ni en libraedge: lo único de libraedge que se usa es el outbox y la tabla `node_identity`, que ya estaban.

**Tests.** `tests/test_erp_offline_stock.py`, de punta a punta con el nodo y el central de verdad, el `SyncReceiver` y el `Despachador`, en las tres combinaciones de motores, y la revisión `0008` contra los dos motores: el ajuste a mano y la transferencia se encolan en la transacción por **cada** escritor, y una transferencia o un gancho que falla no deja ni el movimiento ni la operación; las filas de una venta, una devolución y una anulación **no** suben aparte; el ajuste con lote, vencimiento y costo llega tal cual y con su origen, y aplicarlo tres veces es una fila; la transferencia dentro de la sucursal ata la entrada a la salida **del central** (con los ids desplazados a propósito); la de otra sucursal ni se escribe en el nodo, y un movimiento a un depósito de otra sucursal, sin sucursal, inexistente, de un nodo desconocido, de un producto que no está, o sin la `0008`, se rechaza sin trabar; la guarda da `DepositoDeSucursalConNodo` en los dos escritores, en una venta de la web y al anular una venta del nodo, y **no** frena a los aplicadores, a otra sucursal, a un depósito sin sucursal, a un nodo dado de baja, a una base sin `node_identity` ni al propio nodo; el 409 sale de verdad por FastAPI; la carga inicial con ventas antes (no duplica), durante (espera) y después, si el central tenía más, re-correrla da 0, y espera con pendientes, con stock en revisión y con el espejo incompleto; `compra.recibida` de punta a punta (orden creada en el central, recibida en el nodo, aplicada, la orden avanza a `partial` y a `received`, el costo se actualiza, el reintento no duplica, la orden cancelada se rechaza) por «Recibir mercadería» y por la confirmación en tres pasos; y la `0008`: datos previos intactos, idempotente, único sólo sobre las filas con origen y baja y vuelve a subir.

**No cubre.**
- **Un producto que monte Compras en un nodo sin `OpcionesCompras(al_recibir=...)`** no sube nada de sus recepciones (ni `compra.recibida` ni movimiento): el stock del central no se entera. Es cableado del producto.
- **Lo que mueve el central antes de que haya nodo** y no se refleja: la carga inicial lo trae una vez; después, el stock de esa sucursal en el central es sólo lo que sube el nodo.
- **Una venta, una anulación o una devolución del nodo sobre un depósito de otra sucursal** se aplican en el central igual (los aplicadores de venta no validan sucursal: es el comportamiento de ADR-067).
- **Variantes y reasignaciones de lote** viajan como movimientos comunes (salida y entrada), sin una operación propia.
- **La carga inicial no borra** lo que el nodo tenga y el central no: la lleva a lo del central con un ajuste negativo, pero deja el historial.
- **El registro del nodo y la carga inicial los cablea libraedge** (v0.20.0): `Aprovisionamiento(validar_registro=offline_stock.depositos_sin_sucursal)` y `LIBRAEDGE_CARGA_INICIAL=libracommerce.erp.offline_stock:aplicar_carga_inicial`.

**Cómo se adopta** (el próximo tag, v0.71.0: menor, módulo y migración nuevos, dos tipos de operación nuevos y el parámetro opcional `al_recibir`). En cada producto con nodo, en las **dos puntas**, junto a su `OUTBOX`: `offline_stock.configurar(OUTBOX)`; en el central, `offline_stock.instalar_manejador_409(app)`; en Compras, `OpcionesCompras(al_recibir=offline_stock.recepcion_con_outbox(OUTBOX))`. Nada más en el central: `registrar_en` ya registra los dos tipos. Correr `libracommerce-migrar upgrade` en el central **antes** de que un nodo suba stock. Orden de despliegue: libracommerce, los productos con el central primero (un nodo que sube un tipo que un central viejo no conoce lo manda a revisión manual) y recién después vincular el nodo, con los depósitos ya asignados a su sucursal. Requiere libracore >= 1.189 y libraedge >= 0.18 en las dos puntas (sin cambios).

**Enmienda (2026-10-22, v0.72.0): en un nodo, el depósito por defecto es el de su sucursal.**

*Problema.* Medido en la prueba real del 2026-10-22: un nodo vinculado a la sucursal 2 («Sucursal con PC del local», depósito 2) hizo un ajuste de stock en ContaLibra, que no usa `OpcionesStock.por_deposito` y por eso no manda `deposito_id`. El movimiento cayó en el depósito por defecto **de la instancia** (`catalogo.get_default_deposito_id`: `locations.is_default = 1 AND active = 1`) y el espejo trae ese `is_default` del central: el de «Casa central», depósito 1. El central lo rechazó a revisión manual (`DepositoFueraDeLaSucursal`). Pasaba igual con las ventas, la producción y todo movimiento sin depósito explícito hecho en un nodo que no está en la sucursal predeterminada. El ADR original suponía que el nodo escribe sólo en depósitos de su sucursal, pero nadie le decía cuál era «el de siempre».

*Qué ya existía.* `get_default_deposito_id` es el **único** lugar donde se resuelve un depósito faltante: `erp.stock._deposito_del_movimiento`, `ajustar_stock`, el descuento FEFO de la venta, `erp.lotes` y `sucursal_del_deposito` (que es lo que anota `sales.branch_id`) pasan todos por ahí; las ventas no usan `get_default_sucursal_id`. El patrón de gancho global de `db/ganchos_de_movimientos.py`, y `catalogo.get_deposito_de_venta` (el predeterminado de una sucursal, pero con un `UPDATE` que repara: no sirve en medio de un movimiento).

*Decisión.*
- **El nodo guarda su sucursal.** Tabla chica y propia de LibraCommerce, `nodo_local(clave TEXT PRIMARY KEY, valor TEXT NOT NULL)`, en la revisión **`0009_sucursal_del_nodo`** (aditiva, idempotente, `op.create_table`, igual en SQLite y PostgreSQL; **no** de `init_schema()`: el schema congelado no cambia). Hoy tiene una clave, `sucursal_id`. Se descartó guardarlo en una tabla de LibraEdge (`node_identity` es del central y el nodo no la tiene; tocarla acopla el motor a un esquema ajeno) y en el entorno (`LIBRAEDGE_*` lo fija el instalador; una sucursal que cambia no se arregla con un reinicio).
- **Quién la escribe: la carga inicial.** `carga_inicial_del_central` suma `sucursal_id` (entero) al payload —aditivo, `version` sigue en 1: un nodo anterior ignora la clave, un central anterior no la manda y el nodo no guarda nada— y `aplicar_carga_inicial` la guarda en la misma transacción que los ajustes (si no se confirma, ninguna de las dos). Sin la `0009`, levanta `NodoSinRevision` (un `ValueError`) **antes de escribir** y el worker de LibraEdge lo muestra en el estado y reintenta.
- **Un tercer gancho global**, `db.ganchos_de_movimientos.configurar_deposito_por_defecto(fn)` (`fn(conn)` devuelve un id o `None`), que `catalogo.get_default_deposito_id` consulta primero. Lo registra `offline_stock.configurar(OUTBOX)` (y lo saca `desconfigurar()`): `deposito_por_defecto_del_nodo(outbox)` contesta sólo si `outbox.nodo()` no es `None` y hay sucursal guardada, con `catalogo.deposito_activo_de_la_sucursal` (lectura pura: el `branches.default_location_id` si sigue activo y es de la sucursal, y si no el activo de menor id). En cualquier otro caso contesta `None` y vale lo de siempre: **el central, una instalación común, un nodo que todavía no hizo la carga, uno sin la `0009` o uno cuya sucursal no tiene depósito activo no cambian.** Sin tocar productos: el cableado es el mismo `configurar(OUTBOX)` de antes.
- **Nodos ya instalados.** La `0009` **deduce** la sucursal de las filas `reason_code = 'carga_inicial'` (sólo las escribe un nodo, y sólo en depósitos de su sucursal): si todas son de una sola sucursal, la guarda; el nodo queda arreglado con sólo actualizar y migrar. Si no hay ninguna (la sucursal no tenía stock al vincular) o hay de varias, no guarda nada. Para eso queda `libraedge-nodo carga-inicial --forzar`: borra la marca `carga_inicial:<node_id>` y vuelve a llamar a `aplicar_carga_inicial`, que con el nodo al día da delta 0 (no mueve stock) y guarda la sucursal. Se descartó deducir en cada movimiento: `stock_movements.reason_code` no tiene índice y el escaneo caería en el camino de cada venta.

*Tests.* `tests/test_deposito_del_nodo.py` (las tres combinaciones de motores y la `0009` contra SQLite y PostgreSQL): el ajuste sin depósito del nodo de la sucursal 2 cae en el depósito 3 y el central lo **aplica** (no revisión); una venta descuenta del 3, anota la sucursal 2 y el central la aplica; el nodo de la sucursal predeterminada cae donde siempre, y el predeterminado de la sucursal manda sobre el de menor id; fuera de un nodo (`Outbox.nodo()` en `None`), sin el gancho, sin sucursal guardada, sin la tabla o con una sucursal sin depósito activo, igual que antes; la carga guarda la sucursal en la misma transacción (con `rollback` no queda), volver a aplicarla la guarda sin mover una fila del ledger, un central anterior no la manda, sin la `0009` pide migrar sin escribir; `--forzar` con el `CargaInicialWorker` de libraedge (>= 0.20); y la `0009` (crea la tabla, deduce de una sucursal, no deduce de ninguna o de dos ni de depósitos sin sucursal, no pisa lo guardado, baja y vuelve a subir sin tocar el ledger).

*No cubre.* `get_default_sucursal_id` (el `is_default` de `branches`) no cambia: sólo lo usan el alta de depósitos de la web y `adoptar_sucursal_unica`, que no corren en un nodo. Un producto que **sí** manda `deposito_id` (VentaLibra con `por_deposito`) no necesitaba el arreglo y no cambia. Si el humano reasigna el nodo a otra sucursal, vale la de la próxima carga inicial (`--forzar`).

*Cómo se adopta* (v0.72.0: menor, migración nueva). Subir el pin y **correr `libracommerce-migrar upgrade` en el nodo** (`0009`); central primero (para que la carga traiga `sucursal_id`), después el nodo. Un nodo con stock cargado queda arreglado con eso; uno sin stock al vincular necesita además `libraedge-nodo carga-inicial --forzar`. Sin cambios en libracore ni en libraedge.

**Enmienda (2026-10-22, v0.72.1): la carga inicial lleva a cero el stock local de los depósitos que no son de la sucursal del nodo.**

*Problema.* Medido en la misma prueba real del 2026-10-22: una PC del local que estuvo vinculada a la sucursal 1 («Casa central») y después se volvió a vincular, con otro `node_id`, a la sucursal 2 conservaba en su base el stock de los depósitos de la sucursal 1: la carga inicial vieja se lo había traído y sus ventas lo habían movido. Ese stock es basura para el nodo (no puede mover stock de otra sucursal, y el central es la verdad ahí) y confunde: pantallas de stock del nodo, reportes, el conteo de `ajustar_stock` (que fija el **total** del producto, ajeno incluido). La carga solo miraba los buckets de los depósitos de **su** sucursal, así que nunca lo tocaba; y la `0009` no pudo deducir la sucursal porque había filas `carga_inicial` de dos sucursales.

*Qué ya existía.* `aplicar_carga_inicial` ya calcula `locales` (los saldos de todo el ledger del nodo) y escribe un `ajuste` `carga_inicial` por delta; `carga_inicial` ya está fuera del encolado (`sube_como_movimiento`) y estos ajustes se escriben con `INSERT` directo, sin pasar por el gancho de movimientos. No hace falta ningún mecanismo nuevo.

*Decisión.*
- **Dónde:** en `aplicar_carga_inicial`, solo en el nodo. Si la carga trae `sucursal_id` **y** al menos un depósito, cada bucket `(producto, variante, depósito, lote, vencimiento)` con saldo local distinto de cero en un depósito que no está en `depositos` de la carga (de otra sucursal **o sin sucursal**) recibe un `ajuste` `reason_code = 'carga_inicial'` por el saldo opuesto (también si el saldo era negativo), con la nota `NOTA_CARGA_INICIAL_AJENO`. «Ajeno» se define por **la carga**, no por el `branch_id` del espejo del nodo (que puede estar viejo): el central es la verdad.
- **Sin `sucursal_id`** (un central anterior) **o sin depósitos** en la carga (una sucursal sin depósitos: no se sabe qué sería «propio», y no se vacía un nodo por una sucursal mal armada), no se toca nada fuera: es lo de siempre.
- **Contrato:** el resumen suma `ajustes_ajenos` (cuántos de `ajustes` fueron a cero de depósitos ajenos; `ajustes` sigue siendo el total escrito). Aditivo: LibraEdge solo lee `hecha`. Las respuestas con `hecha: False` también traen `ajustes_ajenos: 0`.
- **Atomicidad e idempotencia:** los ceros van en la misma transacción que los demás ajustes y la sucursal guardada (si no se confirma, ninguno); no se calculan antes de la espera por el outbox ni de la verificación `el_stock_cambio`, así que valen las mismas garantías. Volver a correrla da 0 (el saldo ajeno ya es cero). Una venta del nodo sobre un depósito ajeno **después** de la carga (ADR-067 la deja pasar) se vuelve a llevar a cero en la próxima carga forzada; el central la aplicó igual.
- **No sube nada:** son `carga_inicial`; el test lo verifica (`sync_outbox` vacío, el ledger del central intacto).
- **La `0009` no cambia, y no se agrega una `0010`.** Se evaluó que la deducción prefiriera la sucursal de las filas `carga_inicial` **más recientes** cuando hay varias (una PC mudada) y se descartó: (1) si la sucursal nueva no tenía stock en el central, la carga no escribió filas suyas y las «más recientes» serían igual las de la sucursal vieja: guardaría la sucursal **equivocada**, que es justo el bug que se arregla (el depósito por defecto caería en uno que el central rechaza); (2) con este arreglo, los ceros de los depósitos ajenos son filas `carga_inicial` en depósitos de la sucursal **vieja** y, además, las más nuevas: deducir por «la fila más reciente» las contaría como la sucursal del nodo. Una migración no tiene cómo distinguirlas con certeza (la nota es texto libre), y una deducción equivocada es peor que ninguna: hoy, con varias sucursales, no guarda nada y el nodo sigue como antes hasta que corra la carga. Un nodo mudado se arregla solo, sin `--forzar`: LibraEdge marca la carga por `node_id` (`carga_inicial:<node_id>`), así que el `node_id` nuevo hace su carga con esta versión, que guarda la sucursal y limpia los ajenos. Un nodo mudado **ya cargado** con la versión anterior: actualizar, migrar y `libraedge-nodo carga-inicial --forzar` (central primero).

*Tests.* `tests/test_erp_offline_stock.py` (las tres combinaciones de motores): la PC con stock local en la sucursal 1 (con lote, en un depósito sin sucursal y con un bucket negativo) que pasa a la 2 queda con los depósitos 1, 2 y 4 en cero y el 3 al saldo del central, sin una operación en `sync_outbox`, con el ledger y el saldo del central intactos y la sucursal guardada; re-correrla (dos veces) da 0; `rollback` no deja ni los ceros ni la sucursal; una carga que espera no toca nada; sin `sucursal_id` en la carga, o con una sucursal sin depósitos, no cambia nada fuera; y un nodo de una sola sucursal desde siempre carga como hoy (`ajustes_ajenos = 0`). Sin el arreglo, el primero y el de `rollback` fallan. En `tests/test_deposito_del_nodo.py` cambian las cifras de tres tests que partían de un nodo con 10 en el depósito 1 y vinculaban a la sucursal 2: ahora ese 10 queda en cero (era el caso del bug).

*No cubre.* No hay migración: el stock ajeno de un nodo mudado ya cargado se limpia con la carga (`--forzar`), no al migrar. Un depósito sin sucursal en el central cuenta como ajeno (el ADR exige asignarlos antes de vincular). El historial no se borra: quedan los movimientos viejos y el ajuste que los neutraliza.

*Cómo se adopta* (v0.72.1: parche, sin migración). Subir el pin; central primero (la carga ya trae `depositos`; no hace falta que el central cambie), después el nodo. Sin cambios en libracore ni en libraedge.


## ADR-072 — El vínculo propio del producto sobrevive a la venta que sube de un nodo: `source_type` / `source_id` en `venta.registrada`, `venta_origen_nodo` y `tablas_de_origen` (2026-10-23)

**Qué ya existe.** Se buscó con `git grep` en `libracore`, `libracommerce`, `libraedge` y los productos antes de diseñar, y casi todo está:
- **`Outbox.extra_de_venta` y `al_materializar(conn, venta_id, operacion)`** (ADR-067): lo propio del producto ya viaja en `payload["extra"]` y el aplicador de `venta.registrada` ya llama al gancho al final, dentro de la transacción del receptor. RestoLibra (v1.6.2) los usa para el pedido de la mesa. **No hay gancho nuevo que crear**: el plan lo llamaba `al_materializar(venta_central, extra, conn)`, pero cambiar la firma rompería a RestoLibra y no agrega nada (el `extra` está en `operacion.payload["extra"]` y la venta se lee con `obtener_venta(conn, venta_id)`).
- **Ids provisorios y mapeo** (`libracore.altas_en_nodo`, ADR-077 del motor; `libraedge.mapeo`, ADR-021 de libraedge; `offline_clientes.cliente_del_central`, ADR-070 de este motor): un alta en el nodo nace con id negativo, el central anota `(nodo, tabla, id_nodo) -> id_central` en `libraedge_mapeo` y quien lleva ese id lo traduce con `traducir_id`; sin mapeo, `ValueError` (revisión manual), nunca una referencia a otra cosa. Se reusa tal cual.
- **Ganchos inyectados que no cambian nada sin ellos** (ADR-076 del motor, `caja_con_outbox` de ADR-069): la costura la pasa el producto y, sin ella, el motor se comporta como siempre. Es el criterio de esta pieza.
- **LibraClub** (`app/servicios/buffet.py`): el consumo del buffet es una `Sale` con `source_type = 'reserva'` y `source_id = <reserva>` (o `'mostrador'`); `consumos_de_reserva`, `consumido_de_reservas` y `ventas_de_mostrador` leen esas dos columnas con SQL directo, y la factura conjunta de la reserva sale de ahí.

**Problema.** Lo que no existía: ADR-067 usó `sales.source_type = 'libraedge:<node_id>'` y `sales.source_id = <id en el nodo>` como **clave de idempotencia y de búsqueda** de la venta del nodo (`venta_del_central`), «sin tabla nueva ni migración». Son las mismas dos columnas con las que un producto ata su venta a lo suyo. Una venta que sube de un nodo con `source_type = 'reserva'` llegaba al central con `libraedge:<node_id>`: el consumo dejaba de pertenecer a su reserva (el buffet no lo encontraba y la factura conjunta salía sin él). Hallazgo 5 del relevamiento de LibraCargo y LibraClub en LibraEdge. No se puede resolver sólo con un gancho: si el producto rehace `source_type` desde `al_materializar`, las operaciones siguientes (pago acreditado, devolución, anulación, factura) ya no encuentran la venta.

**Decisión.**
- **Sube.** `encolar_venta_registrada` lee `sales.source_type` / `source_id` de la venta del nodo y, si el producto le puso un vínculo (algo distinto del `'pos'` con que nace toda venta de `crear_venta` y sin `source_id`), el payload suma `source_type` y `source_id`. Aditivo: sin vínculo el payload es **idéntico** al de antes, `SCHEMA_VERSION` sigue en 1 y un central anterior ignora las dos claves. El producto pone el vínculo en la venta del nodo como siempre (su `al_registrar_venta`, que `con_outbox` corre **antes** de encolar), y lo propio que no cabe en dos columnas va en `extra`.
- **Se conserva.** Con vínculo, el aplicador deja en la venta del central el `source_type` / `source_id` **del producto**, y la identidad de nodo pasa a una tabla aparte: **`venta_origen_nodo(node_id, venta_id_nodo, venta_id)`**, clave `(node_id, venta_id_nodo)`, `venta_id` único y en cascada con `sales`, de la **revisión `0010_vinculo_de_la_venta`** (aditiva, vacía al nacer, idempotente, `op.create_table`; no de `init_schema()`, que sigue congelado, como la 0006 a la 0009). Sin vínculo, nada cambia: la venta sigue marcada `libraedge:<node_id>` y no escribe en la tabla. `venta_del_central` busca por las dos vías (primero la marca, después la tabla si la base la tiene), así que las cuatro operaciones que llegan después encuentran la venta igual, y el aplicador es idempotente por la misma pregunta.
- **Se traduce.** Un `source_id` negativo es de una entidad que el nodo dio de alta. El producto declara a qué tabla del mapeo corresponde cada `source_type` (`registrar_en(..., tablas_de_origen={"reserva": "reservas"})`) y el aplicador lo traduce con `libraedge.mapeo`. Sin tabla declarada o sin mapeo: `VinculoNoSincronizado` (`ValueError`, revisión manual; se reintenta cuando llega el alta). `offline.id_del_central(conn, node_id, tabla, id_nodo)` es la misma traducción, pública, para los ids que el producto lleve en su `extra`.
- **Se valida.** `source_type` es un texto que **no** empieza con `libraedge:` (marcador reservado: lo confundiría con una venta sin vínculo) y `source_id` es `None` o un entero: si no, `VinculoInvalido`. Un central **sin la revisión `0010`** rechaza (`VentaSinRevision`, con el nombre de la revisión y `libracommerce-migrar`) la venta que trae vínculo, en vez de perderlo o de dejarla sin identidad; las ventas sin vínculo se aplican como siempre aunque falte la tabla.
- **Se rehace.** `al_materializar(conn, venta_id, operacion)` corre al final, con la venta, sus pagos, su caja, su stock y su vínculo ya escritos, en la transacción del receptor. Si levanta —un `ValueError` va a revisión manual; cualquier otra excepción se propaga y la transacción sin confirmar se deshace—, no queda **nada** de la operación (ni la venta, ni su caja y stock, ni la fila de `venta_origen_nodo`, ni lo que el gancho haya escrito), y reintentar la aplica entera. Reaplicar una operación ya aplicada no duplica la venta ni vuelve a llamar al gancho.
- **`siguiente_numero` ignora también esas ventas.** Tras ADR-067 saltaba las `libraedge:%` para que el `V-` del central no corriera al número del nodo; las de vínculo propio ya no se reconocen por el `source_type`, así que, si la base tiene `venta_origen_nodo`, se excluyen por ahí (una consulta de metadatos más por venta nueva, como `tiene_devoluciones_de_servicio`).

**Tests.** `tests/test_erp_offline_vinculo.py`, con el nodo y el central de verdad, el `SyncReceiver` y el `Despachador`, en las tres combinaciones de motores: sin vínculo el payload y la marca son los de antes y no se escribe en la tabla; con vínculo viaja y se conserva (y el buffet lo encuentra con su SQL); pago acreditado, devolución y anulación encuentran la venta por la tabla; con gancho y sin gancho; el gancho ve la venta ya con su vínculo; si el gancho falla (un `ValueError` y un `RuntimeError`) no queda nada en `sales`, `sale_items`, `ventas_pagos`, `caja_movimientos`, `venta_origen_nodo` ni en la tabla del gancho, y el reintento la aplica entera; reaplicar no duplica ni llama dos veces; `source_id` provisorio traducido, sin mapeo (rechazo y recuperación al llegar el alta) y sin tabla declarada; `id_del_central` sobre el `extra`; marcador reservado y `source_id` no entero; central sin la `0010`; `siguiente_numero`; y la `0010` contra SQLite y PostgreSQL (tabla vacía sin tocar `sales`, clave, unicidad y cascada, idempotente, baja y vuelve a subir). Verificado en rojo: con el código de antes fallan todos los que ejercitan el vínculo; con el aplicador pisando el vínculo otra vez, los que lo esperan conservado; sin la vía de la tabla en `venta_del_central`, el de las operaciones siguientes y el de reaplicar; sin la exclusión de `siguiente_numero`, el de numeración.

**No cubre.**
- **Qué escribe `source_type` en el nodo.** `registrar_venta` / `crear_venta` crean la venta con `'pos'` y no reciben un origen: el producto lo pone en su `al_registrar_venta` (un `UPDATE sales`), que `con_outbox` corre antes de encolar. Un parámetro `origen` en `registrar_venta` es una mejora posible, no hecha.
- **Un `source_id` que apunta a algo que el central no tiene y no es provisorio** (positivo): pasa tal cual, como el `cliente_id` positivo; es del producto verificarlo en su `al_materializar`.
- **Bajar la `0010` con ventas anotadas** deja esas ventas con su vínculo pero sin identidad de nodo: sus operaciones siguientes van a revisión manual (`VentaNoSincronizada`). Volver a subir la crea vacía.
- **RestoLibra, VentaLibra y ContaLibra no cambian**: no ponen vínculo, y su venta sigue marcada `libraedge:<node_id>`. LibraCargo no usa `sales` (no depende de este motor): su vínculo (órdenes, comprobantes) irá con sus propias operaciones `cargo.*` y el mapeo de `libraedge`.

**Cómo se adopta** (v0.73.0: menor, migración nueva). Subir el pin; **correr `libracommerce-migrar upgrade --prefijo <producto>` en el central antes de que un nodo suba una venta con vínculo** (sin la `0010` esa venta queda en revisión manual y se reaplica al migrar). Un producto con vínculo propio: lo pone en su `al_registrar_venta`, lo lee `extra_de_venta` si necesita algo más que las dos columnas, y registra `registrar_en(Despachador(), GANCHOS, al_materializar=..., tablas_de_origen={...})` en el central. Sin cambios en libracore ni en libraedge.


## ADR-073 — `serializar_esquema` toma siempre antes el candado del esquema del core: un orden global core → LibraCommerce (2026-10-24)

**El problema (medido).** Con `WEB_CONCURRENCY=2`, un worker de LibraClub murió al arrancar con `TimeoutError: 'libracommerce.esquema': no se pudo tomar el candado en 120 s` (PR `fix/arranque-dos-workers` de LibraClub, demo). Reproducido acá con dos conexiones (`tests/test_esquema_en_paralelo.py::test_una_tabla_de_cruce_no_se_cruza_con_el_arranque_del_core_de_otro_worker`, rojo con el `TimeoutError` exacto a los 6 s del tope del test). La causa es el **orden de los candados**:
- `libracore.db.schema.init_core_schema` toma el candado del core (`CANDADO_DEL_ESQUEMA`) y **después**, cuando el producto sigue con `init_schema()` en la misma transacción, `libracommerce.esquema`. Hasta el commit guarda, además, locks de las tablas del core (`SHARE` por sus `CREATE INDEX IF NOT EXISTS`, y un `ROW EXCLUSIVE` por un `UPDATE ... WHERE caja_id IS NULL` incondicional, éste ya condicional en el motor v1.192.1).
- `serializar_esquema` (ADR-062) tomaba **sólo** `libracommerce.esquema`, en una transacción propia, y las tablas de cruce (`crear_venta_links`, `crear_cliente_lista_precio`) hacen `CREATE TABLE ... REFERENCES` sobre tablas del core, que pide `SHARE ROW EXCLUSIVE` sobre la referenciada.
- Ciclo: A tiene `libracommerce.esquema` y espera la tabla; B tiene la tabla y espera `libracommerce.esquema`. Los dos candados no se ven entre sí como una sola cosa, y con el reintento de `pg_try_advisory_xact_lock` de libracore < 1.192.1 PostgreSQL ni siquiera lo veía como deadlock: 120 s mudos.

**Qué ya existe** (se miró antes): el ADR-062 (esta costura), `libracore.candados.esperar_candado_de_transaccion` y `libracore.db.schema.CANDADO_DEL_ESQUEMA` (ADR-053 del motor, desde v1.168.0). El producto ya lo mitigaba con `una_vez_al_arrancar` (LibraClub, VentaLibra, Contalibra), que oculta el ciclo para su propio arranque sin quitarlo del motor: RestoLibra y cualquier producto nuevo lo siguen teniendo.

**Decisión.** `db.candado.serializar_esquema(conn)` toma **primero el candado del core y después el propio**, siempre. Con un orden único los candados no pueden formar un ciclo entre ellos, y quien espera el del core todavía no tocó ninguna tabla, así que tampoco hay ciclo con las de tabla.
- **Es gratis cuando ya se lo tiene:** los candados asesores son reentrantes por sesión, así que `init_core_schema(conn)` + `init_schema(conn)` + `crear_venta_links(conn)` en la misma conexión (el arranque de un producto) no esperan.
- **Sin cambio de API ni de libracore:** usa `CANDADO_DEL_ESQUEMA` de `libracore.db.schema`, que existe desde v1.168.0 (el piso que ya pide el motor). El arreglo funciona con cualquier libracore >= 1.168; v1.192.1 suma que, si alguien rompe el orden, el ciclo salga como `deadlock detected` en ~1 s y no a los 120 s (ADR-081 del motor).
- **SQLite sigue sin hacer nada ni importar libracore.** El mensaje de `ImportError` con libracore < 1.168 se conserva.
- Mismas garantías de `espera`: el mismo tope para cada uno de los dos candados.

**Alternativas descartadas.**
- **Que `init_core_schema` no tome nada de este motor / separar las transacciones del producto:** el producto elige cómo arma su arranque; el motor no puede obligar a que sus `crear_*` corran siempre dentro de la transacción del core. Un orden de candados es una regla que el motor sí cumple solo.
- **Un solo candado para los dos motores:** el core y este motor se instalan por separado (hay productos de LibraCore sin LibraCommerce) y `init_core_schema` no debe saber de este motor.
- **Que cada producto envuelva el arranque en `una_vez_al_arrancar`:** sigue siendo válido y barato (conexión propia), pero deja al motor con el defecto para el próximo producto. Se mantiene en los que ya lo tienen.

**Alcance dicho.** Sólo cubre los candados del esquema. `erp/compras.py` (`recepcion-de-mercaderia:`) y `erp/reposicion_ordenes.py` toman candados asesores por pedido, no de esquema, y no se tocaron. **No cubre** un DDL propio de un producto que se corra fuera de las funciones de este motor y toque tablas del core sin pasar por `serializar_esquema` (ver el ADR-062).

**Tests.** `tests/test_esquema_en_paralelo.py`: el entrelazado de LibraClub con dos conexiones y `init_core_schema` real (`crear_venta_links` y `crear_cliente_lista_precio`; sin el arreglo falla con el `TimeoutError` del candado de LibraCommerce con libracore anterior y con `deadlock detected` con el v1.192.1), el orden función por función (con el candado del core ocupado, la función espera **sin tener el suyo**: cinco funciones) y la reentrancia. Los 7 que prueban el orden y el entrelazado fallan sin el arreglo.

**Cómo se adopta** (v0.73.1: parche, sin migración, sin cambio de esquema). Subir el pin. Los productos que mitigaron con `una_vez_al_arrancar` pueden dejarlo: es inofensivo.

## ADR-074 — El nombre de la sucursal del nodo viaja con la carga inicial: `nombre_de_la_sucursal_del_nodo` (2026-10-28)

**Problema.** La barra de estado del pie (libracore ADR-075) muestra la sucursal de quien mira, y en la PC del local
(un nodo) el usuario no la trae: el producto no tenía de dónde sacarla. Decisión del humano (2026-10-28): «donde hay
selector, la elegida; en la PC del local, la sucursal de ese nodo; en el resto no se muestra».

**Qué ya existía.** El nodo guarda el **id** de su sucursal en `nodo_local` al hacer la carga inicial (v0.72.0,
`sucursal_guardada_del_nodo`), pero no su nombre, y `branches` no está entre las tablas que espejan los productos.

**Decisión.**
1. `carga_inicial_del_central` suma `sucursal_nombre` (el `name` de `branches`, o `None`). Aditivo: la `version` sigue en
   1; un central anterior no lo manda y un nodo anterior no lo lee.
2. `guardar_sucursal_del_nodo(conn, id, nombre=None)` lo guarda con la clave `sucursal_nombre` de la misma tabla (sin
   revisión nueva). Sin nombre **borra** el que hubiera: sería el de otra sucursal.
3. `nombre_de_la_sucursal_del_nodo(conn)`: el guardado; si no hay (un nodo cargado antes), el de `branches` por el id,
   si el espejo lo tiene; si no, `None`. **Mejor no mostrar nada que una sucursal equivocada.**
4. `sucursal_de_la_barra(conectar, es_nodo)` arma el `sucursal=` de `build_estado_del_sistema_router`: la del usuario si
   la trae (el selector), y si no y es un nodo, la de esta función. Una sola vez acá y no copiada en los cuatro productos con
   nodo y stock (ContaLibra, RestoLibra, VentaLibra, LibraClub). libracore no cambia.
