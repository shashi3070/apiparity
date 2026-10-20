"""In-process request deadlines: a handler that never completes must produce
a timeout snapshot instead of hanging the run (audit finding #1).

Covers both adapters: WSGI (detached daemon thread) and ASGI (cancellable
stream), at the ``execute()`` level and through a full ``run_comparison``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import hang_apps
import pytest

from apiparity.corpus import load_corpus
from apiparity.models import AppConfig, RequestCase, TargetConfig
from apiparity.runner import run_comparison
from apiparity.targets.asgi_target import AsgiTarget
from apiparity.targets.wsgi_target import WsgiTarget


def _case() -> RequestCase:
    return RequestCase(id="hang", method="GET", path="/hang")


async def _respond_json(send: Any) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b'{"ok": true}'})


def _hang_wsgi_app(environ: Any, start_response: Any) -> Any:
    # Safety valve: even if the test fails to release, the thread dies in 10s.
    if not hang_apps.RELEASE.wait(timeout=10.0):
        start_response("500 Server Error", [("content-type", "text/plain")])
        return [b"handler never released"]
    start_response("200 OK", [("content-type", "application/json")])
    return [b'{"ok": true}']


async def _hang_asgi(scope: Any, receive: Any, send: Any) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    await asyncio.Event().wait()  # never completes; cancelled at the deadline


def _wsgi_target() -> WsgiTarget:
    return WsgiTarget(
        "candidate",
        TargetConfig(kind="wsgi", app="test:hang"),
        app=_hang_wsgi_app,
        size_limit=1024,
        follow_redirects=False,
        corpus_dir=Path(__file__).parent,
        default_timeout=0.2,
    )


def _asgi_target() -> AsgiTarget:
    return AsgiTarget(
        "candidate",
        TargetConfig(kind="asgi", app="test:hang"),
        app=_hang_asgi,
        size_limit=1024,
        follow_redirects=False,
        corpus_dir=Path(__file__).parent,
        default_timeout=0.2,
    )


@pytest.mark.asyncio
async def test_wsgi_handler_that_never_completes_times_out() -> None:
    hang_apps.RELEASE.clear()
    target = _wsgi_target()
    await target.startup()
    try:
        snapshot = await target.execute(_case())
        assert snapshot.error_kind == "timeout"
        assert snapshot.status_code is None
        assert snapshot.transport_error is not None
        assert "0.2s" in snapshot.transport_error
    finally:
        hang_apps.RELEASE.set()  # let the abandoned handler thread finish
        await target.shutdown()


@pytest.mark.asyncio
async def test_asgi_handler_that_never_completes_times_out() -> None:
    target = _asgi_target()
    await target.startup()
    try:
        snapshot = await target.execute(_case())
        assert snapshot.error_kind == "timeout"
        assert snapshot.status_code is None
        assert snapshot.transport_error is not None
        assert "0.2s" in snapshot.transport_error
    finally:
        await target.shutdown()


@pytest.mark.asyncio
async def test_wsgi_case_timeout_override_wins(tmp_path: Path) -> None:
    """A per-case timeout shorter than the target default still bounds the handler."""
    hang_apps.RELEASE.clear()
    target = WsgiTarget(
        "candidate",
        TargetConfig(kind="wsgi", app="test:hang", timeout_seconds=30.0),
        app=_hang_wsgi_app,
        size_limit=1024,
        follow_redirects=False,
        corpus_dir=Path(__file__).parent,
        default_timeout=30.0,
    )
    await target.startup()
    try:
        case = RequestCase(id="hang", method="GET", path="/hang", timeout_seconds=0.2)
        snapshot = await target.execute(case)
        assert snapshot.error_kind == "timeout"
        assert snapshot.transport_error is not None
        assert "0.2s" in snapshot.transport_error
    finally:
        hang_apps.RELEASE.set()
        await target.shutdown()


@pytest.mark.asyncio
async def test_compare_with_hanging_wsgi_baseline_exits_1(tmp_path: Path) -> None:
    """Full run: baseline handler never completes → failed case → exit code 1."""
    hang_apps.RELEASE.clear()
    corpus = tmp_path / "hang.yaml"
    corpus.write_text(
        "version: 1\n"
        "defaults: {timeout_seconds: 0.3}\n"
        "cases: [{id: hang, method: GET, path: /hang}]\n",
        encoding="utf-8",
    )
    config = AppConfig(
        old_target=TargetConfig(kind="wsgi", app="hang_apps:hang_wsgi_app"),
        new_target=TargetConfig(kind="asgi", app="migrated_fastapi.app:app"),
    )
    try:
        result = await run_comparison(corpus=load_corpus(corpus), config=config)
    finally:
        hang_apps.RELEASE.set()

    assert result.summary.exit_code == 1
    case = result.cases[0]
    assert case.status in ("failed", "mismatch")
    assert case.baseline is not None
    assert case.baseline.error_kind == "timeout"
    assert case.baseline.transport_error is not None
