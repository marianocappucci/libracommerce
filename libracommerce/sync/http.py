"""HTTP transport for the offline synchronization worker."""

import json
from urllib.request import Request, urlopen

from libracommerce.domain.sync import OutboxOperation
from libracommerce.sync.worker import PushResult


class HttpSyncTransport:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def push(self, operation: OutboxOperation) -> PushResult:
        body = json.dumps({
            "operation_id": operation.operation_id,
            "node_id": operation.node_id,
            "sequence": operation.sequence,
            "operation_type": operation.operation_type,
            "aggregate_type": operation.aggregate_type,
            "aggregate_id": operation.aggregate_id,
            "occurred_at": operation.occurred_at,
            "schema_version": operation.schema_version,
            "payload": operation.payload,
        }).encode("utf-8")
        request = Request(
            f"{self.base_url}/sync/v1/push", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read())
        return PushResult(data["result"], data.get("error"))
