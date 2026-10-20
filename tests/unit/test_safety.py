"""Unit tests for fail-closed safety guards (apiparity.safety)."""

from __future__ import annotations

import pytest

from apiparity.exceptions import SafetyError
from apiparity.models import RequestCase, SafetyConfig, TargetConfig
from apiparity.safety import (
    MUTATING_METHODS,
    evaluate_safety,
    is_loopback_url,
    mutating_methods_in,
)


def case(method: str = "GET", *, enabled: bool = True, id: str = "c1") -> RequestCase:
    return RequestCase(id=id, method=method, path="/x", enabled=enabled)


class TestMutatingMethods:
    def test_membership(self) -> None:
        assert {"POST", "PUT", "PATCH", "DELETE", "TRACE"} == MUTATING_METHODS

    def test_mutating_methods_in_skips_disabled(self) -> None:
        cases = [case("GET"), case("POST", enabled=False), case("DELETE", id="c2")]
        assert mutating_methods_in(cases) == {"DELETE"}


class TestIsLoopbackUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000",
            "http://LOCALHOST/x",
            "http://127.0.0.1",
            "http://127.0.0.1:5000/api",
            "http://127.0.0.2",
            "http://[::1]:8080",
            "http://service.localhost",
        ],
    )
    def test_loopback(self, url: str) -> None:
        assert is_loopback_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com",
            "http://192.168.1.10",
            "http://10.0.0.1",
            "http://[fe80::1]",
            "",
            "not-a-url",
            "http://",
        ],
    )
    def test_remote_or_unparseable(self, url: str) -> None:
        assert not is_loopback_url(url)


class TestEvaluateSafety:
    def test_read_only_loopback_run_passes(self) -> None:
        cases = [case("GET"), case("HEAD", id="c2")]
        targets = [
            ("baseline", TargetConfig(kind="http", url="http://127.0.0.1:1")),
            ("candidate", TargetConfig(kind="http", url="http://localhost:2")),
        ]
        report = evaluate_safety(cases, targets, SafetyConfig())
        assert report.mutating_case_ids == ()
        assert report.remote_targets == ()
        assert len(report.loopback_targets) == 2

    def test_mutating_without_opt_in_refuses(self) -> None:
        cases = [case("POST")]
        with pytest.raises(SafetyError, match="mutating methods"):
            evaluate_safety(
                cases, [("baseline", TargetConfig(kind="wsgi", app="a:b"))], SafetyConfig()
            )

    def test_mutating_with_opt_in_allows(self) -> None:
        cases = [case("POST"), case("GET", id="c2")]
        report = evaluate_safety(
            cases,
            [("baseline", TargetConfig(kind="wsgi", app="a:b"))],
            SafetyConfig(allow_mutating_requests=True),
        )
        assert report.mutating_case_ids == ("c1",)
        assert report.granted_mutating is True

    def test_remote_target_without_opt_in_refuses(self) -> None:
        with pytest.raises(SafetyError, match="outside loopback"):
            evaluate_safety(
                [case()],
                [("candidate", TargetConfig(kind="http", url="http://example.com"))],
                SafetyConfig(),
            )

    def test_remote_target_with_opt_in_allows(self) -> None:
        report = evaluate_safety(
            [case()],
            [("candidate", TargetConfig(kind="http", url="http://example.com"))],
            SafetyConfig(allow_remote_targets=True),
        )
        assert len(report.remote_targets) == 1
        assert report.granted_remote is True

    def test_http_target_without_url_is_remote(self) -> None:
        with pytest.raises(SafetyError, match="outside loopback"):
            evaluate_safety(
                [case()],
                [("baseline", TargetConfig(kind="http"))],
                SafetyConfig(),
            )

    def test_in_process_targets_never_remote(self) -> None:
        report = evaluate_safety(
            [case()],
            [
                ("baseline", TargetConfig(kind="wsgi", app="legacy_flask.app:app")),
                ("candidate", TargetConfig(kind="asgi", app="migrated_fastapi.app:app")),
            ],
            SafetyConfig(),
        )
        assert report.remote_targets == ()
        assert len(report.in_process_targets) == 2

    def test_both_problems_listed_together(self) -> None:
        with pytest.raises(SafetyError) as excinfo:
            evaluate_safety(
                [case("PATCH")],
                [("baseline", TargetConfig(kind="http", url="http://example.com"))],
                SafetyConfig(),
            )
        message = str(excinfo.value)
        assert "PATCH" in message
        assert "outside loopback" in message

    def test_render_mentions_grants_and_rollback(self) -> None:
        report = evaluate_safety(
            [case("POST")],
            [("baseline", TargetConfig(kind="wsgi", app="a:b"))],
            SafetyConfig(allow_mutating_requests=True),
        )
        text = report.render()
        assert "mutating cases: 1" in text
        assert "ALLOWED" in text
        assert "cannot roll back" in text
        assert "in-process target" in text


class TestUrlSecretRedaction:
    """Target URLs must never reach summaries/messages with credentials intact."""

    LOOPBACK_URL = "http://admin:hunter2@127.0.0.1:5000/api?access_token=secret-value&x=1"
    REMOTE_URL = "http://admin:hunter2@example.invalid/api?access_token=secret-value"

    def test_loopback_summary_masks_credentials(self) -> None:
        report = evaluate_safety(
            [case()],
            [("baseline", TargetConfig(kind="http", url=self.LOOPBACK_URL))],
            SafetyConfig(),
        )
        listed = report.loopback_targets[0]
        for secret in ("hunter2", "admin", "secret-value"):
            assert secret not in listed, f"secret {secret!r} leaked into safety summary"
        assert "[REDACTED]" in listed
        rendered = report.render()
        for secret in ("hunter2", "admin", "secret-value"):
            assert secret not in rendered

    def test_remote_summary_masks_credentials_when_granted(self) -> None:
        report = evaluate_safety(
            [case()],
            [("candidate", TargetConfig(kind="http", url=self.REMOTE_URL))],
            SafetyConfig(allow_remote_targets=True),
        )
        listed = report.remote_targets[0]
        for secret in ("hunter2", "admin", "secret-value"):
            assert secret not in listed
        for secret in ("hunter2", "admin", "secret-value"):
            assert secret not in report.render()

    def test_refusal_message_masks_credentials(self) -> None:
        with pytest.raises(SafetyError) as excinfo:
            evaluate_safety(
                [case()],
                [("candidate", TargetConfig(kind="http", url=self.REMOTE_URL))],
                SafetyConfig(),
            )
        message = str(excinfo.value)
        for secret in ("hunter2", "admin", "secret-value"):
            assert secret not in message, f"secret {secret!r} leaked into SafetyError"

    def test_non_sensitive_query_parameters_survive(self) -> None:
        report = evaluate_safety(
            [case()],
            [("baseline", TargetConfig(kind="http", url="http://127.0.0.1:5000/api?x=1&y=2"))],
            SafetyConfig(),
        )
        assert "?x=1&y=2" in report.loopback_targets[0]
