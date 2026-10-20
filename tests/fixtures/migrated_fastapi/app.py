"""Behaviorally equivalent candidate API used by apiparity integration tests.

This is the migrated twin of ``legacy_flask.app``. Response shapes must stay
identical to the baseline for the equivalence suite to pass. The deliberate
divergence lives in ``create_app(mismatch=True)`` (exposed by
``app_mismatch.py``), which flips ``/api/customers/{id}`` to return
``status: "inactive"`` — producing a single, exact JSON Pointer mismatch at
``/customer/status``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def create_app(*, mismatch: bool = False) -> FastAPI:
    app = FastAPI()
    orders: list[dict[str, Any]] = []

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/customers/{customer_id}")
    async def customer(customer_id: str) -> dict[str, Any]:
        status = "inactive" if mismatch else "active"
        return {
            "customer": {"id": customer_id, "name": "Ada Lovelace", "status": status},
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
    async def search(request: Request) -> dict[str, list[str]]:
        return {"tags": request.query_params.getlist("tags")}

    @app.get("/api/messages/{message_id}")
    async def message(message_id: str) -> dict[str, str]:
        return {"id": message_id}

    @app.post("/api/orders")
    async def create_order(request: Request) -> JSONResponse:
        body: Any = None
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            body = {}
        fields: dict[str, str] = {}
        item = body.get("item")
        if not isinstance(item, str) or not item:
            fields["item"] = "required"
        quantity = body.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            fields["quantity"] = "must be a positive integer"
        if fields:
            return JSONResponse(
                status_code=422,
                content={"error": "validation_error", "fields": fields},
            )
        order = {"id": len(orders) + 1, "item": item, "quantity": quantity}
        orders.append(order)
        return JSONResponse(status_code=201, content={"order": order})

    @app.get("/api/orders")
    async def list_orders() -> dict[str, list[dict[str, Any]]]:
        return {"orders": orders}

    @app.get("/api/private", response_model=None)
    async def private(request: Request) -> JSONResponse | dict[str, str]:
        if not request.headers.get("Authorization"):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        return {"status": "authorized"}

    @app.get("/api/boom")
    async def boom() -> None:
        raise RuntimeError("deliberate fixture exception")

    @app.get("/api/session", response_model=None)
    async def session() -> JSONResponse:
        response = JSONResponse(content={"credentials": {"token": "hunter2"}, "session": "active"})
        response.set_cookie("sid", "hunter2", httponly=True)
        return response

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": "not_found"})

    @app.exception_handler(Exception)
    async def internalServerError(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": "internal"})

    return app


app = create_app()
