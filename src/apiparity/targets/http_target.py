"""HTTP endpoint target — compares separately running implementations over the network.

Uses one :class:`httpx.AsyncClient` per run: TLS verification on by default, no
redirect following unless configured, no retries, explicit timeouts, connections
closed in :meth:`shutdown`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from apiparity.models import ResponseSnapshot, TargetConfig
from apiparity.targets.base import Target, _build_snapshot, read_capped_async

__all__ = ["HttpTarget"]


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
    ) -> None:
        super().__init__(
            side,
            config,
            size_limit=size_limit,
            follow_redirects=follow_redirects,
            corpus_dir=corpus_dir,
            default_timeout=default_timeout,
        )
        self._client: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        timeout = httpx.Timeout(self.effective_timeout)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            verify=self.config.verify_tls,
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
