"""Integration tests: full runs of the comparison engine against the fixture apps.

These exercise the real WSGI (Flask) and ASGI (FastAPI) targets end-to-end —
importing the fixture apps, driving them through the runner, and asserting on
run summaries, difference pointers, and exit codes. A separate suite spins up
real HTTP servers (Werkzeug + uvicorn) on loopback ephemeral ports.

Run with: ``python -m pytest tests/integration -q``
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from werkzeug.serving import make_server

from apiparity.corpus import load_corpus
from apiparity.exceptions import CorpusError, SafetyError, TargetInitError
from apiparity.models import AppConfig, SafetyConfig, TargetConfig
from apiparity.runner import run_comparison
from apiparity.targets.asgi_target import AsgiTarget

CORPORA_DIR = Path(__file__).parent.parent / "fixtures" / "corpora"


def _wsgi_asgi_config() -> AppConfig:
    return AppConfig(
        old_target=TargetConfig(kind="wsgi", app="legacy_flask.app:app"),
        new_target=TargetConfig(kind="asgi", app="migrated_fastapi.app:app"),
    )


@pytest.mark.asyncio
async def test_equivalent_apps_all_match() -> None:
    corpus = load_corpus(CORPORA_DIR / "equivalent.yaml")
    result = await run_comparison(corpus=corpus, config=_wsgi_asgi_config())
    assert result.summary.exit_code == 0
    assert result.summary.matched == 9
    assert result.summary.mismatched == 0
    assert result.summary.failed == 0
    assert all(case.status == "match" for case in result.cases)


@pytest.mark.asyncio
async def test_mismatch_variant_detects_exact_divergences() -> None:
    corpus = load_corpus(CORPORA_DIR / "equivalent.yaml")
    config = AppConfig(
        old_target=TargetConfig(kind="wsgi", app="legacy_flask.app:app"),
        new_target=TargetConfig(kind="asgi", app="migrated_fastapi.app_mismatch:app"),
    )
    result = await run_comparison(corpus=corpus, config=config)
    assert result.summary.exit_code == 1

    by_id = {case.case_id: case for case in result.cases}

    customer = by_id["customer"]
    assert customer.status == "mismatch"
    status_diffs = [d for d in customer.differences if d.path == "/customer/status"]
    assert len(status_diffs) == 1
    assert status_diffs[0].category == "json_value"
    assert status_diffs[0].baseline_value == "active"
    assert status_diffs[0].candidate_value == "inactive"

    private = by_id["auth-failure"]
    assert private.status == "mismatch"
    assert private.baseline is not None and private.candidate is not None
    assert private.baseline.status_code == 401
    assert private.candidate.status_code == 200

    for case_id in (
        "health",
        "items-default",
        "items-filtered",
        "search-repeated-tags",
        "path-unicode",
        "not-found",
        "server-exception",
    ):
        assert by_id[case_id].status == "match", case_id


@pytest.mark.asyncio
async def test_mutating_corpus_with_opt_in() -> None:
    corpus = load_corpus(CORPORA_DIR / "mutating.yaml")
    config = AppConfig(
        old_target=TargetConfig(kind="wsgi", app="legacy_flask.app:app"),
        new_target=TargetConfig(kind="asgi", app="migrated_fastapi.app:app"),
        safety=SafetyConfig(allow_mutating_requests=True),
    )
    result = await run_comparison(corpus=corpus, config=config)
    assert result.summary.exit_code == 0
    assert all(case.status == "match" for case in result.cases)


@pytest.mark.asyncio
async def test_mutating_corpus_refused_without_opt_in() -> None:
    corpus = load_corpus(CORPORA_DIR / "mutating.yaml")
    with pytest.raises(SafetyError, match="mutating"):
        await run_comparison(corpus=corpus, config=_wsgi_asgi_config())


@pytest.mark.asyncio
async def test_factory_target_initialization() -> None:
    corpus = load_corpus(CORPORA_DIR / "equivalent.yaml")
    config = AppConfig(
        old_target=TargetConfig(kind="wsgi", app="legacy_flask.app:create_app", factory=True),
        new_target=TargetConfig(kind="asgi", app="migrated_fastapi.app:create_app", factory=True),
    )
    result = await run_comparison(corpus=corpus, config=config)
    assert result.summary.exit_code == 0
    assert all(case.status == "match" for case in result.cases)


# --------------------------------------------------------------------------- #
# Real HTTP servers (Werkzeug + uvicorn on loopback ephemeral ports)
# --------------------------------------------------------------------------- #


@pytest.fixture
def flask_http_url() -> Iterator[str]:
    from legacy_flask.app import create_app

    server = make_server("127.0.0.1", 0, create_app(), threaded=True)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=10)


@pytest.fixture
def fastapi_http_url() -> Iterator[str]:
    from migrated_fastapi.app import create_app

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover — startup hang guard
            raise RuntimeError("uvicorn did not start within 10s")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.asyncio
async def test_http_http_real_local_servers(flask_http_url: str, fastapi_http_url: str) -> None:
    corpus = load_corpus(CORPORA_DIR / "equivalent.yaml")
    config = AppConfig(
        old_target=TargetConfig(kind="http", url=flask_http_url),
        new_target=TargetConfig(kind="http", url=fastapi_http_url),
    )
    result = await run_comparison(corpus=corpus, config=config)
    assert result.summary.exit_code == 0
    assert all(case.status == "match" for case in result.cases)


# --------------------------------------------------------------------------- #
# Infrastructure failure classification
# --------------------------------------------------------------------------- #


async def _failing_lifespan_app(scope: Any, receive: Any, send: Any) -> None:
    if scope["type"] != "lifespan":
        return
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.failed", "message": "lifespan exploded"})
            return
        if message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


@pytest.mark.asyncio
async def test_asgi_lifespan_startup_failure_raises() -> None:
    target = AsgiTarget(
        "candidate",
        TargetConfig(kind="asgi", app="test:failing", label="candidate"),
        app=_failing_lifespan_app,
        size_limit=1024,
        follow_redirects=False,
        corpus_dir=Path(__file__).parent,
    )
    with pytest.raises(TargetInitError, match="startup failed: lifespan exploded"):
        await target.startup()


def _reserve_closed_port() -> int:
    """Reserve an ephemeral port number that is (momentarily) closed."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.mark.asyncio
