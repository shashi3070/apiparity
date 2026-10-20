"""Target abstraction shared by HTTP, WSGI, and ASGI adapters.

Lifecycle contract (one policy for every target type):

1. construct with :func:`apiparity.targets.loader.resolve_target`
   (no I/O yet),
2. ``await target.startup()`` — may raise :class:`TargetInitError`,
3. ``await target.execute(case)`` for each case in corpus order,
4. ``await target.shutdown()`` — always runs, even after failures.

``execute`` never raises for transport problems: failures are encoded in the
returned :class:`ResponseSnapshot` so that baseline and candidate failures can be
recorded side by side.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

from apiparity.models import ErrorKind, FileUpload, RequestCase, ResponseSnapshot, TargetConfig
from apiparity.redaction import sanitize_exception_message

__all__ = ["IN_PROCESS_HOST", "Target"]

#: Hostname used for in-process targets (never resolved — transports never dial it).
IN_PROCESS_HOST = "http://apiparity.local"


class Target(ABC):
    """One implementation under test (baseline or candidate)."""

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
        self.side = side
        self.config = config
        self.size_limit = size_limit
        self.follow_redirects = follow_redirects
        self.corpus_dir = corpus_dir
        self.default_timeout = default_timeout

    @property
    def effective_timeout(self) -> float:
        """Case override > per-target config > effective run default."""
        return self.config.timeout_seconds or self.default_timeout

    # -- lifecycle ---------------------------------------------------------- #

    @abstractmethod
    async def startup(self) -> None:
        """Acquire resources. Must raise :class:`TargetInitError` on failure."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Release resources. Must swallow-and-log nothing; best-effort close only."""

    def describe(self) -> str:
        return f"{self.side}: {self.config.describe()}"

    # -- execution ---------------------------------------------------------- #

    async def execute(self, case: RequestCase) -> ResponseSnapshot:
        method, url, kwargs = self._prepare_request(case)
        started = time.perf_counter()
        try:
            return await self._send_and_capture(method, url, kwargs, started)
        except httpx.TimeoutException as exc:
            return _error_snapshot(
                "timeout",
                sanitize_exception_message(f"{type(exc).__name__}: {exc}"),
                started,
            )
        except httpx.HTTPError as exc:
            return _error_snapshot(
                "transport",
                sanitize_exception_message(f"{type(exc).__name__}: {exc}"),
                started,
            )
        except Exception as exc:
            return _error_snapshot(
                "transport",
                sanitize_exception_message(f"{type(exc).__name__}: {exc}"),
                started,
            )

    @abstractmethod
    async def _send_and_capture(
        self,
        method: str,
        url: str,
        kwargs: dict[str, Any],
        started: float,
    ) -> ResponseSnapshot:
        """Send one request through the concrete transport and capture a snapshot."""

    # -- request preparation ------------------------------------------------ #

    def _prepare_request(self, case: RequestCase) -> tuple[str, str, dict[str, Any]]:
        timeout = (
            case.timeout_seconds if case.timeout_seconds is not None else self.effective_timeout
        )
        kwargs: dict[str, Any] = {
            "headers": dict(case.headers),
            "follow_redirects": self.follow_redirects,
            "timeout": timeout,
        }
        pairs = case.query_pairs()
        if pairs:
            kwargs["params"] = pairs

        if case.json_body is not None:
            kwargs["json"] = case.json_body
        elif case.data is not None:
            kwargs["content"] = case.data.encode("utf-8")

        if case.files:
            kwargs["files"] = [self._file_tuple(upload) for upload in case.files]

        return case.method, self._url_for(case.path), kwargs

    def _url_for(self, path: str) -> str:
        base = (
            self.config.url.rstrip("/")
            if self.config.kind == "http" and self.config.url
            else IN_PROCESS_HOST
        )
        return base + path

    def _file_tuple(self, upload: FileUpload) -> tuple[str, tuple[str, bytes, str]]:
        file_path = (self.corpus_dir / upload.path).resolve()
        data = file_path.read_bytes()
        content_type = upload.content_type or "application/octet-stream"
        return (upload.field, (file_path.name, data, content_type))


# --------------------------------------------------------------------------- #
# Shared capture helpers
# --------------------------------------------------------------------------- #


def _error_snapshot(kind: ErrorKind, message: str, started: float) -> ResponseSnapshot:
    return ResponseSnapshot(
        status_code=None,
        headers=(),
        content_type=None,
        body_bytes=None,
        body_truncated=False,
        body_size=0,
        duration_ms=_ms_since(started),
        transport_error=message,
        error_kind=kind,
    )


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _group_headers(response: httpx.Response) -> tuple[tuple[str, tuple[str, ...]], ...]:
    grouped: dict[str, list[str]] = {}
    for name, value in response.headers.multi_items():
        lowered = name.lower()
        grouped.setdefault(lowered, []).append(value)
    return tuple((name, tuple(values)) for name, values in grouped.items())


def _build_snapshot(
    response: httpx.Response,
    body: bytes,
    *,
    truncated: bool,
    error_kind: ErrorKind,
    started: float,
) -> ResponseSnapshot:
    content_length = response.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and not truncated:
        body_size = int(content_length)
    else:
        body_size = len(body)
    return ResponseSnapshot(
        status_code=response.status_code,
        headers=_group_headers(response),
        content_type=response.headers.get("content-type"),
        body_bytes=body,
        body_truncated=truncated,
        body_size=body_size,
        duration_ms=_ms_since(started),
        transport_error=None,
        error_kind=error_kind,
    )


async def read_capped_async(response: httpx.Response, limit: int) -> tuple[bytes, bool, ErrorKind]:
    """Read at most ``limit`` bytes; never claim completeness when capped."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        if total + len(chunk) > limit:
            remaining = limit - total
            if remaining > 0:
                chunks.append(chunk[:remaining])
            return b"".join(chunks), True, "too_large"
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), False, "none"


def read_capped_sync(response: httpx.Response, limit: int) -> tuple[bytes, bool, ErrorKind]:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        if total + len(chunk) > limit:
            remaining = limit - total
            if remaining > 0:
                chunks.append(chunk[:remaining])
            return b"".join(chunks), True, "too_large"
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), False, "none"


async def shutdown_sync_client(client: httpx.Client) -> None:
    await asyncio.to_thread(client.close)
