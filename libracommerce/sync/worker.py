"""Durable local outbox worker, independent from HTTP transport."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from libracommerce.domain.sync import OutboxOperation


@dataclass(frozen=True)
class PushResult:
    result: str  # accepted, duplicate, rejected
    error: str | None = None


class SyncTransport(Protocol):
    def push(self, operation: OutboxOperation) -> PushResult: ...


class OutboxWorker:
    def __init__(self, repository, transport: SyncTransport):
        self.repository = repository
        self.transport = transport

    def run_once(self, limit=100) -> int:
        processed = 0
        for operation in self.repository.list_pending_operations(limit):
            self.repository.mark_operation_sending(operation.operation_id)
            try:
                result = self.transport.push(operation)
            except Exception as exc:
                self.repository.retry_operation(
                    operation.operation_id, str(exc), self._now()
                )
                processed += 1
                continue
            if result.result in {"accepted", "duplicate"}:
                self.repository.acknowledge_operation(operation.operation_id, self._now())
            else:
                self.repository.mark_operation_manual_review(
                    operation.operation_id, result.error or "operación rechazada"
                )
            processed += 1
        return processed

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
