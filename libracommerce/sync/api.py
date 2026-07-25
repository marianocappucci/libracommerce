"""FastAPI adapter for the central synchronization receiver."""

from libracommerce.domain.sync import OutboxOperation


def create_sync_app(receiver):
    """Build the HTTP app around an already-configured SyncReceiver.

    FastAPI is optional at library level; only the central deployment needs it.
    """
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI es opcional; instalar libracommerce[offline-server]"
        ) from exc

    app = FastAPI(title="LibraCommerce Sync API", version="0.1")

    @app.post("/sync/v1/push")
    def push(payload: dict):
        required = {
            "operation_id", "node_id", "sequence", "operation_type",
            "aggregate_type", "aggregate_id", "occurred_at",
            "schema_version", "payload",
        }
        missing = sorted(required - payload.keys())
        if missing:
            raise HTTPException(422, detail={"missing": missing})
        try:
            operation = OutboxOperation(
                operation_id=payload["operation_id"], node_id=payload["node_id"],
                sequence=int(payload["sequence"]),
                operation_type=payload["operation_type"],
                aggregate_type=payload["aggregate_type"],
                aggregate_id=payload["aggregate_id"],
                occurred_at=payload["occurred_at"],
                schema_version=int(payload["schema_version"]),
                payload=payload["payload"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, detail="invalid operation") from exc
        result = receiver.accept(operation)
        return {"operation_id": operation.operation_id, "result": result.result, "error": result.error}

    return app
