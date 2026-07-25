# LibraCommerce

Motor comercial reutilizable de la familia Libra.

## Alcance inicial

LibraCommerce contiene contratos y lógica de dominio para:

- entidades comerciales;
- catálogo de productos y servicios;
- ubicaciones e inventario;
- ventas y líneas de venta;
- eventos/puertos de integración.

No depende de FastAPI, SQLite, LibraCore, ARCA, MercadoPago ni de un producto vertical. Los adaptadores concretos se implementan fuera del dominio.

## Módulo offline

El contrato de sincronización está en [`docs/OFFLINE_SYNC.md`](docs/OFFLINE_SYNC.md): identidad de nodo, outbox, idempotencia, API conceptual y criterios de aceptación. Ya implementado — la persistencia, el worker, el transporte y el receptor viven en el paquete transversal `libraedge` (extra opcional `offline`/`offline-server`); LibraCommerce solo traduce entre su dominio (`Sale`) y el `OutboxOperation` genérico (`libracommerce.integrations.libraedge`).

## Desarrollo

```bash
python -m pytest
```
