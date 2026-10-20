"""Behaviorally frozen baseline API used by apiparity integration tests.

Any response change here must be mirrored in ``migrated_fastapi`` (the
equivalent candidate) — the two packages exist to be compared. The mismatch
lives only in ``migrated_fastapi.app_mismatch``.

Endpoint map (see REQUIREMENTS.md §18.2):

* ``GET /health`` — trivial JSON.
* ``GET /api/customers/{id}`` — nested JSON with dynamic ``metadata.*``.
* ``GET /api/items`` — query-parameter defaults (``limit=10``).
* ``GET /api/search`` — repeated query parameters (``tags``).
* ``GET /api/messages/{id}`` — path parameters incl. unicode/space decoding.
* ``POST /api/orders`` — hand-rolled body validation + per-process store.
* ``GET /api/orders`` — list state-changing results.
* ``GET /api/private`` — Authorization required → identical 401 shape.
* ``GET /api/boom`` — raises → identical 500 shape.
* unknown routes → identical 404 shape.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from flask import Flask, jsonify, request


def create_app() -> Flask:
    """Application factory (exercises ``--old-factory`` as well as plain import)."""
    app = Flask(__name__)
    orders: list[dict[str, object]] = []

    @app.get("/health")
    def health() -> object:
        return jsonify({"status": "ok"})

    @app.get("/api/customers/<customer_id>")
    def customer(customer_id: str) -> object:
        return jsonify(
            {
                "customer": {
                    "id": customer_id,
                    "name": "Ada Lovelace",
                    "status": "active",
                },
                "metadata": {
                    "request_id": uuid.uuid4().hex,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "version": "1.0",
                },
            }
        )

    @app.get("/api/items")
    def items() -> tuple[object, int] | object:
        limit_raw = request.args.get("limit", "10")
        try:
            limit = int(limit_raw)
        except ValueError:
            return (
                jsonify({"error": "validation_error", "fields": {"limit": "must be an integer"}}),
                422,
            )
        payload: dict[str, object] = {
            "limit": limit,
            "items": [f"item-{index}" for index in range(limit)],
        }
        q = request.args.get("q")
        if q is not None:
            payload["q"] = q
        return jsonify(payload)

    @app.get("/api/search")
    def search() -> object:
        return jsonify({"tags": request.args.getlist("tags")})

    @app.get("/api/messages/<message_id>")
    def message(message_id: str) -> object:
        return jsonify({"id": message_id})

    @app.post("/api/orders")
    def create_order() -> tuple[object, int]:
        body = request.get_json(silent=True)
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
            return jsonify({"error": "validation_error", "fields": fields}), 422
        order = {"id": len(orders) + 1, "item": item, "quantity": quantity}
        orders.append(order)
        return jsonify({"order": order}), 201

    @app.get("/api/orders")
    def list_orders() -> object:
        return jsonify({"orders": orders})

    @app.get("/api/private")
    def private() -> tuple[object, int] | object:
        if not request.headers.get("Authorization"):
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"status": "authorized"})

    @app.get("/api/boom")
    def boom() -> None:
        raise RuntimeError("deliberate fixture exception")

    @app.get("/api/session")
    def session() -> object:
        response = jsonify({"credentials": {"token": "hunter2"}, "session": "active"})
        response.set_cookie("sid", "hunter2", httponly=True)
        return response

    @app.errorhandler(404)
    def not_found(_error: object) -> tuple[object, int]:
        return jsonify({"error": "not_found"}), 404

    @app.errorhandler(500)
    def internal(_error: object) -> tuple[object, int]:
        return jsonify({"error": "internal"}), 500

    return app


app = create_app()
