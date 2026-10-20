"""Security tests: secret non-disclosure, safety refusals, zero-leak guarantees.

These verify the redaction and safety contracts from REQUIREMENTS.md §7/§14:

* secrets (Authorization/X-Api-Key request headers, Set-Cookie response
  headers, ``/credentials/token`` JSON values) never appear in report.json,
  junit.xml, or terminal output — even with body previews enabled;
* mutating corpora and remote targets are refused before any request is sent;
* URL userinfo credentials and sensitive query parameters never leak into
  safety summaries, reports, terminal output, or error messages;
* redirect hops cannot escape the loopback policy without
  ``allow_remote_targets``;
* a failed run never reports success.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from apiparity.corpus import Corpus, load_corpus
from apiparity.exceptions import SafetyError
from apiparity.models import AppConfig, RedactionConfig, RequestCase, SafetyConfig, TargetConfig
from apiparity.reports import render_terminal, write_reports
from apiparity.reports.json_report import build_json_report
from apiparity.runner import run_comparison
from apiparity.safety import evaluate_safety
from apiparity.targets.http_target import RemoteGuardedTransport, UnsafeRedirectError
from apiparity.targets.loader import resolve_target

CORPORA_DIR = Path(__file__).parent.parent / "fixtures" / "corpora"

SECRETS = ("hunter2", "secret-key-123")
URL_SECRETS = ("hunter2", "admin", "secret-value")


def _app_config(**kwargs: object) -> AppConfig:
    return AppConfig(
        old_target=TargetConfig(kind="wsgi", app="legacy_flask.app:app"),
        new_target=TargetConfig(kind="asgi", app="migrated_fastapi.app:app"),
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_secrets_absent_from_all_reports(tmp_path: Path) -> None:
    corpus = load_corpus(CORPORA_DIR / "secrets.yaml")
    config = _app_config()
    result = await run_comparison(corpus=corpus, config=config)
    assert result.summary.exit_code == 0, "fixture apps should be equivalent here"

    written = write_reports(
        result,
        tmp_path / "out",
        reporting=config.reporting,
        redaction=config.redaction,
        include_body_previews=True,  # worst case: previews explicitly enabled
    )
    assert len(written) == 2  # report.json + junit.xml

    report_text = (tmp_path / "out" / "report.json").read_text(encoding="utf-8")
    junit_text = (tmp_path / "out" / "junit.xml").read_text(encoding="utf-8")
    for secret in SECRETS:
        assert secret not in report_text, f"secret {secret!r} leaked into report.json"
        assert secret not in junit_text, f"secret {secret!r} leaked into junit.xml"

    # Redaction must appear as masking, not merely as absence of the body.
    report = json.loads(report_text)
    session_case = next(c for c in report["cases"] if c["id"] == "session")
    previews = json.dumps(session_case)
    assert "[REDACTED]" in previews

    terminal = render_terminal(result, verbose=True, quiet=False, redaction=RedactionConfig())
    for secret in SECRETS:
        assert secret not in terminal, f"secret {secret!r} leaked into terminal output"


@pytest.mark.asyncio
async def test_mutating_refused_before_any_target_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apiparity.targets.base import Target

    calls: list[str] = []

    async def spy(self: object, case: object) -> object:  # pragma: no cover
        calls.append("execute")
        raise AssertionError("no request may be sent after a safety refusal")

    monkeypatch.setattr(Target, "execute", spy)

    corpus = load_corpus(CORPORA_DIR / "mutating.yaml")
    with pytest.raises(SafetyError, match="mutating"):
        await run_comparison(corpus=corpus, config=_app_config())
    assert calls == []


def test_remote_targets_refused_without_opt_in() -> None:
    corpus = load_corpus(CORPORA_DIR / "equivalent.yaml")
    config = AppConfig(
        old_target=TargetConfig(kind="http", url="http://example.com"),
        new_target=TargetConfig(kind="http", url="http://127.0.0.1:9"),
    )
    # evaluate_safety is what run_comparison calls before touching the network.
    assert config.old_target is not None and config.new_target is not None
    with pytest.raises(SafetyError, match="loopback"):
        evaluate_safety(
            corpus.cases,
            [("baseline", config.old_target), ("candidate", config.new_target)],
            config.safety,
        )


@pytest.mark.asyncio
async def test_userinfo_credentials_never_leak(tmp_path: Path) -> None:
    """A URL with userinfo to a dead port: error paths must not echo secrets."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()

    corpus_yaml = tmp_path / "ping.yaml"
    corpus_yaml.write_text(
        "version: 1\ncases:\n  - id: ping\n    method: GET\n    path: /health\n",
        encoding="utf-8",
    )
    corpus = load_corpus(corpus_yaml)
    config = AppConfig(
        old_target=TargetConfig(kind="http", url=f"http://user:hunter2@127.0.0.1:{dead_port}"),
        new_target=TargetConfig(kind="http", url=f"http://127.0.0.1:{dead_port}"),
    )
    result = await run_comparison(corpus=corpus, config=config)
    assert result.summary.exit_code != 0

    serialized = json.dumps(
        build_json_report(result, redaction=RedactionConfig(), include_body_previews=True)
    )
    assert "hunter2" not in serialized, "URL userinfo leaked into the report"
    # describe() strips userinfo from target labels shown in reports.
    assert result.target_descriptions["baseline"] == f"baseline: http://127.0.0.1:{dead_port}"


