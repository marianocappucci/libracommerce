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

## Desarrollo

```bash
python -m pytest
```
