"""In-process request deadlines: a handler that never completes must produce
a timeout snapshot instead of hanging the run (audit finding #1).

Covers both adapters: WSGI (detached daemon thread) and ASGI (cancellable
stream), at the ``execute()`` level and through a full ``run_comparison``.

Quarantine semantics covered here:

* a WSGI timeout leaves an uninterruptible handler thread behind, so the
  target is quarantined and later cases are never sent to it;
* an ASGI handler that honours cancellation (or a clean timeout) does *not*
  quarantine — the next case still runs;
* an ASGI handler that resists cancellation, or blocks the event loop with
  synchronous code, quarantines the target.
"""

from __future__ import annotations

import asyncio
import time
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


async def _path_asgi(scope: Any, receive: Any, send: Any) -> None:
    """``/hang`` waits forever; anything else answers 200 immediately."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope.get("path") == "/hang":
        await asyncio.Event().wait()
    await _respond_json(send)


async def _resistant_asgi(scope: Any, receive: Any, send: Any) -> None:
    """/hang' swallows cancellation once and keeps working past the grace."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope.get("path") == "/hang":
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.4)  # ignores the cancellation
    await _respond_json(send)


async def _blocking_asgi(scope: Any, receive: Any, send: Any) -> None:
    """/hang' blocks the event loop with synchronous sleep."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope.get("path") == "/hang":
        time.sleep(0.4)  # deliberately blocks every coroutine on the loop
    await _respond_json(send)


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


def _asgi_target_for(
    app: Any, *, default_timeout: float = 0.2, cancel_grace: float = 1.0
) -> AsgiTarget:
    return AsgiTarget(
        "candidate",
        TargetConfig(kind="asgi", app="test:app"),
        app=app,
        size_limit=1024,
        follow_redirects=False,
        corpus_dir=Path(__file__).parent,
        default_timeout=default_timeout,
        cancel_grace=cancel_grace,
    )


def _recording_wsgi_target() -> WsgiTarget:
    return WsgiTarget(
        "baseline",
        TargetConfig(kind="wsgi", app="hang_apps:recording_wsgi_app"),
        app=hang_apps.recording_wsgi_app,
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
        # An uninterruptible thread is still inside the handler: quarantine.
        assert target.quarantined is True
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
        # The handler was cancelled cleanly: the target stays trustworthy.
        assert target.quarantined is False
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


# --------------------------------------------------------------------------- #
# Quarantine: WSGI (uninterruptible handler thread)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wsgi_quarantine_refuses_later_requests() -> None:
    """After an uninterruptible timeout the target is quarantined: the next
    case is refused locally and the application is never touched again."""
    hang_apps.RELEASE.clear()
    hang_apps.SERVED.clear()
    target = _recording_wsgi_target()
    await target.startup()
    try:
        first = await target.execute(RequestCase(id="hang", method="GET", path="/hang"))
        assert first.error_kind == "timeout"
        assert target.quarantined is True

        second = await target.execute(RequestCase(id="after", method="GET", path="/after"))
        assert second.error_kind == "transport"
        assert second.transport_error is not None
        assert "request not sent" in second.transport_error
        assert "quarantined" in second.transport_error
        # The app saw only the first case: the refused one never left the client.
        assert hang_apps.SERVED == ["/hang"]
    finally:
        hang_apps.RELEASE.set()
        await target.shutdown()


@pytest.mark.asyncio
async def test_compare_later_case_not_sent_after_wsgi_quarantine(tmp_path: Path) -> None:
    """Full run: case 1 times out (quarantine), case 2 baseline never dials."""
    hang_apps.RELEASE.clear()
    hang_apps.SERVED.clear()
    corpus = tmp_path / "quarantine.yaml"
    corpus.write_text(
        "version: 1\n"
        "defaults: {timeout_seconds: 0.3}\n"
        "cases:\n"
        "  - {id: hang, method: GET, path: /hang}\n"
        "  - {id: after, method: GET, path: /after}\n",
        encoding="utf-8",
    )
    config = AppConfig(
        old_target=TargetConfig(kind="wsgi", app="hang_apps:recording_wsgi_app"),
        new_target=TargetConfig(kind="asgi", app="migrated_fastapi.app:app"),
    )
    try:
        result = await run_comparison(corpus=load_corpus(corpus), config=config)
    finally:
        hang_apps.RELEASE.set()

    assert result.summary.exit_code == 1
    first, second = result.cases
    assert first.baseline is not None
    assert first.baseline.error_kind == "timeout"
    assert second.baseline is not None
    assert second.baseline.error_kind == "transport"
    assert second.baseline.transport_error is not None
    assert "request not sent" in second.baseline.transport_error
    assert "quarantined" in second.baseline.transport_error
    # The candidate (separate process boundary: in-process ASGI) still ran.
    assert second.candidate is not None
    assert second.candidate.status_code is not None
    assert "/after" not in hang_apps.SERVED


# --------------------------------------------------------------------------- #
# Quarantine: ASGI (cancellation resistance / blocked event loop)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_asgi_clean_timeout_allows_next_case() -> None:
    """/hang times out and cancels cleanly → /after still runs."""
    target = _asgi_target_for(_path_asgi)
    await target.startup()
    try:
        first = await target.execute(RequestCase(id="hang", method="GET", path="/hang"))
        assert first.error_kind == "timeout"
        assert first.transport_error is not None
        assert "handler cancelled" in first.transport_error
        assert target.quarantined is False

        second = await target.execute(RequestCase(id="after", method="GET", path="/after"))
        assert second.status_code == 200
        assert second.error_kind == "none"
        assert target.quarantined is False
    finally:
        await target.shutdown()


@pytest.mark.asyncio
async def test_asgi_cancellation_resistant_handler_quarantines() -> None:
    """/hang ignores cancellation past the grace period → quarantine."""
    target = _asgi_target_for(_resistant_asgi, cancel_grace=0.05)
    await target.startup()
    try:
        first = await target.execute(RequestCase(id="hang", method="GET", path="/hang"))
        assert first.error_kind == "timeout"
        assert first.transport_error is not None
        assert "could not be cancelled" in first.transport_error
        assert target.quarantined is True

        second = await target.execute(RequestCase(id="after", method="GET", path="/after"))
        assert second.error_kind == "transport"
        assert second.transport_error is not None
        assert "request not sent" in second.transport_error
        # Let the abandoned handler finish its post-cancel sleep so no task
        # outlives the test's event loop.
        await asyncio.sleep(0.5)
    finally:
        await target.shutdown()


@pytest.mark.asyncio
async def test_asgi_event_loop_blocked_quarantines() -> None:
    """/hang' blocks the loop with time.sleep() → quarantine."""
    target = _asgi_target_for(_blocking_asgi, default_timeout=0.05)
    await target.startup()
    try:
        first = await target.execute(RequestCase(id="hang", method="GET", path="/hang"))
        assert first.error_kind == "timeout"
        assert first.transport_error is not None
        assert "event loop blocked" in first.transport_error
        assert target.quarantined is True

        second = await target.execute(RequestCase(id="after", method="GET", path="/after"))
        assert second.error_kind == "transport"
        assert second.transport_error is not None
        assert "request not sent" in second.transport_error
    finally:
        await target.shutdown()
