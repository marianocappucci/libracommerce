import sqlite3

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.db.schema import init_schema
from libracommerce.domain.sync import OutboxOperation, SyncOperationStatus


def operation(operation_id="node-1:1", sequence=1):
    return OutboxOperation(
        operation_id=operation_id, node_id="node-1", sequence=sequence,
        operation_type="sale.confirmed", aggregate_type="sale",
        aggregate_id="sale-1", occurred_at="2026-07-25T18:30:00Z",
        schema_version=1, payload={"total": "100.00"},
    )


def test_schema_creates_sync_tables():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"node_identity", "local_sequences", "sync_outbox", "sync_inbox"} <= tables


def test_outbox_round_trip_and_idempotent_retry():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    repo = SqliteCommerceRepository(conn)
    saved = repo.enqueue_operation(operation())
    same = repo.enqueue_operation(operation())
    assert saved == same
    assert len(repo.list_pending_operations()) == 1
    ack = repo.acknowledge_operation(saved.operation_id, "2026-07-25T18:31:00Z")
    assert ack.status == SyncOperationStatus.ACKNOWLEDGED
    assert repo.list_pending_operations() == ()


def test_outbox_rejects_same_sequence_for_different_operation():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    repo = SqliteCommerceRepository(conn)
    repo.enqueue_operation(operation())
    other = operation("node-1:999")
    try:
        repo.enqueue_operation(other)
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("duplicate node sequence must be rejected")
