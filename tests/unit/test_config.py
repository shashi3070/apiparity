from __future__ import annotations

import json
from pathlib import Path

import pytest

from apiparity.config import (
    build_config,
    config_fingerprint,
    load_config_file,
    load_config_text,
    merge_cli_over_file,
)
from apiparity.exceptions import ConfigError
from apiparity.models import AppConfig, SafetyConfig, TargetConfig

FULL_CONFIG = """
version: 1
defaults:
  timeout_seconds: 7
  follow_redirects: false
safety:
  allow_mutating_requests: true
  allow_remote_targets: false
comparison:
  compare_status: true
  compare_headers: false
  compare_body: true
  max_differences_per_case: 25
  response_size_limit_bytes: 1048576
  timing: {mode: warn, max_ms: 500}
normalization:
  ignored_headers: [Date, X-Trace]
  ignored_json_pointers: ["/metadata/request_id"]
  whitespace: {text: true}
  array_order: {"/items": sort}
redaction:
  sensitive_headers: [authorization, x-custom-secret]
  sensitive_json_pointers: ["/credentials/token", "/user/ssn"]
reporting:
  json: true
  junit_xml: false
  terminal: true
targets:
  old: {kind: http, url: "http://127.0.0.1:5000"}
  new: {kind: asgi, app: "migrated_fastapi.app:app", factory: false}
"""


class TestValidConfig:
    def test_full_config(self, tmp_path: Path) -> None:
        path = tmp_path / "apiparity.yaml"
        path.write_text(FULL_CONFIG, encoding="utf-8")
        config = load_config_file(path)
        assert config.timeout_seconds == 7
        assert config.safety.allow_mutating_requests is True
        assert config.compare_headers is False
        assert config.max_differences_per_case == 25
        assert config.timing_mode == "warn"
        assert config.timing_max_ms == 500
        assert config.normalization.ignored_headers == ("date", "x-trace")
        assert config.normalization.whitespace_text is True
        assert config.normalization.array_order == {"/items": "sort"}
        assert "x-custom-secret" in config.redaction.sensitive_headers
        assert config.reporting.junit_xml is False
        assert config.old_target is not None and config.old_target.kind == "http"
        assert config.new_target is not None and config.new_target.kind == "asgi"
        assert config.source_path == str(path)

    def test_normalization_section_without_ignored_headers_keeps_defaults(
        self, tmp_path: Path
    ) -> None:
        """Omitting ignored_headers must not clobber the built-in defaults."""
        path = tmp_path / "apiparity.yaml"
        path.write_text(
            'version: 1\nnormalization:\n  ignored_json_pointers: ["/metadata/request_id"]\n',
            encoding="utf-8",
        )
        config = load_config_file(path)
        assert config.normalization.ignored_headers == (
            "date",
            "content-length",
            "server",
            "connection",
        )
        assert config.normalization.ignored_json_pointers == ("/metadata/request_id",)

    def test_normalization_explicit_ignored_headers_replaces_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "apiparity.yaml"
        path.write_text(
            "version: 1\nnormalization:\n  ignored_headers: [X-Trace]\n",
            encoding="utf-8",
        )
        config = load_config_file(path)
        assert config.normalization.ignored_headers == ("x-trace",)

    def test_json_config(self, tmp_path: Path) -> None:
        path = tmp_path / "apiparity.json"
        path.write_text(
            json.dumps({"version": 1, "defaults": {"timeout_seconds": 3}}), encoding="utf-8"
        )
        assert load_config_file(path).timeout_seconds == 3

    def test_empty_config_uses_defaults(self) -> None:
        config = load_config_text("")
        assert config.timeout_seconds is None
        assert config.safety == SafetyConfig()
        assert config.reporting.json is True

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config_file(tmp_path / "absent.yaml")


