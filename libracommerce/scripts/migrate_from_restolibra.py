"""Migracion de datos, de una sola corrida, del catalogo/stock/ventas/
recetas de Restolibra hacia el esquema de LibraCommerce -- P8 del plan de
consolidacion de la familia Libra (ver
wiki/analyses/migracion-p8-restolibra-libracommerce.md).

Restolibra es un fork de Contalibra con el mismo schema exacto de
libracore para productos/movimientos_stock/ventas/listas_precio
(mismo paquete libracore instalado en ambos). `migrate_from_contalibra.migrate()`
es puramente schema-shaped -- nunca referencia nada especifico de
Contalibra en su logica, solo en sus docstrings/nombres historicos (fue
P7, el primer consumidor) -- asi que se reusa tal cual para esa parte, sin
duplicarla.

Lo unico que agrega este modulo es el dominio exclusivo de Restolibra:
recetas/receta_items, que no existen en Contalibra y no tienen tabla
propia en LibraCommerce (quedan como tablas de Restolibra, igual que
`venta_links` quedo propio de Contalibra -- ver
wiki/analyses/arquitectura-familia-libra-alcance.md, "Recetas, elaborados,
food cost y mermas: exclusivo gastronomico"). Como los IDs de
catalog_items se preservan 1:1 desde productos.id, las filas de
recetas/receta_items no necesitan reescribirse: solo se repunta el
FOREIGN KEY de `productos(id)` a `catalog_items(id)` (rebuild de 12 pasos
de SQLite, mismo patron que `_repoint_lista_precio_items_fk`).

`pedidos.venta_id` tambien tiene una FK dura a `ventas(id)` -- a
diferencia de Contalibra, que no tiene tabla `pedidos`, este caso no lo
cubria ningun repoint de la migracion base. Bug real encontrado
end-to-end (Fase 3, 2026-07-27): `cobrar_pedido` fallaba con
`sqlite3.IntegrityError: FOREIGN KEY constraint failed` al hacer
`UPDATE pedidos SET venta_id=?` con un id de `sales`, porque la FK
todavia apuntaba a la vieja `ventas`. Se repunta igual que
recetas/receta_items.

Segundo bug real, tambien encontrado end-to-end (mismo dia): `pedidos`
es tabla padre de `comandas`/`pedido_items` (`pedido_id REFERENCES
pedidos(id)`). El rebuild de 12 pasos de SQLite (`ALTER TABLE pedidos
RENAME TO pedidos_old`) hace que SQLite **reescriba automaticamente** el
texto de la FK en las tablas hijas para que apunten a `pedidos_old` --
comportamiento estandar de `ALTER TABLE ... RENAME TO` desde SQLite
3.25. Como el nuevo `pedidos` se crea con `CREATE TABLE` (no un rename),
esa reescritura automatica nunca se corrige sola, y `comandas`/
`pedido_items` quedan apuntando permanentemente a una tabla que ya no
existe (`sqlite3.OperationalError: no such table: main.pedidos_old` al
insertar). Se resuelve reconstruyendo tambien esas dos tablas, en orden
de dependencia (pedidos -> comandas -> pedido_items, porque
`pedido_items.comanda_id` referencia `comandas`, que tambien se
reconstruye). De paso, `pedido_items.producto_id` -- que nunca se habia
tocado -- se repunta de `productos` a `catalog_items`, mismo criterio
que `receta_items.ingrediente_id`.
"""
import sqlite3
from dataclasses import dataclass

from libracommerce.scripts.migrate_from_contalibra import (
    MigrationReport,
    migrate as _migrate_base,
)


@dataclass
class RestolibraMigrationReport(MigrationReport):
    recetas_repointed: bool = False
    receta_items_repointed: bool = False
    pedidos_repointed: bool = False
    comandas_repointed: bool = False
    pedido_items_repointed: bool = False


