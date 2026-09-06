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
