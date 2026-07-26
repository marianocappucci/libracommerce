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

        CREATE TABLE IF NOT EXISTS units (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            allows_fraction INTEGER NOT NULL DEFAULT 0,
            decimal_scale INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS catalog_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            category_id INTEGER REFERENCES categories(id),
            unit_code TEXT NOT NULL REFERENCES units(code),
            active INTEGER NOT NULL DEFAULT 1,
            sellable INTEGER NOT NULL DEFAULT 1,
            purchasable INTEGER NOT NULL DEFAULT 1,
            tax_profile TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            default_sale_price NUMERIC NOT NULL DEFAULT 0,
            default_cost NUMERIC NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS item_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES catalog_items(id),
            code_type TEXT NOT NULL,
            code TEXT NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            UNIQUE(code_type, code)
        );

        CREATE INDEX IF NOT EXISTS idx_item_codes_item
            ON item_codes(item_id);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_item_codes_one_primary_per_item
            ON item_codes(item_id) WHERE is_primary = 1;

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

        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'draft',
            customer_party_id INTEGER REFERENCES parties(id),
            branch_id INTEGER,
            register_id INTEGER,
            source_type TEXT NOT NULL DEFAULT 'pos',
            source_id INTEGER,
            subtotal NUMERIC NOT NULL DEFAULT 0,
            discount_total NUMERIC NOT NULL DEFAULT 0,
            tax_total NUMERIC NOT NULL DEFAULT 0,
            total NUMERIC NOT NULL DEFAULT 0,
            confirmed_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id),
            kind TEXT NOT NULL,
            item_id INTEGER REFERENCES catalog_items(id),
            description_snapshot TEXT NOT NULL,
            quantity NUMERIC NOT NULL,
            unit_price NUMERIC NOT NULL,
            discount_amount NUMERIC NOT NULL DEFAULT 0,
            tax_rate NUMERIC NOT NULL DEFAULT 0,
            tax_amount NUMERIC NOT NULL DEFAULT 0,
            unit_cost_snapshot NUMERIC,
            CHECK (kind != 'product' OR item_id IS NOT NULL)
        );

        CREATE INDEX IF NOT EXISTS idx_sale_items_sale
            ON sale_items(sale_id);

        CREATE TABLE IF NOT EXISTS purchase_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT NOT NULL UNIQUE,
            supplier_party_id INTEGER NOT NULL REFERENCES parties(id),
            branch_id INTEGER,
            status TEXT NOT NULL DEFAULT 'draft',
            ordered_at TEXT,
            expected_at TEXT,
            notes TEXT NOT NULL DEFAULT '',
            created_by INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS purchase_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_order_id INTEGER NOT NULL REFERENCES purchase_orders(id),
            item_id INTEGER NOT NULL REFERENCES catalog_items(id),
            quantity_ordered NUMERIC NOT NULL,
            quantity_received NUMERIC NOT NULL DEFAULT 0,
            unit_cost NUMERIC NOT NULL,
            tax_rate NUMERIC NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_purchase_order_items_order
            ON purchase_order_items(purchase_order_id);

        CREATE TABLE IF NOT EXISTS purchase_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_order_id INTEGER REFERENCES purchase_orders(id),
            supplier_party_id INTEGER NOT NULL REFERENCES parties(id),
            status TEXT NOT NULL DEFAULT 'draft',
            received_at TEXT,
            document_reference TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS purchase_receipt_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL REFERENCES purchase_receipts(id),
            item_id INTEGER NOT NULL REFERENCES catalog_items(id),
            quantity NUMERIC NOT NULL,
            unit_cost NUMERIC NOT NULL,
            lot_code TEXT,
            expires_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_purchase_receipt_items_receipt
            ON purchase_receipt_items(receipt_id);
        """
    )
