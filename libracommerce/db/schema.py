import sqlite3


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS parties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            party_type TEXT NOT NULL,
            display_name TEXT NOT NULL,
            legal_name TEXT,
            tax_id TEXT,
            tax_id_type TEXT,
            email TEXT,
            phone TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            parent_id INTEGER REFERENCES categories(id),
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(parent_id, name)
        );

        CREATE TABLE IF NOT EXISTS catalog_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            category_id INTEGER REFERENCES categories(id),
            unit_code TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            sellable INTEGER NOT NULL DEFAULT 1,
            purchasable INTEGER NOT NULL DEFAULT 1,
            tax_profile TEXT,
            default_sale_price NUMERIC NOT NULL DEFAULT 0,
            default_cost NUMERIC NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            branch_id INTEGER,
            location_type TEXT NOT NULL DEFAULT 'warehouse',
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES catalog_items(id),
            location_id INTEGER NOT NULL REFERENCES locations(id),
            movement_type TEXT NOT NULL,
            quantity_delta NUMERIC NOT NULL CHECK (quantity_delta <> 0),
            occurred_at TEXT NOT NULL,
            source_type TEXT,
            source_id INTEGER,
            unit_cost NUMERIC,
            lot_code TEXT,
            expires_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_stock_item_location
            ON stock_movements(item_id, location_id);
        """
    )
