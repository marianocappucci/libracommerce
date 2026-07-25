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


def test_offline_sale_and_outbox_are_atomic(repo=None):
    import sqlite3
    from decimal import Decimal
    from libracommerce.domain.sales import Sale, SaleItem, SaleStatus
    from libracommerce.domain.catalog import CatalogItemType

    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    repository = SqliteCommerceRepository(conn)
    sale = Sale(
        None, "OFF-0001",
        (SaleItem(CatalogItemType.SERVICE, "Servicio", Decimal("1"), Decimal("100")),),
        status=SaleStatus.CONFIRMED, total=Decimal("100"),
    )
    saved, event = repository.save_offline_sale(sale, "node-1", "2026-07-25T18:30:00Z")
    assert saved.id is not None
    assert event.operation_id == "node-1:1"
    assert repository.get_operation(event.operation_id).payload["sale_id"] == saved.id

    second, event2 = repository.save_offline_sale(sale.__class__(None, "OFF-0002", sale.items, status=SaleStatus.CONFIRMED, total=Decimal("100")), "node-1", "2026-07-25T18:31:00Z")
    assert second.id != saved.id
    assert event2.sequence == 2


def test_offline_sale_rolls_back_when_outbox_insert_fails():
    from decimal import Decimal
    from libracommerce.domain.sales import Sale, SaleItem, SaleStatus
    from libracommerce.domain.catalog import CatalogItemType
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    repository = SqliteCommerceRepository(conn)
    sale = Sale(None, "OFF-FAIL", (SaleItem(CatalogItemType.SERVICE, "Servicio", Decimal("1"), Decimal("100")),), status=SaleStatus.CONFIRMED, total=Decimal("100"))
    conn.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON sync_outbox BEGIN SELECT RAISE(ABORT, 'boom'); END")
    try:
        repository.save_offline_sale(sale, "node-1", "2026-07-25T18:30:00Z")
    except sqlite3.IntegrityError:
        pass
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0


def test_worker_acknowledges_accepted_and_duplicate_operations():
    from libracommerce.sync.worker import OutboxWorker, PushResult
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    repository = SqliteCommerceRepository(conn)
    repository.enqueue_operation(operation())
    class Transport:
        def push(self, op):
            return PushResult("accepted")
    assert OutboxWorker(repository, Transport()).run_once() == 1
    assert repository.get_operation("node-1:1").status == SyncOperationStatus.ACKNOWLEDGED


def test_worker_retries_transport_failures_and_marks_rejections_for_review():
    from libracommerce.sync.worker import OutboxWorker, PushResult
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    repository = SqliteCommerceRepository(conn)
    repository.enqueue_operation(operation())
    class FailingTransport:
        def push(self, op):
            raise ConnectionError("VPS offline")
    OutboxWorker(repository, FailingTransport()).run_once()
    saved = repository.get_operation("node-1:1")
    assert saved.status == SyncOperationStatus.RETRYABLE_ERROR
    assert saved.attempts == 1
    assert saved.last_error == "VPS offline"

    repository.enqueue_operation(operation("node-1:2", 2))
    class RejectingTransport:
        def push(self, op):
            return PushResult("rejected", "schema incompatible")
    OutboxWorker(repository, RejectingTransport()).run_once()
    assert repository.get_operation("node-1:2").status == SyncOperationStatus.MANUAL_REVIEW


def test_receiver_accepts_once_and_returns_duplicate_afterwards():
    from libracommerce.sync.receiver import SyncReceiver
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    receiver = SyncReceiver(conn)
    event = operation()
    assert receiver.accept(event).result == "accepted"
    assert receiver.accept(event).result == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM sync_inbox").fetchone()[0] == 1


def test_receiver_rejects_unknown_schema_version():
    from dataclasses import replace
    from libracommerce.sync.receiver import SyncReceiver
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    result = SyncReceiver(conn).accept(replace(operation(), schema_version=99))
    assert result.result == "rejected"
    assert "schema" in result.error


def test_fastapi_adapter_contract_when_dependency_is_available():
    fastapi = __import__("pytest").importorskip("fastapi")
    from fastapi.testclient import TestClient
    from libracommerce.sync.api import create_sync_app
    from libracommerce.sync.receiver import SyncReceiver
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    init_schema(conn)
    client = TestClient(create_sync_app(SyncReceiver(conn)))
    event = operation()
    response = client.post("/sync/v1/push", json={
        "operation_id": event.operation_id, "node_id": event.node_id,
        "sequence": event.sequence, "operation_type": event.operation_type,
        "aggregate_type": event.aggregate_type, "aggregate_id": event.aggregate_id,
        "occurred_at": event.occurred_at, "schema_version": event.schema_version,
        "payload": event.payload,
    })
    assert response.status_code == 200
    assert response.json()["result"] == "accepted"


def test_receiver_applies_offline_sale_to_central_tables():
    from decimal import Decimal
    from libracommerce.domain.catalog import CatalogItemType
    from libracommerce.domain.sales import Sale, SaleItem, SaleStatus
    from libracommerce.sync.receiver import SyncReceiver
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    local = SqliteCommerceRepository(conn)
    sale = Sale(None, "OFF-APPLY", (SaleItem(CatalogItemType.SERVICE, "Consulta", Decimal("1"), Decimal("250")),), status=SaleStatus.CONFIRMED, total=Decimal("250"))
    _, event = local.save_offline_sale(sale, "node-apply", "2026-07-25T18:30:00Z")
    # Remove the local sale to simulate a separate central database.
    conn.execute("DELETE FROM sale_items"); conn.execute("DELETE FROM sales"); conn.commit()
    receiver = SyncReceiver(conn)
    assert receiver.accept(event).result == "accepted"
    central_id = conn.execute("SELECT id FROM sales WHERE number = 'OFF-APPLY'").fetchone()[0]
    central = local.get_sale(central_id)
    assert central.number == "OFF-APPLY"
    assert central.source_type == "offline:node-apply"
    assert central.items[0].description_snapshot == "Consulta"
    assert receiver.accept(event).result == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1


def test_libraedge_adapter_translates_confirmed_sale():
    __import__("pytest").importorskip("libraedge")
    from decimal import Decimal
    from libracommerce.domain.catalog import CatalogItemType
    from libracommerce.domain.sales import Sale, SaleItem, SaleStatus
    from libracommerce.integrations.libraedge import sale_to_edge_operation
    sale = Sale(None, "V-EDGE", (SaleItem(CatalogItemType.SERVICE, "Consulta", Decimal("1"), Decimal("300")),), status=SaleStatus.CONFIRMED, total=Decimal("300"))
    event = sale_to_edge_operation(sale, "node-1", 7, "2026-07-25T19:00:00Z")
    assert event.operation_id == "node-1:7"
    assert event.payload["items"][0]["description_snapshot"] == "Consulta"
