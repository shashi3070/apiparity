"""In-process WSGI target — runs an imported application (e.g. Flask) without a server.

httpx's :class:`WSGITransport` is a *synchronous* transport, so each request is
executed on a worker thread via :func:`asyncio.to_thread`; the corpus stays
sequential, so only one thread is ever active per target.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from apiparity.models import ResponseSnapshot, TargetConfig
from apiparity.targets.base import Target, _build_snapshot, read_capped_sync

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
        # Local transports cannot enforce network timeouts; the application itself
        # is trusted (documented in docs/safety.md).
        kwargs.pop("timeout", None)
        return await asyncio.to_thread(self._send_sync, method, url, kwargs, started)

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
