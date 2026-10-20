"""HTTP endpoint target — compares separately running implementations over the network.

Uses one :class:`httpx.AsyncClient` per run: TLS verification on by default, no
redirect following unless configured, no retries, explicit timeouts, connections
closed in :meth:`shutdown`.

Every request the client makes — including each redirect hop — passes through
:class:`RemoteGuardedTransport`, so a redirect cannot escape the loopback
policy: without ``allow_remote_targets``, a hop to a non-loopback host is
refused before any bytes leave the process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from apiparity.models import ResponseSnapshot, TargetConfig
from apiparity.redaction import redact_url
from apiparity.safety import is_loopback_url
from apiparity.targets.base import Target, _build_snapshot, read_capped_async

__all__ = ["HttpTarget", "RemoteGuardedTransport", "UnsafeRedirectError"]


class UnsafeRedirectError(httpx.TransportError):
    """A request would leave loopback without ``allow_remote_targets``."""


class RemoteGuardedTransport(httpx.AsyncBaseTransport):
    """Refuse non-loopback destinations when remote access was not granted.

    Wraps the real transport and sees every request the client issues,
    including redirect hops followed on the client's behalf.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, *, allow_remote: bool) -> None:
        self._inner = inner
        self._allow_remote = allow_remote

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._allow_remote and not is_loopback_url(str(request.url)):
            raise UnsafeRedirectError(
                f"request to {redact_url(str(request.url))} refused: host is outside "
                "loopback and allow_remote_targets is disabled (redirects are checked "
                "hop by hop)"
            )
        return await self._inner.handle_async_request(request)


class HttpTarget(Target):
    def __init__(
        self,
        side: str,
        config: TargetConfig,
        *,
        size_limit: int,
        follow_redirects: bool,
        corpus_dir: Path,
        default_timeout: float = 10.0,
        allow_remote: bool = False,
    ) -> None:
        super().__init__(
            side,
            config,
            size_limit=size_limit,
            follow_redirects=follow_redirects,
            corpus_dir=corpus_dir,
            default_timeout=default_timeout,
        )
        self.allow_remote = allow_remote
        self._client: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        timeout = httpx.Timeout(self.effective_timeout)
        transport = RemoteGuardedTransport(
            httpx.AsyncHTTPTransport(verify=self.config.verify_tls, trust_env=False),
            allow_remote=self.allow_remote,
        )
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            follow_redirects=self.follow_redirects,
            # Never honor environment/system proxy settings: requests must go
            # exactly where the user pointed them, and proxy variables on the
            # host must not silently redirect or leak comparison traffic.
            trust_env=False,
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

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
