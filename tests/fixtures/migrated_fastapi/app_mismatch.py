"""Deliberately divergent candidate app (subset of endpoints, mismatching values).

Only the endpoints exercised by the mismatch integration suite are defined
here; missing routes fall through to the 404 handler, which is intentional.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse


def create_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/customers/{customer_id}")
    async def customer(customer_id: str) -> dict[str, Any]:
        return {
            "customer": {"id": customer_id, "name": "Ada Lovelace", "status": "inactive"},
            "metadata": {
                "request_id": uuid.uuid4().hex,
                "generated_at": datetime.now(UTC).isoformat(),
                "version": "1.0",
            },
        }

    @app.get("/api/items")
    async def items(limit: int = 10, q: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "limit": limit,
            "items": [f"item-{index}" for index in range(limit)],
        }
        if q is not None:
            payload["q"] = q
        return payload

    @app.get("/api/search")
    async def search(
        tags: list[str] | None = Query(default=None),  # noqa: B008 — FastAPI idiom
    ) -> dict[str, list[str]]:
        return {"tags": tags or []}

    @app.get("/api/messages/{message_id}")
    async def message(message_id: str) -> dict[str, str]:
        return {"id": message_id}

    @app.get("/api/private")
    async def private() -> dict[str, str]:
        # Deliberate mismatch: this variant forgot the Authorization check.
        return {"status": "authorized"}

    @app.get("/api/boom")
    async def boom() -> None:
        raise RuntimeError("deliberate fixture exception")

    @app.exception_handler(404)
    async def not_found(request: object, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": "not_found"})

    @app.exception_handler(Exception)
    async def internalServerError(request: object, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": "internal"})

    return app


app = create_app()
