"""ASGI lifespan policy matrix — ``TargetConfig.lifespan``: auto | required | off.

Covers audit finding #2: a lifespan task that dies without speaking the
protocol must not be concealed as an app that "does not support lifespan", and
``required`` must abort when no startup message arrives. Also covers the
startup-timeout and shutdown-error cleanup paths.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from apiparity.exceptions import TargetInitError
from apiparity.models import RequestCase, TargetConfig
from apiparity.targets.asgi_target import AsgiTarget


async def _respond_json(send: Any) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b'{"ok": true}'})


async def _proper_lifespan_app(scope: Any, receive: Any, send: Any) -> None:
    """Speaks the lifespan protocol correctly and serves HTTP requests."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    await _respond_json(send)


async def _lifespan_less_app(scope: Any, receive: Any, send: Any) -> None:
    """Returns cleanly on lifespan scope without ever sending a message."""
    if scope["type"] == "lifespan":
        return
    await _respond_json(send)


async def _raising_lifespan_app(scope: Any, receive: Any, send: Any) -> None:
    """Dies with an exception on lifespan scope (the audit's concealment case)."""
    if scope["type"] == "lifespan":
        raise RuntimeError("boom during startup")
    await _respond_json(send)


async def _hanging_lifespan_app(scope: Any, receive: Any, send: Any) -> None:
    """Never answers the lifespan protocol (startup would hang)."""
    if scope["type"] == "lifespan":
        await asyncio.Event().wait()
    await _respond_json(send)


async def _shutdown_raising_app(scope: Any, receive: Any, send: Any) -> None:
    """Starts fine but raises while shutting down."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                raise RuntimeError("boom at shutdown")
    await _respond_json(send)


def _target(
    app: Any,
    lifespan: str,
    *,
    lifespan_timeout: float = 1.0,
) -> AsgiTarget:
    return AsgiTarget(
        "candidate",
        TargetConfig(kind="asgi", app="test:lifespan", lifespan=lifespan),
        app=app,
        size_limit=1024,
        follow_redirects=False,
        corpus_dir=Path(__file__).parent,
        default_timeout=2.0,
        lifespan_timeout=lifespan_timeout,
    )


async def _assert_serves(target: AsgiTarget) -> None:
    snapshot = await target.execute(RequestCase(id="x", method="GET", path="/x"))
    assert snapshot.status_code == 200


# --------------------------------------------------------------------------- #
# auto (default)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_auto_runs_proper_lifespan() -> None:
    target = _target(_proper_lifespan_app, "auto")
    await target.startup()
    try:
        await _assert_serves(target)
    finally:
        await target.shutdown()


@pytest.mark.asyncio
async def test_auto_treats_clean_exit_without_message_as_lifespan_less() -> None:
    target = _target(_lifespan_less_app, "auto")
    await target.startup()
    try:
        await _assert_serves(target)
    finally:
        await target.shutdown()


@pytest.mark.asyncio
async def test_auto_surfaces_lifespan_exception_instead_of_concealing() -> None:
    target = _target(_raising_lifespan_app, "auto")
    with pytest.raises(
        TargetInitError, match="raised during startup: RuntimeError: boom during startup"
    ):
        await target.startup()
    await target.shutdown()  # cleanup after a failed startup stays safe


# --------------------------------------------------------------------------- #
# required
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_required_accepts_proper_lifespan() -> None:
    target = _target(_proper_lifespan_app, "required")
    await target.startup()
    try:
        await _assert_serves(target)
    finally:
        await target.shutdown()


@pytest.mark.asyncio
async def test_required_rejects_missing_startup_message() -> None:
    target = _target(_lifespan_less_app, "required")
    with pytest.raises(TargetInitError, match="did not send a lifespan startup message"):
        await target.startup()
    await target.shutdown()


@pytest.mark.asyncio
async def test_required_also_surfaces_lifespan_exception() -> None:
    target = _target(_raising_lifespan_app, "required")
    with pytest.raises(TargetInitError, match="boom during startup"):
        await target.startup()
    await target.shutdown()


# --------------------------------------------------------------------------- #
# off
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_off_skips_lifespan_entirely() -> None:
    """An app that would hang the lifespan protocol runs fine with lifespan off."""
    target = _target(_hanging_lifespan_app, "off")
    await target.startup()
    try:
        await _assert_serves(target)
    finally:
        await target.shutdown()


# --------------------------------------------------------------------------- #
# timeout and shutdown paths
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_startup_timeout_fails_closed() -> None:
    target = _target(_hanging_lifespan_app, "auto", lifespan_timeout=0.15)
    with pytest.raises(TargetInitError, match=r"timed out after 0\.15s"):
        await target.startup()
    await target.shutdown()


@pytest.mark.asyncio
async def test_shutdown_error_never_blocks_cleanup() -> None:
    target = _target(_shutdown_raising_app, "auto")
    await target.startup()
    await target.shutdown()  # must not raise
    assert target._lifespan_task is None
