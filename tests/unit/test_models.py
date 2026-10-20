from __future__ import annotations

from apiparity.models import AppConfig, ComparisonConfig, NormalizationConfig, ResponseSnapshot


class TestEffectiveComparisonPrecedence:
    def test_builtins_when_nothing_configured(self) -> None:
        config = AppConfig()
        effective = config.effective_comparison()
        assert effective == ComparisonConfig()
        assert config.effective_timeout() == 10.0

    def test_corpus_defaults_beat_builtins(self) -> None:
        config = AppConfig()
        effective = config.effective_comparison({"compare_body": False, "timeout_seconds": 3})
        assert effective.compare_body is False
        assert config.effective_timeout({"timeout_seconds": 3}) == 3.0

    def test_config_file_beats_corpus_defaults(self) -> None:
        config = AppConfig(compare_body=True, timeout_seconds=20.0)
        effective = config.effective_comparison({"compare_body": False, "timeout_seconds": 3})
        assert effective.compare_body is True
        assert config.effective_timeout({"timeout_seconds": 3}) == 20.0

    def test_timing_from_corpus_defaults(self) -> None:
        config = AppConfig()
        effective = config.effective_comparison({"timing_mode": "warn", "timing_max_ms": 250.0})
        assert effective.timing_mode == "warn"
        assert effective.timing_max_ms == 250.0

    def test_timing_config_file_wins(self) -> None:
        config = AppConfig(timing_mode="fail", timing_max_ms=999.0)
        effective = config.effective_comparison({"timing_mode": "warn", "timing_max_ms": 250.0})
        assert effective.timing_mode == "fail"
        assert effective.timing_max_ms == 999.0


class TestResponseSnapshot:
    def test_header_map_and_ok(self) -> None:
        snapshot = ResponseSnapshot(
            status_code=200,
            headers=(("content-type", ("application/json",)), ("set-cookie", ("a=1", "b=2"))),
            content_type="application/json",
            body_bytes=b"{}",
            body_truncated=False,
            body_size=2,
            duration_ms=1.5,
            transport_error=None,
        )
        assert snapshot.header_map()["set-cookie"] == ("a=1", "b=2")
        assert snapshot.ok is True

    def test_not_ok_on_transport_error(self) -> None:
        snapshot = ResponseSnapshot(
            status_code=None,
            headers=(),
            content_type=None,
            body_bytes=None,
            body_truncated=False,
            body_size=0,
            duration_ms=1.0,
            transport_error="connection refused",
            error_kind="transport",
        )
        assert snapshot.ok is False


class TestNormalizationDefaults:
    def test_default_ignore_list_is_small(self) -> None:
        config = NormalizationConfig()
        assert set(config.ignored_headers) == {"date", "content-length", "server", "connection"}
        assert config.ignored_json_pointers == ()
        assert config.whitespace_text is False
        assert config.array_order == {}
