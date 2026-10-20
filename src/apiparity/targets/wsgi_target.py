"""In-process WSGI target — runs an imported application (e.g. Flask) without a server.

httpx's :class:`WSGITransport` is a *synchronous* transport, so each request is
executed on a dedicated daemon thread; the corpus stays sequential, so only one
thread is ever active per target. The per-request timeout is enforced on the
event loop: a handler that never returns is abandoned at the deadline (its
thread cannot be interrupted — see ``docs/safety.md``).
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import httpx

from apiparity.models import ResponseSnapshot, TargetConfig
from apiparity.targets.base import (
    Target,
    _build_snapshot,
    _error_snapshot,
    _request_deadline,
    read_capped_sync,
)

__all__ = ["WsgiTarget"]


def _fix_path_info(app: Any) -> Any:
    """Wrap a WSGI app so ``PATH_INFO`` matches real-server semantics.

    httpx's ``WSGITransport`` passes the URL-*decoded* Unicode path as
    ``PATH_INFO``, but PEP 3333 requires PATH_INFO to be the raw request bytes
    represented as a latin-1 "bytes-as-str". Frameworks like Werkzeug encode
    PATH_INFO back to latin-1 and then decode it as UTF-8, so an already-decoded
    non-ASCII path gets mangled. Re-encoding as UTF-8 and presenting it as
    latin-1 restores the original bytes, making in-process WSGI behave like a
    real server (pure-ASCII paths are unaffected — the round-trip is identity).
    """

    def middleware(environ: dict[str, Any], start_response: Any) -> Any:
        path = environ.get("PATH_INFO", "")
        environ["PATH_INFO"] = path.encode("utf-8").decode("latin-1")
        return app(environ, start_response)

    return middleware


class WsgiTarget(Target):
    #: A WSGI handler runs on a thread asyncio cannot interrupt: once a
    #: request times out the target's state can no longer be trusted.
    supports_cancellation = False

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
        self._client: httpx.Client | None = None

    async def startup(self) -> None:
        self._client = httpx.Client(
            transport=httpx.WSGITransport(app=_fix_path_info(self._app)),
            follow_redirects=self.follow_redirects,
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await asyncio.to_thread(client.close)

    async def _send_and_capture(
        self,
        method: str,
        url: str,
        kwargs: dict[str, Any],
        started: float,
    ) -> ResponseSnapshot:
        if self._client is None:  # pragma: no cover — lifecycle misuse guard
            raise RuntimeError("target used before startup()")
        # WSGITransport cannot enforce network timeouts itself, so the
        # per-request timeout (case override > target config > run default) is
        # enforced here. The handler runs on a *daemon* thread that asyncio
        # does not own: at the deadline this coroutine stops waiting and the
        # thread is abandoned (a handler that never returns cannot block the
        # run or process exit; any response it eventually produces is dropped).
        # NOTE: asyncio.wait_for(asyncio.to_thread(...)) would NOT work here —
        # it waits for an un-cancellable executor future instead of firing.
        deadline = _request_deadline(kwargs)
        kwargs.pop("timeout", None)

        loop = asyncio.get_running_loop()
        finished = asyncio.Event()
        snapshot: ResponseSnapshot | None = None
        error: Exception | None = None

        def worker() -> None:
            nonlocal snapshot, error
            try:
                snapshot = self._send_sync(method, url, kwargs, started)
            except Exception as exc:
                error = exc
            finally:
                try:
                    loop.call_soon_threadsafe(finished.set)
                except RuntimeError:
                    pass  # event loop already closed; nobody is waiting anymore

        threading.Thread(
            target=worker, name=f"apiparity-wsgi-request-{self.side}", daemon=True
        ).start()
        if deadline is not None:
            try:
                await asyncio.wait_for(finished.wait(), timeout=deadline)
            except TimeoutError:
                return _error_snapshot(
                    "timeout",
                    f"in-process WSGI handler exceeded the {deadline:g}s request timeout "
                    "(the handler thread cannot be interrupted and may still be running)",
                    started,
                )
        else:
            await finished.wait()
        if error is not None:
            raise error
        if snapshot is None:  # pragma: no cover — worker exits with a result or error
            raise RuntimeError("WSGI worker thread finished without a result")
        return snapshot

    def _send_sync(
        self,
        method: str,
        url: str,
        kwargs: dict[str, Any],
        started: float,
    ) -> ResponseSnapshot:
        assert self._client is not None
        with self._client.stream(method, url, **kwargs) as response:
            body, truncated, kind = read_capped_sync(response, self.size_limit)
            return _build_snapshot(
                response, body, truncated=truncated, error_kind=kind, started=started
            )
