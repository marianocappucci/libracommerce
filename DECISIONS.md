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