@pytest.mark.asyncio
async def test_failed_run_never_reports_success(tmp_path: Path) -> None:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()

    corpus_yaml = tmp_path / "ping.yaml"
    corpus_yaml.write_text(
        "version: 1\ncases:\n  - id: ping\n    method: GET\n    path: /health\n",
        encoding="utf-8",
    )
    corpus = load_corpus(corpus_yaml)
    config = AppConfig(
        old_target=TargetConfig(kind="http", url=f"http://127.0.0.1:{dead_port}"),
        new_target=TargetConfig(kind="http", url=f"http://127.0.0.1:{dead_port}"),
    )
    result = await run_comparison(corpus=corpus, config=config)
    assert result.result_kind != "pass"
    assert result.summary.exit_code != 0
    terminal = render_terminal(result, verbose=False, quiet=False, redaction=RedactionConfig())
    assert "RESULT: PASS" not in terminal


# --------------------------------------------------------------------------- #
# URL credentials: summaries, metadata, reports, terminal output
# --------------------------------------------------------------------------- #


def _dead_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _ping_corpus(tmp_path: Path) -> Corpus:
    corpus_yaml = tmp_path / "ping.yaml"
    corpus_yaml.write_text(
        "version: 1\ncases:\n  - id: ping\n    method: GET\n    path: /health\n",
        encoding="utf-8",
    )
    return load_corpus(corpus_yaml)


def _assert_no_url_secrets(*texts: str, context: str) -> None:
    for text in texts:
        for secret in URL_SECRETS:
            assert secret not in text, f"URL secret {secret!r} leaked ({context})"


@pytest.mark.asyncio
async def test_loopback_url_credentials_never_leak_end_to_end(tmp_path: Path) -> None:
    """Loopback URL with userinfo + sensitive query: safety summary, target
    descriptions, JSON report, junit, and terminal output must all be clean."""
    corpus = _ping_corpus(tmp_path)
    url = f"http://admin:hunter2@127.0.0.1:{_dead_port()}?access_token=secret-value"
    config = AppConfig(
        old_target=TargetConfig(kind="http", url=url),
        new_target=TargetConfig(kind="http", url=f"http://127.0.0.1:{_dead_port()}"),
    )
    assert config.old_target is not None and config.new_target is not None
    safety_report = evaluate_safety(
        corpus.cases,
        [("baseline", config.old_target), ("candidate", config.new_target)],
        config.safety,
    )
    result = await run_comparison(corpus=corpus, config=config)
    assert result.summary.exit_code != 0

    written = write_reports(
        result,
        tmp_path / "out",
        reporting=config.reporting,
        redaction=config.redaction,
        include_body_previews=True,
    )
    report_text = (tmp_path / "out" / "report.json").read_text(encoding="utf-8")
    junit_text = (tmp_path / "out" / "junit.xml").read_text(encoding="utf-8")
    terminal = render_terminal(result, verbose=True, quiet=False, redaction=RedactionConfig())
    _assert_no_url_secrets(
        safety_report.render(),
        report_text,
        junit_text,
        terminal,
        json.dumps(result.target_descriptions),
        json.dumps(result.normalization_rules_applied),
        context="loopback run",
    )
    assert "[REDACTED]" in safety_report.render()
    assert len(written) == 2


