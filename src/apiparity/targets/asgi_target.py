"""In-process ASGI target — runs an imported application (e.g. FastAPI).

httpx's :class:`ASGITransport` never runs the ASGI *lifespan* protocol, so this
target drives it explicitly on the same event loop, governed by
``TargetConfig.lifespan``:

* ``auto`` (default) — a ``lifespan.startup.complete`` message starts the app;
  an application whose lifespan task exits cleanly without ever speaking the
  protocol is treated as *lifespan-less* and runs without startup hooks; a
  ``lifespan.startup.failed`` message **or an uncaught exception in the
  lifespan task** aborts with :class:`TargetInitError` — startup errors are
  never concealed;
* ``required`` — like ``auto``, but an application that never sends a startup
  message (clean exit included) aborts with :class:`TargetInitError`; use it
  to guarantee startup hooks actually ran;
* ``off`` — the lifespan protocol is not run at all (explicit escape hatch for
  applications that cannot handle lifespan scope);

``lifespan.shutdown`` is always attempted during :meth:`shutdown` (unless the
mode is ``off`` or the app was lifespan-less). Shutdown problems are recorded
nowhere but never block cleanup: every wait around the lifespan task is
bounded.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from apiparity.exceptions import TargetInitError
from apiparity.models import LifespanMode, ResponseSnapshot, TargetConfig
from apiparity.redaction import sanitize_exception_message
from apiparity.targets.base import (
    Target,
    _build_snapshot,
    _error_snapshot,
    _request_deadline,
    read_capped_async,
)

__all__ = ["LIFESPAN_TIMEOUT_SECONDS", "AsgiTarget"]

LIFESPAN_TIMEOUT_SECONDS = 10.0


class AsgiTarget(Target):
    def __init__(
        self,
        side: str,
        config: TargetConfig,
        *,
        app: Any,
        size_limit: int,
        follow_redirects: bool,
        corpus_dir: Path,
        default_timeout: float = 10.0,
        lifespan_timeout: float = LIFESPAN_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            side,
            config,
            size_limit=size_limit,
            follow_redirects=follow_redirects,
            corpus_dir=corpus_dir,
            default_timeout=default_timeout,
        )
        self._app = app
        self._lifespan_mode: LifespanMode = config.lifespan
        self._lifespan_timeout = lifespan_timeout
        self._client: httpx.AsyncClient | None = None
        self._lifespan_task: asyncio.Task[None] | None = None
        self._lifespan_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._lifespan_message: dict[str, Any] = {}
        self._lifespan_event: asyncio.Event | None = None

    # -- lifecycle ---------------------------------------------------------- #

    async def startup(self) -> None:
        if self._lifespan_mode != "off":
            await self._start_lifespan()
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app, raise_app_exceptions=False),
            follow_redirects=self.follow_redirects,
            timeout=httpx.Timeout(self.effective_timeout),
        )

    async def shutdown(self) -> None:
        try:
            if self._client is not None:
                client, self._client = self._client, None
                await client.aclose()
        finally:
            await self._stop_lifespan()

    # -- lifespan driver ---------------------------------------------------- #

    async def _start_lifespan(self) -> None:
        self._lifespan_queue = asyncio.Queue()
        self._lifespan_event = asyncio.Event()
        self._lifespan_message = {}

        async def receive() -> dict[str, Any]:
            assert self._lifespan_queue is not None
            return await self._lifespan_queue.get()

        async def send(message: dict[str, Any]) -> None:
            self._lifespan_message = message
            assert self._lifespan_event is not None
            self._lifespan_event.set()

        scope = {
            "type": "lifespan",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "state": {},
        }
        self._lifespan_task = asyncio.create_task(
            self._app(scope, receive, send), name=f"apiparity-lifespan-{self.side}"
        )
        await self._lifespan_queue.put({"type": "lifespan.startup"})
        task = self._lifespan_task
        event = self._lifespan_event
        assert task is not None and event is not None
        message = await self._await_lifespan_message(task, event, phase="startup")

        if message is None:
            # No startup message was ever sent: distinguish "the app does not
            # speak lifespan" from "the app died while starting up".
            exc = self._lifespan_task_exception()
            if exc is not None:
                detail = sanitize_exception_message(f"{type(exc).__name__}: {exc}")
                await self._reap_lifespan_task()
                raise TargetInitError(
                    self.side,
                    f"application lifespan task raised during startup: {detail}",
                    action=(
                        "Fix the application's startup code, or set 'lifespan: off' "
                        "for this target if the application cannot handle lifespan scope."
                    ),
                )
            if self._lifespan_mode == "required":
                await self._reap_lifespan_task()
                raise TargetInitError(
                    self.side,
                    "application did not send a lifespan startup message "
                    "(lifespan mode 'required')",
                    action=(
                        "Implement the ASGI lifespan protocol in the application, "
                        "or set 'lifespan: auto' for this target."
                    ),
                )
            # Lifespan-less (auto): the app returned cleanly without speaking
            # the protocol — run it without startup hooks (documented behavior).
            return
        if message.get("type") == "lifespan.startup.failed":
            detail = sanitize_exception_message(str(message.get("message", "unknown failure")))
            await self._reap_lifespan_task()
            raise TargetInitError(
                self.side,
                f"application startup failed: {detail}",
                action="Fix the application's lifespan/startup hook, then rerun.",
            )
        if message.get("type") != "lifespan.startup.complete":
            detail = sanitize_exception_message(str(message.get("type")))
            await self._reap_lifespan_task()
            raise TargetInitError(
                self.side,
                f"unexpected ASGI lifespan message during startup: {detail}",
                action="Verify the application implements the ASGI lifespan protocol.",
            )

    async def _stop_lifespan(self) -> None:
        task = self._lifespan_task
        self._lifespan_task = None
        if task is None:
            return
        if task.done():
            _swallow_task_exception(task)
            return
        self._lifespan_message = {}
        event = self._lifespan_event = asyncio.Event()
        assert self._lifespan_queue is not None
        await self._lifespan_queue.put({"type": "lifespan.shutdown"})
        await self._await_lifespan_message(task, event, phase="shutdown")
        # Shutdown problems are recorded nowhere but never block cleanup.
        await self._finish_task(task)

    async def _await_lifespan_message(
        self,
        task: asyncio.Task[None],
        event: asyncio.Event,
        *,
        phase: str,
    ) -> dict[str, Any] | None:
        """Return the lifespan message, ``None`` for lifespan-less apps.

        Raises :class:`TargetInitError` when *startup* neither completes nor
        reports an unsupported protocol within the timeout.
        """
        waiter = asyncio.create_task(event.wait())
        try:
            done, _pending = await asyncio.wait(
                {waiter, task},
                timeout=self._lifespan_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not waiter.done():
                waiter.cancel()

        if event.is_set():
            _ = done
            if task.done():
                _swallow_task_exception(task)
            return dict(self._lifespan_message)

        # No message: either the app finished (lifespan-less) or timed out.
        if task.done():
            _swallow_task_exception(task)
            return None

        # Startup hung: fail closed. Shutdown: cancel and move on.
        if phase == "startup":
            await self._finish_task(task)
            raise TargetInitError(
                self.side,
                f"application lifespan startup timed out after {self._lifespan_timeout:g}s",
                action="Check startup hooks for deadlocks or blocking calls.",
            )
        await self._finish_task(task)
        return None

    def _lifespan_task_exception(self) -> BaseException | None:
        """The lifespan task's exception, or ``None`` when it did not raise."""
        task = self._lifespan_task
        if task is None or not task.done() or task.cancelled():
            return None
        return task.exception()

    async def _reap_lifespan_task(self) -> None:
        """Dispose of the lifespan task after an abort: cancel, wait, bounded."""
        task = self._lifespan_task
        self._lifespan_task = None
        if task is None:
            return
        await self._finish_task(task)

    async def _finish_task(self, task: asyncio.Task[None]) -> None:
        """Cancel (if needed) and wait for completion, bounded so cleanup never hangs."""
        if not task.done():
            task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=self._lifespan_timeout)
        if task in done:
            _swallow_task_exception(task)

    # -- request execution -------------------------------------------------- #

    async def _send_and_capture(
        self,
        method: str,
        url: str,
        kwargs: dict[str, Any],
        started: float,
    ) -> ResponseSnapshot:
        if self._client is None:  # pragma: no cover — lifecycle misuse guard
            raise RuntimeError("target used before startup()")
        # ASGITransport cannot enforce network timeouts itself, so the
        # per-request timeout is enforced here with an asyncio deadline; the
        # stream context is cancelled cleanly when it fires.
        deadline = _request_deadline(kwargs)
        if deadline is None:
            return await self._stream(method, url, kwargs, started)
        try:
            return await asyncio.wait_for(
                self._stream(method, url, kwargs, started), timeout=deadline
            )
        except TimeoutError:
            return _error_snapshot(
                "timeout",
                f"ASGI request exceeded the {deadline:g}s request timeout",
                started,
            )

    async def _stream(
        self,
        method: str,
        url: str,
        kwargs: dict[str, Any],
        started: float,
    ) -> ResponseSnapshot:
        assert self._client is not None
        async with self._client.stream(method, url, **kwargs) as response:
            body, truncated, kind = await read_capped_async(response, self.size_limit)
            return _build_snapshot(
                response, body, truncated=truncated, error_kind=kind, started=started
            )


def _swallow_task_exception(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    task.exception()
