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
  (`Location`, `StockMovement`), `sales` (`Sale`, `SaleItem`, `SalePayment`),
  `purchasing` (`PurchaseOrder`, `PurchaseReceipt`), `scale` (lectura de códigos
  de balanza, `parse_scale_barcode`) y `presets` (rubros comerciales y sus ejes
  de variante).
- **`ports/`** — los contratos que el dominio necesita del exterior:
  `persistence.CommerceRepository` (la interfaz de almacenamiento) e
  `integrations.CommerceEventPublisher` / `SaleConfirmedEvent` (publicación de
  eventos de dominio).
- **`usecases/`** — la capa de aplicación que orquesta dominio + puertos:
  `sales` (`confirm_sale`, `cancel_sale`, `return_sale_items`), `inventory`
  (`verificar_disponibilidad`, `transfer_stock`, error `StockInsuficienteError`),
  `purchasing` (`confirm_purchase_receipt`) y `presets`.
- **`db/`** — un adaptador de persistencia concreto: `repository`
  (`SqliteCommerceRepository`), `schema` (`init_schema`), `migrations` (cadena de
  migraciones incrementales `_migration_0001…0009`) y `auditoria`
  (`RepositorioAuditado`, `ActividadRepository`).
- **`adapters/`** e **`integrations/`** — puentes hacia afuera: `adapters/
  contalibra` lee datos del schema legado de Contalibra; `integrations/libraedge`
  traduce una venta confirmada a una operación de sincronización del nodo edge
  (`sale_to_edge_operation`, `apply_confirmed_sale_operation`).
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