class TestConfigRejections:
    @pytest.mark.parametrize(
        "document,match",
        [
            ({"version": 2}, "version"),
            ({"version": 1, "nope": {}}, "unknown field"),
            ({"version": 1, "defaults": {"timeout_seconds": -1}}, "positive"),
            ({"version": 1, "defaults": {"timeout_seconds": "x"}}, "positive"),
            ({"version": 1, "defaults": {"follow_redirects": "yes"}}, "boolean"),
            ({"version": 1, "safety": {"allow_mutating_requests": 1}}, "boolean"),
            ({"version": 1, "comparison": {"max_differences_per_case": 0}}, "positive integer"),
            ({"version": 1, "comparison": {"surprise": 1}}, "unknown field"),
            ({"version": 1, "comparison": {"timing": {"mode": "sometimes"}}}, "timing.mode"),
            ({"version": 1, "comparison": {"timing": {"surprise": 1}}}, "unknown field"),
            (
                {"version": 1, "redaction": {"sensitive_headers": "authorization"}},
                "list of strings",
            ),
            ({"version": 1, "reporting": {"json": "true"}}, "boolean"),
            ({"version": 1, "targets": "nope"}, "mapping"),
            ({"version": 1, "targets": {"old": {"kind": "ftp"}}}, "kind"),
            ({"version": 1, "targets": {"old": {"kind": "http"}}}, "url is required"),
            ({"version": 1, "targets": {"old": {"url": "ftp://x"}}}, "http:// or https://"),
            (
                {"version": 1, "targets": {"old": {"kind": "wsgi", "app": "bad spec"}}},
                "package.module",
            ),
            ({"version": 1, "targets": {"old": {"kind": "http", "app": "a:b"}}}, "not valid"),
            ({"version": 1, "targets": {"old": {"kind": "asgi", "url": "http://x"}}}, "not valid"),
            ({"version": 1, "targets": {"middle": {}}}, "unknown field"),
            (
                {
                    "version": 1,
                    "targets": {"new": {"kind": "wsgi", "app": "a:b", "lifespan": "off"}},
                },
                "lifespan only applies",
            ),
            (
                {
                    "version": 1,
                    "targets": {"new": {"kind": "http", "url": "http://x", "lifespan": "off"}},
                },
                "lifespan only applies",
            ),
            (
                {
                    "version": 1,
                    "targets": {"new": {"kind": "asgi", "app": "a:b", "lifespan": "sometimes"}},
                },
                "lifespan must be one of",
            ),
            (
                {
                    "version": 1,
                    "targets": {"new": {"kind": "asgi", "app": "a:b", "lifespan": False}},
                },
                "quote it",
            ),
            ({"version": 1, "normalization": {"ignored_json_pointers": ["bad"]}}, "JSON Pointer"),
            ({"version": 1, "normalization": {"array_order": {"/x": "rotate"}}}, "preserve"),
        ],
    )
    def test_invalid_documents(self, document: dict, match: str) -> None:
        with pytest.raises(ConfigError, match=match):
            build_config(document)

    def test_invalid_yaml(self) -> None:
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_config_text("version: 1\nsafety: [unclosed")

    def test_root_not_mapping(self) -> None:
        with pytest.raises(ConfigError, match="mapping"):
            load_config_text("- 1\n- 2")


class TestTargetDescriptions:
    def test_http_url_userinfo_stripped(self) -> None:
        target = TargetConfig(kind="http", url="https://user:pass@example.com:8443/api?k=1")
        described = target.describe()
        assert "user" not in described
        assert "pass" not in described
        assert described == "https://example.com:8443/api?k=1"

    def test_plain_url_untouched(self) -> None:
        target = TargetConfig(kind="http", url="http://127.0.0.1:5000/base")
        assert target.describe() == "http://127.0.0.1:5000/base"

    def test_app_description_with_factory(self) -> None:
        target = TargetConfig(kind="wsgi", app="legacy_flask.app:app", factory=True)
        assert target.describe() == "wsgi:legacy_flask.app:app (factory)"


class TestTargetLifespan:
    def test_asgi_lifespan_modes_parse(self) -> None:
        for mode in ("auto", "required", "off"):
            config = load_config_text(
                "version: 1\n"
                f"targets: {{new: {{kind: asgi, app: 'pkg.mod:app', lifespan: '{mode}'}}}}\n"
            )
            assert config.new_target is not None
            assert config.new_target.lifespan == mode

    def test_default_lifespan_is_auto(self) -> None:
        config = load_config_text("version: 1\ntargets: {new: {kind: asgi, app: 'pkg.mod:app'}}\n")
        assert config.new_target is not None
        assert config.new_target.lifespan == "auto"


class TestMergePrecedence:
    def _file_config(self) -> AppConfig:
        return load_config_text(FULL_CONFIG)

    def test_cli_scalar_overrides_file(self) -> None:
        merged = merge_cli_over_file(self._file_config(), {"timeout_seconds": 99.0})
        assert merged.timeout_seconds == 99.0
        assert merged.compare_headers is False  # untouched file value

    def test_safety_is_grant_only(self) -> None:
        base = load_config_text("version: 1\nsafety: {allow_remote_targets: true}")
        merged = merge_cli_over_file(base, {"allow_mutating_requests": True})
        assert merged.safety.allow_remote_targets is True
        assert merged.safety.allow_mutating_requests is True

    def test_cli_false_does_not_revoke_file_true(self) -> None:
        base = load_config_text("version: 1\nsafety: {allow_mutating_requests: true}")
        merged = merge_cli_over_file(base, {"allow_mutating_requests": False})
        assert merged.safety.allow_mutating_requests is True

    def test_target_override(self) -> None:
        merged = merge_cli_over_file(
            self._file_config(), {"old_target": TargetConfig(kind="http", url="http://127.0.0.1:1")}
        )
        assert merged.old_target is not None
        assert merged.old_target.url == "http://127.0.0.1:1"
        assert merged.new_target is not None  # file value retained


class TestFingerprint:
    def test_stable_for_same_config(self) -> None:
        a = load_config_text(FULL_CONFIG)
        b = load_config_text(FULL_CONFIG)
        assert config_fingerprint(a) == config_fingerprint(b)

    def test_changes_when_setting_changes(self) -> None:
        a = load_config_text(FULL_CONFIG)
        b = load_config_text(FULL_CONFIG.replace("timeout_seconds: 7", "timeout_seconds: 8"))
        assert config_fingerprint(a) != config_fingerprint(b)

    def test_contains_no_credentials(self) -> None:
        config = load_config_text(
            "version: 1\ntargets: {old: {url: 'http://user:secret@127.0.0.1:5000'}}"
        )
        assert "secret" not in config_fingerprint(config)
