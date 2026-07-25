# Módulo offline — contrato de sincronización v0.1

Estado: especificación inicial, pendiente de implementación.

## Objetivo

Permitir que una instancia local de LibraCommerce registre operaciones comerciales durante una caída de internet y las sincronice con el servidor central al recuperar conectividad.

La instancia local se identifica como `node_id` y corre en una mini PC dedicada. El servidor central es la autoridad para configuración, catálogo y precios. Las ventas y movimientos confirmados son operaciones append-only.

## Alcance del MVP

Incluye:

- catálogo y precios previamente descargados;
- ventas confirmadas;
- medios de pago locales, inicialmente efectivo;
- movimientos de stock derivados de ventas;
- outbox local;
- reintentos y aceptación idempotente;
- estado de sincronización visible.

No incluye todavía facturación fiscal offline, Mercado Pago, múltiples nodos concurrentes por sucursal, compras offline ni resolución automática de conflictos financieros.

## Identidad y envelope de operación

Cada nodo recibe una identidad estable durante el provisionamiento:

```text
node_id       = UUID asignado al nodo
branch_id     = sucursal a la que pertenece
sequence      = entero monotónico local
operation_id  = node_id + ":" + sequence
```

El `operation_id` es la clave de idempotencia global. Nunca se reutiliza, aunque una operación falle o se anule.

Envelope conceptual:

```json
{
  "operation_id": "node-uuid:42",
  "node_id": "node-uuid",
  "branch_id": "branch-uuid",
  "sequence": 42,
  "operation_type": "sale.confirmed",
  "aggregate_type": "sale",
  "aggregate_id": "local-sale-uuid",
  "occurred_at": "2026-07-25T18:30:00Z",
  "schema_version": 1,
  "payload": {}
}
```

Los UUID de venta y de cualquier agregado creado offline también deben ser globalmente únicos. No se debe depender de autoincrementos SQLite para identificar entidades que luego viajarán al VPS.

## Tablas locales adicionales

Además de las tablas comerciales existentes, el nodo necesita como mínimo:

```sql
CREATE TABLE node_identity (
    node_id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL,
    installed_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    last_server_cursor TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE local_sequences (
    name TEXT PRIMARY KEY,
    next_value INTEGER NOT NULL
);

CREATE TABLE sync_outbox (
    operation_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    operation_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT,
    acknowledged_at TEXT
);

CREATE UNIQUE INDEX idx_sync_outbox_node_sequence
    ON sync_outbox(node_id, sequence);
CREATE INDEX idx_sync_outbox_pending
    ON sync_outbox(status, next_attempt_at);

CREATE TABLE sync_inbox (
    operation_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    applied_at TEXT,
    status TEXT NOT NULL DEFAULT 'received',
    last_error TEXT
);
```

Los nombres son de referencia y pueden adaptarse al estilo final del paquete. La implementación debe activar foreign keys y usar transacciones explícitas.

## Estados de outbox

- `pending`: creada localmente y aún no enviada.
- `sending`: tomada por el worker; debe recuperarse si el proceso muere.
- `acknowledged`: aceptada e incorporada por el servidor.
- `retryable_error`: error de red o indisponibilidad temporal.
- `manual_review`: error de contrato, conflicto o datos inválidos.

Un timeout de red no significa que la operación no fue aceptada. El worker debe reintentar con el mismo `operation_id`; nunca crear otra operación.

## API conceptual

El contrato central puede exponerse inicialmente mediante REST:

```text
POST /sync/v1/nodes/register
POST /sync/v1/push
GET  /sync/v1/pull?cursor=<cursor>
GET  /sync/v1/nodes/<node_id>/health
```

`POST /sync/v1/push` recibe un lote ordenado por `sequence` y devuelve, por cada operación:

```json
{
  "operation_id": "node-uuid:42",
  "result": "accepted | duplicate | rejected",
  "server_cursor": "cursor-value",
  "error_code": null
}
```

`accepted` significa que se aplicó. `duplicate` significa que ya se había aplicado y es éxito idempotente. `rejected` requiere revisión; no debe descartarse la operación local.

`GET /sync/v1/pull` devuelve cambios autorizados por el servidor y un cursor nuevo. El nodo persiste el cursor en la misma transacción que aplica el lote, para no saltear cambios si se corta la energía.

## Orden y consistencia

Dentro de un mismo nodo, las operaciones se envían en orden de `sequence`. El servidor puede procesar varios nodos en paralelo, pero debe conservar el orden causal de cada nodo.

La confirmación local de una venta debe ser atómica:

1. guardar venta y líneas;
2. guardar movimientos de stock locales;
3. generar envelope;
4. guardar envelope en `sync_outbox`;
5. confirmar la transacción.

Si cualquiera falla, no existe venta confirmada ni evento pendiente.

## Seguridad

- TLS obligatorio entre nodo y VPS;
- credencial por nodo, revocable desde el servidor;
- no almacenar secretos en el repositorio ni en imágenes Docker;
- rotación de credenciales sin perder `node_id`;
- autorización por `branch_id`;
- auditoría de registro, desactivación y sincronización;
- API local accesible solo desde la LAN autorizada.

## Política de recuperación

El worker debe usar backoff exponencial con límite y jitter. Las operaciones `sending` antiguas se devuelven a `retryable_error` al iniciar el nodo. El sistema debe alertar cuando:

- haya operaciones pendientes durante más de un umbral configurable;
- el disco esté casi lleno;
- el último backup falle;
- el nodo no haya contactado al servidor durante el máximo permitido;
- exista una operación `manual_review`.

El reemplazo de una mini PC requiere restaurar un backup o registrar un nodo nuevo siguiendo un procedimiento que evite duplicar la identidad activa.

## Criterios de aceptación del MVP

1. Crear y confirmar una venta sin red.
2. Reiniciar la mini PC antes de sincronizar y conservar la outbox.
3. Recuperar red y sincronizar automáticamente.
4. Repetir la misma petición y obtener `duplicate`, sin duplicar venta ni stock.
5. Cortar la red después de que el servidor recibe pero antes del ACK, sin duplicación.
6. Cortar energía durante la transacción y recuperar una base consistente.
7. Rechazar una operación inválida y dejarla visible para revisión.
8. Aplicar un lote pull y su cursor de forma atómica.
9. Restaurar backup en otra instalación de prueba.
10. Verificar que catálogo y precios centralizados se actualizan sin modificar ventas históricas.

## Decisiones pendientes

- formato definitivo de UUID y cursor;
- autenticación concreta por nodo;
- límite de días/operaciones sin sincronización;
- política de stock negativo;
- versión y hardware exactos de Ubuntu Server LTS;
- si la outbox vive en LibraCommerce o en un paquete transversal de infraestructura;
- contrato de compras y devoluciones para una segunda etapa.
