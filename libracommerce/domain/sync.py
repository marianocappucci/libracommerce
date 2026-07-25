"""Domain contracts for durable offline synchronization."""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SyncOperationStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    ACKNOWLEDGED = "acknowledged"
    RETRYABLE_ERROR = "retryable_error"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True)
class OutboxOperation:
    operation_id: str
    node_id: str
    sequence: int
    operation_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    schema_version: int
    payload: dict[str, Any]
    status: SyncOperationStatus = SyncOperationStatus.PENDING
    attempts: int = 0
    next_attempt_at: str | None = None
    last_error: str | None = None
    created_at: str | None = None
    sent_at: str | None = None
    acknowledged_at: str | None = None

    def payload_json(self) -> str:
        return json.dumps(self.payload, sort_keys=True, separators=(",", ":"))