async def test_connection_refusal_classified(tmp_path: Path) -> None:
    port = _reserve_closed_port()
    corpus_yaml = tmp_path / "closed.yaml"
    corpus_yaml.write_text(
        "version: 1\ncases:\n  - id: ping\n    method: GET\n    path: /health\n",
        encoding="utf-8",
    )
    corpus = load_corpus(corpus_yaml)
    config = AppConfig(
        old_target=TargetConfig(kind="http", url=f"http://127.0.0.1:{port}"),
        new_target=TargetConfig(kind="http", url=f"http://127.0.0.1:{port}"),
    )
    result = await run_comparison(corpus=corpus, config=config)
    assert result.summary.exit_code == 1
    case = result.cases[0]
    assert case.status == "failed"
    assert case.baseline is not None and case.baseline.error_kind == "transport"
    assert case.baseline.transport_error is not None


class _StallingServer:
    """TCP server that accepts connections but never writes a response."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._connections: list[socket.socket] = []
        self._running = threading.Event()
        self._running.set()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        while self._running.is_set():
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            self._connections.append(conn)

    def close(self) -> None:
        self._running.clear()
        self._sock.close()
        for conn in self._connections:
            conn.close()
        self._thread.join(timeout=5)


@pytest.mark.asyncio
async def test_timeout_classification(tmp_path: Path) -> None:
    stall = _StallingServer()
    try:
        corpus_yaml = tmp_path / "stall.yaml"
        corpus_yaml.write_text(
            "version: 1\n"
            "cases:\n"
            "  - id: stall\n"
            "    method: GET\n"
            "    path: /hangs\n"
            "    timeout_seconds: 0.3\n",
            encoding="utf-8",
        )
        corpus = load_corpus(corpus_yaml)
        config = AppConfig(
            old_target=TargetConfig(kind="http", url=f"http://127.0.0.1:{stall.port}"),
            new_target=TargetConfig(kind="http", url=f"http://127.0.0.1:{stall.port}"),
        )
        result = await run_comparison(corpus=corpus, config=config)
    finally:
        stall.close()

    assert result.summary.exit_code == 1
    case = result.cases[0]
    assert case.status == "failed"
    assert case.baseline is not None and case.baseline.error_kind == "timeout"
    assert case.candidate is not None and case.candidate.error_kind == "timeout"
    timeout_diffs = [d for d in case.differences if d.category == "timeout"]
    assert timeout_diffs, "expected timeout differences to be reported"


# --------------------------------------------------------------------------- #
# Zero outbound traffic on invalid input
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invalid_corpus_sends_zero_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from apiparity.targets.base import Target

    calls: list[str] = []

    async def spy(self: Any, case: Any) -> Any:  # pragma: no cover — must never run
        calls.append(case.id)
        raise AssertionError("execute() must not be called for an invalid corpus")

    monkeypatch.setattr(Target, "execute", spy)

    bad_corpus = tmp_path / "bad.yaml"
    bad_corpus.write_text("version: 1\ncases: []\n", encoding="utf-8")  # empty: schema error
    with pytest.raises(CorpusError):
        await run_comparison(corpus=load_corpus(bad_corpus), config=_wsgi_asgi_config())
    assert calls == []