@pytest.mark.asyncio
async def test_remote_url_credentials_never_leak_when_authorized(tmp_path: Path) -> None:
    """With remote access granted, the printed/stored target URL still carries
    no credentials into any output artifact."""
    corpus = _ping_corpus(tmp_path)
    url = "http://admin:hunter2@203.0.113.1:81/api?access_token=secret-value"
    config = AppConfig(
        old_target=TargetConfig(kind="http", url=url, timeout_seconds=0.3),
        new_target=TargetConfig(kind="http", url=f"http://127.0.0.1:{_dead_port()}"),
        safety=SafetyConfig(allow_remote_targets=True),
    )
    assert config.old_target is not None and config.new_target is not None
    safety_report = evaluate_safety(
        corpus.cases,
        [("baseline", config.old_target), ("candidate", config.new_target)],
        config.safety,
    )
    assert safety_report.granted_remote is True
    result = await run_comparison(corpus=corpus, config=config)
    assert result.summary.exit_code != 0

    write_reports(
        result,
        tmp_path / "out",
        reporting=config.reporting,
        redaction=config.redaction,
        include_body_previews=True,
    )
    report_text = (tmp_path / "out" / "report.json").read_text(encoding="utf-8")
    terminal = render_terminal(result, verbose=True, quiet=False, redaction=RedactionConfig())
    _assert_no_url_secrets(
        safety_report.render(),
        report_text,
        terminal,
        json.dumps(result.target_descriptions),
        context="remote-allowed run",
    )
    assert "[REDACTED]" in safety_report.render()


# --------------------------------------------------------------------------- #
# Redirect hops must not escape the loopback policy
# --------------------------------------------------------------------------- #


class _RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requested: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requested.append(str(request.url))
        return httpx.Response(200, request=request)


class TestRedirectHopGuard:
    def test_non_loopback_refused_before_dialing(self) -> None:
        inner = _RecordingTransport()
        transport = RemoteGuardedTransport(inner, allow_remote=False)
        request = httpx.Request("GET", "http://example.com/steal?token=x")
        with pytest.raises(UnsafeRedirectError, match="outside loopback"):
            asyncio.run(transport.handle_async_request(request))
        assert inner.requested == [], "the wrapped transport must never be reached"

    def test_loopback_passes_through(self) -> None:
        inner = _RecordingTransport()
        transport = RemoteGuardedTransport(inner, allow_remote=False)
        response = asyncio.run(
            transport.handle_async_request(httpx.Request("GET", "http://127.0.0.1:5000/x"))
        )
        assert response.status_code == 200
        assert inner.requested == ["http://127.0.0.1:5000/x"]

    def test_remote_passes_when_granted(self) -> None:
        inner = _RecordingTransport()
        transport = RemoteGuardedTransport(inner, allow_remote=True)
        response = asyncio.run(
            transport.handle_async_request(httpx.Request("GET", "http://example.com/x"))
        )
        assert response.status_code == 200
        assert inner.requested == ["http://example.com/x"]

    def test_refusal_message_redacts_url(self) -> None:
        inner = _RecordingTransport()
        transport = RemoteGuardedTransport(inner, allow_remote=False)
        with pytest.raises(UnsafeRedirectError) as excinfo:
            asyncio.run(
                transport.handle_async_request(
                    httpx.Request(
                        "GET", "http://user:hunter2@example.com/?access_token=secret-value"
                    )
                )
            )
        message = str(excinfo.value)
        assert "hunter2" not in message
        assert "secret-value" not in message


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(302)
        self.send_header("Location", "http://203.0.113.1/steal")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def redirect_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=10)


@pytest.mark.asyncio
async def test_redirect_from_loopback_to_remote_refused(redirect_server: str) -> None:
    """A loopback service redirecting to a non-loopback host must not produce
    a remote request when allow_remote_targets is off."""
    target = resolve_target(
        TargetConfig(kind="http", url=redirect_server),
        side="baseline",
        size_limit=1024,
        follow_redirects=True,
        corpus_dir=Path(__file__).parent,
        default_timeout=3.0,
        allow_remote=False,
    )
    await target.startup()
    try:
        snapshot = await target.execute(RequestCase(id="r", method="GET", path="/"))
    finally:
        await target.shutdown()
    assert snapshot.error_kind == "transport"
    assert snapshot.transport_error is not None
    assert "outside loopback" in snapshot.transport_error
    assert "allow_remote_targets" in snapshot.transport_error


@pytest.mark.asyncio
async def test_redirect_followed_when_remote_allowed(redirect_server: str) -> None:
    target = resolve_target(
        TargetConfig(kind="http", url=redirect_server),
        side="baseline",
        size_limit=1024,
        follow_redirects=True,
        corpus_dir=Path(__file__).parent,
        default_timeout=0.5,
        allow_remote=True,
    )
    await target.startup()
    try:
        snapshot = await target.execute(RequestCase(id="r", method="GET", path="/"))
    finally:
        await target.shutdown()
    # The hop was permitted; the failure is the (unreachable) remote host.
    assert snapshot.transport_error is not None
    assert "outside loopback" not in snapshot.transport_error
