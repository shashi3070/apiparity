"""In-process ASGI target — runs an imported application (e.g. FastAPI).

httpx's :class:`ASGITransport` never runs the ASGI *lifespan* protocol, so this
target drives it explicitly on the same event loop:

* ``lifespan.startup`` is sent during :meth:`startup`;
* ``lifespan.startup.failed`` ⇒ :class:`TargetInitError` (fail closed, no fallback);
* applications that ignore lifespan or raise outside the protocol are treated as
  *lifespan-less* and run without startup hooks (documented in docs/architecture.md);
* ``lifespan.shutdown`` is always attempted during :meth:`shutdown`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from apiparity.exceptions import TargetInitError
from apiparity.models import ResponseSnapshot, TargetConfig
from apiparity.redaction import sanitize_exception_message
from apiparity.targets.base import Target, _build_snapshot, read_capped_async

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
        self._lifespan_timeout = lifespan_timeout
        self._client: httpx.AsyncClient | None = None
        self._lifespan_task: asyncio.Task[None] | None = None
        self._lifespan_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._lifespan_message: dict[str, Any] = {}
        self._lifespan_event: asyncio.Event | None = None

    # -- lifecycle ---------------------------------------------------------- #

    async def startup(self) -> None:
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
        message = await self._await_lifespan_message(phase="startup")

        if message is None:
            # App returned or raised outside the lifespan protocol: lifespan-less.
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
        self._lifespan_event = asyncio.Event()
        assert self._lifespan_queue is not None
        await self._lifespan_queue.put({"type": "lifespan.shutdown"})
        await self._await_lifespan_message(phase="shutdown")
        # Shutdown problems are recorded nowhere but never block cleanup.
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _await_lifespan_message(self, *, phase: str) -> dict[str, Any] | None:
        """Return the lifespan message, ``None`` for lifespan-less apps.

        Raises :class:`TargetInitError` when *startup* neither completes nor
        reports an unsupported protocol within the timeout.
        """
        task = self._lifespan_task
        event = self._lifespan_event
        assert task is not None and event is not None

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
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if phase == "startup":
            raise TargetInitError(
                self.side,
                f"application lifespan startup timed out after {self._lifespan_timeout:g}s",
                action="Check startup hooks for deadlocks or blocking calls.",
            )
        return None

    async def _reap_lifespan_task(self) -> None:
        task = self._lifespan_task
        self._lifespan_task = None
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

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
        async with self._client.stream(method, url, **kwargs) as response:
            body, truncated, kind = await read_capped_async(response, self.size_limit)
            return _build_snapshot(
                response, body, truncated=truncated, error_kind=kind, started=started
            )


def _swallow_task_exception(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    task.exception()
