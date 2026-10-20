"""Security tests: secret non-disclosure, safety refusals, zero-leak guarantees.

These verify the redaction and safety contracts from REQUIREMENTS.md §7/§14:

* secrets (Authorization/X-Api-Key request headers, Set-Cookie response
  headers, ``/credentials/token`` JSON values) never appear in report.json,
  junit.xml, or terminal output — even with body previews enabled;
* mutating corpora and remote targets are refused before any request is sent;
* URL userinfo credentials never leak into reports or error messages;
* a failed run never reports success.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apiparity.corpus import load_corpus
from apiparity.exceptions import SafetyError
from apiparity.models import AppConfig, RedactionConfig, TargetConfig
from apiparity.reports import render_terminal, write_reports
from apiparity.reports.json_report import build_json_report
from apiparity.runner import run_comparison
from apiparity.safety import evaluate_safety

CORPORA_DIR = Path(__file__).parent.parent / "fixtures" / "corpora"

SECRETS = ("hunter2", "secret-key-123")


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