def _repoint_recetas_fk(conn: sqlite3.Connection) -> bool:
    fks = conn.execute("PRAGMA foreign_key_list(recetas)").fetchall()
    if not any(row[2] == "productos" for row in fks):
        return False  # ya reapuntada, o base sin recetas

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("ALTER TABLE recetas RENAME TO recetas_old")
        conn.execute(
            """
            CREATE TABLE recetas (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                producto_id     INTEGER NOT NULL UNIQUE REFERENCES catalog_items(id) ON DELETE CASCADE,
                rinde           REAL NOT NULL DEFAULT 1,
                rinde_unidad    TEXT NOT NULL DEFAULT 'u',
                rendimiento_pct REAL NOT NULL DEFAULT 100,
                activo          INTEGER NOT NULL DEFAULT 1,
                notas           TEXT DEFAULT '',
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "INSERT INTO recetas (id, producto_id, rinde, rinde_unidad, rendimiento_pct, "
            "activo, notas, created_at, updated_at) "
            "SELECT id, producto_id, rinde, rinde_unidad, rendimiento_pct, activo, notas, "
            "created_at, updated_at FROM recetas_old"
        )
        conn.execute("DROP TABLE recetas_old")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return True


def _repoint_receta_items_fk(conn: sqlite3.Connection) -> bool:
    fks = conn.execute("PRAGMA foreign_key_list(receta_items)").fetchall()
    if not any(row[2] == "productos" for row in fks):
        return False

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("ALTER TABLE receta_items RENAME TO receta_items_old")
        conn.execute(
            """
            CREATE TABLE receta_items (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                receta_id      INTEGER NOT NULL REFERENCES recetas(id) ON DELETE CASCADE,
                ingrediente_id INTEGER NOT NULL REFERENCES catalog_items(id) ON DELETE CASCADE,
                cantidad       REAL NOT NULL DEFAULT 0,
                created_at     TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "INSERT INTO receta_items (id, receta_id, ingrediente_id, cantidad, created_at) "
            "SELECT id, receta_id, ingrediente_id, cantidad, created_at FROM receta_items_old"
        )
        conn.execute("DROP TABLE receta_items_old")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return True


def _repoint_pedidos_venta_fk(conn: sqlite3.Connection) -> bool:
    fks = conn.execute("PRAGMA foreign_key_list(pedidos)").fetchall()
    if not any(row[2] == "ventas" for row in fks):
        return False  # ya reapuntada, o base sin pedidos

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("ALTER TABLE pedidos RENAME TO pedidos_old")
        conn.execute(
            """
            CREATE TABLE pedidos (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                numero         TEXT NOT NULL,
                canal          TEXT NOT NULL DEFAULT 'salon',
                mesa_id        INTEGER REFERENCES mesas(id) ON DELETE SET NULL,
                estado         TEXT NOT NULL DEFAULT 'abierto',
                comensales     INTEGER NOT NULL DEFAULT 1,
                usuario_id     INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                cliente_id     INTEGER REFERENCES clients(id) ON DELETE SET NULL,
                cliente_nombre TEXT DEFAULT '',
                direccion      TEXT DEFAULT '',
                telefono       TEXT DEFAULT '',
                repartidor     TEXT DEFAULT '',
                costo_envio    REAL NOT NULL DEFAULT 0,
                observaciones  TEXT DEFAULT '',
                venta_id       INTEGER REFERENCES sales(id) ON DELETE SET NULL,
                created_at     TEXT DEFAULT (datetime('now')),
                updated_at     TEXT DEFAULT (datetime('now')),
                hora_retiro    TEXT DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT INTO pedidos (id, numero, canal, mesa_id, estado, comensales, usuario_id, "
            "cliente_id, cliente_nombre, direccion, telefono, repartidor, costo_envio, "
            "observaciones, venta_id, created_at, updated_at, hora_retiro) "
            "SELECT id, numero, canal, mesa_id, estado, comensales, usuario_id, "
            "cliente_id, cliente_nombre, direccion, telefono, repartidor, costo_envio, "
            "observaciones, venta_id, created_at, updated_at, hora_retiro FROM pedidos_old"
        )
        conn.execute("DROP TABLE pedidos_old")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return True


def _repoint_comandas_fk(conn: sqlite3.Connection) -> bool:
    """Corre DESPUES de `_repoint_pedidos_venta_fk`: el rename de `pedidos`
    dejo `comandas.pedido_id` apuntando a `pedidos_old` (ver docstring del
    modulo). Si `pedidos` nunca se reapunto (base ya migrada, o instalacion
    nueva que ya nace con el schema correcto), esto es un no-op."""
    fks = conn.execute("PRAGMA foreign_key_list(comandas)").fetchall()
    if not any(row[2] == "pedidos_old" for row in fks):
        return False

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("ALTER TABLE comandas RENAME TO comandas_old")
        conn.execute(
            """
            CREATE TABLE comandas (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id  INTEGER NOT NULL REFERENCES pedidos(id) ON DELETE CASCADE,
                estacion   TEXT NOT NULL,
                numero     INTEGER NOT NULL DEFAULT 0,
                estado     TEXT NOT NULL DEFAULT 'pendiente',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                preparacion_at TEXT,
                listo_at TEXT,
                entregado_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO comandas (id, pedido_id, estacion, numero, estado, created_at, "
            "updated_at, preparacion_at, listo_at, entregado_at) "
            "SELECT id, pedido_id, estacion, numero, estado, created_at, "
            "updated_at, preparacion_at, listo_at, entregado_at FROM comandas_old"
        )
        conn.execute("DROP TABLE comandas_old")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return True


def _repoint_pedido_items_fk(conn: sqlite3.Connection) -> bool:
    """Corre DESPUES de `_repoint_pedidos_venta_fk` y `_repoint_comandas_fk`
    -- `pedido_items` referencia a ambas, asi que se reconstruye al final
    para heredar los nombres ya corregidos. De paso repunta `producto_id`
    de `productos` a `catalog_items` (nunca lo cubria ningun repoint
    anterior)."""
    fks = conn.execute("PRAGMA foreign_key_list(pedido_items)").fetchall()
    necesita_fix = any(row[2] in ("pedidos_old", "comandas_old", "productos") for row in fks)
    if not necesita_fix:
        return False

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("ALTER TABLE pedido_items RENAME TO pedido_items_old")
        conn.execute(
            """
            CREATE TABLE pedido_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id   INTEGER NOT NULL REFERENCES pedidos(id) ON DELETE CASCADE,
                comanda_id  INTEGER REFERENCES comandas(id) ON DELETE SET NULL,
                producto_id INTEGER REFERENCES catalog_items(id) ON DELETE SET NULL,
                nombre      TEXT NOT NULL,
                qty         REAL NOT NULL DEFAULT 1,
                precio      REAL NOT NULL DEFAULT 0,
                subtotal    REAL NOT NULL DEFAULT 0,
                estacion    TEXT DEFAULT '',
                nota        TEXT DEFAULT '',
                estado      TEXT NOT NULL DEFAULT 'nuevo',
                created_at  TEXT DEFAULT (datetime('now')),
                modificadores TEXT DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT INTO pedido_items (id, pedido_id, comanda_id, producto_id, nombre, qty, "
            "precio, subtotal, estacion, nota, estado, created_at, modificadores) "
            "SELECT id, pedido_id, comanda_id, producto_id, nombre, qty, "
            "precio, subtotal, estacion, nota, estado, created_at, modificadores FROM pedido_items_old"
        )
        conn.execute("DROP TABLE pedido_items_old")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return True


def migrate(conn: sqlite3.Connection) -> RestolibraMigrationReport:
    """Corre la migracion base (identica a Contalibra) y despues repunta
    recetas/receta_items/pedidos/comandas/pedido_items, en ese orden (cada
    uno depende de que el anterior ya haya quedado con su nombre final --
    ver docstring del modulo sobre la reescritura automatica de FK de
    SQLite). Misma politica que la base: falla ruidosamente si se corre
    dos veces sobre el mismo destino, pensada para una copia limpia
    (Fase 1) y luego, con confirmacion explicita, produccion real
    (Fase 4)."""
    base_report = _migrate_base(conn)
    recetas_repointed = _repoint_recetas_fk(conn)
    receta_items_repointed = _repoint_receta_items_fk(conn)
    pedidos_repointed = _repoint_pedidos_venta_fk(conn)
    comandas_repointed = _repoint_comandas_fk(conn)
    pedido_items_repointed = _repoint_pedido_items_fk(conn)
    conn.commit()
    return RestolibraMigrationReport(
        **base_report.__dict__,
        recetas_repointed=recetas_repointed,
        receta_items_repointed=receta_items_repointed,
        pedidos_repointed=pedidos_repointed,
        comandas_repointed=comandas_repointed,
        pedido_items_repointed=pedido_items_repointed,
    )
