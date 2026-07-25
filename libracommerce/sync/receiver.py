"""Central-side idempotent receiver for synchronization operations."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from libracommerce.domain.sync import OutboxOperation
from libracommerce.sync.worker import PushResult


@dataclass(frozen=True)
class SyncReceiver:
    conn: sqlite3.Connection
    supported_schema_version: int = 1

    def accept(self, operation: OutboxOperation) -> PushResult:
        if operation.schema_version != self.supported_schema_version:
            return PushResult("rejected", "schema incompatible")
        existing = self.conn.execute(
            "SELECT status FROM sync_inbox WHERE operation_id = ?",
            (operation.operation_id,),
        ).fetchone()
        if existing is not None:
            return PushResult("duplicate")
        try:
            self.conn.execute(
                """INSERT INTO sync_inbox (operation_id, applied_at, status)
                   VALUES (?, ?, 'applied')""",
                (operation.operation_id, datetime.now(timezone.utc).isoformat()),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return PushResult("duplicate")
        return PushResult("accepted")
