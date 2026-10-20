"""Project-level configuration file loading and precedence merging.

Schema (``version: 1``) is documented in ``REQUIREMENTS.md`` §8 and
``docs/configuration.md``. Precedence (highest wins):

    CLI flags > config file > corpus defaults > built-in defaults

Safety flags are grant-only: they can only turn a permission ON, never off.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

import yaml

from apiparity.exceptions import ConfigError
from apiparity.models import (
    AppConfig,
    LifespanMode,
    NormalizationConfig,
    RedactionConfig,
    ReportingConfig,
    SafetyConfig,
    TargetConfig,
    TargetKind,
)

__all__ = ["config_fingerprint", "load_config_file", "load_config_text", "merge_cli_over_file"]

_TOP_LEVEL_KEYS = {
    "version",
    "defaults",
    "safety",
    "comparison",
    "normalization",
    "redaction",
    "reporting",
    "targets",
}
_DEFAULT_KEYS = {
    "timeout_seconds",
    "follow_redirects",
}
_COMPARISON_KEYS = {
    "compare_status",
    "compare_headers",
    "compare_body",
    "max_differences_per_case",
    "response_size_limit_bytes",
    "timing",
}
_TIMING_KEYS = {"mode", "max_ms"}
_NORMALIZATION_KEYS = {"ignored_headers", "ignored_json_pointers", "whitespace", "array_order"}
_WHITESPACE_KEYS = {"text"}
_REDACTION_KEYS = {"sensitive_headers", "sensitive_json_pointers", "header_name_substrings"}
_REPORTING_KEYS = {"json", "junit_xml", "terminal", "include_body_previews"}
_TARGET_KEYS = {
    "kind",
    "url",
    "app",
    "factory",
    "verify_tls",
    "timeout_seconds",
    "lifespan",
    "label",
}
_TARGET_SIDES = {"old", "new"}

_APP_SPEC_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


def load_config_file(path: str | Path) -> AppConfig:
    """Load and validate a YAML/JSON project configuration file."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file: {exc}") from exc
    config = load_config_text(text, source_path=config_path)
    return _with_source(config, str(config_path))


def load_config_text(text: str, *, source_path: Path | None = None) -> AppConfig:
    try:
        if source_path is not None and source_path.suffix.lower() == ".json":
            document: Any = json.loads(text)
        else:
            document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"invalid YAML in config file: {str(exc).replace(chr(10), ' ')[:200]}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid JSON in config file at line {exc.lineno} col {exc.colno}: {exc.msg}"
        ) from exc
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise ConfigError("config root must be a mapping/object")
    return build_config(document)


def build_config(document: dict[str, Any]) -> AppConfig:
    """Validate an already-parsed config document. Exposed for unit tests."""
    _reject_unknown(document, _TOP_LEVEL_KEYS, location="<config>")
    version = document.get("version", 1)
    if version != 1:
        raise ConfigError("unsupported or missing 'version' (expected 1)")

    timeout: float | None = None
    follow_redirects: bool | None = None
    if "defaults" in document:
        defaults = _section(document, "defaults", _DEFAULT_KEYS)
        if "timeout_seconds" in defaults:
            timeout = _positive_number(defaults["timeout_seconds"], "defaults.timeout_seconds")
        if "follow_redirects" in defaults:
            follow_redirects = _bool(defaults["follow_redirects"], "defaults.follow_redirects")

    safety = SafetyConfig()
    if "safety" in document:
        section = _section(document, "safety", {"allow_mutating_requests", "allow_remote_targets"})
        safety = SafetyConfig(
            allow_mutating_requests=_bool(
                section.get("allow_mutating_requests", False),
                "safety.allow_mutating_requests",
            ),
            allow_remote_targets=_bool(
                section.get("allow_remote_targets", False),
                "safety.allow_remote_targets",
            ),
        )

    compare_status: bool | None = None
    compare_headers: bool | None = None
    compare_body: bool | None = None
    max_differences: int | None = None
    size_limit: int | None = None
    timing_mode: str | None = None
    timing_max_ms: float | None = None
    if "comparison" in document:
        section = _section(document, "comparison", _COMPARISON_KEYS)
        _reject_unknown(section.get("timing", {}), _TIMING_KEYS, location="comparison.timing")
        if "compare_status" in section:
            compare_status = _bool(section["compare_status"], "comparison.compare_status")
        if "compare_headers" in section:
            compare_headers = _bool(section["compare_headers"], "comparison.compare_headers")
        if "compare_body" in section:
            compare_body = _bool(section["compare_body"], "comparison.compare_body")
        if "max_differences_per_case" in section:
            max_differences = _positive_int(
                section["max_differences_per_case"], "comparison.max_differences_per_case"
            )
        if "response_size_limit_bytes" in section:
            size_limit = _positive_int(
                section["response_size_limit_bytes"], "comparison.response_size_limit_bytes"
            )
        timing = section.get("timing", {})
        if not isinstance(timing, dict):
            raise ConfigError("comparison.timing must be a mapping")
        if "mode" in timing:
            if timing["mode"] not in ("off", "warn", "fail"):
                raise ConfigError("comparison.timing.mode must be one of 'off', 'warn', 'fail'")
            timing_mode = str(timing["mode"])
        if timing.get("max_ms") is not None:
            timing_max_ms = _positive_number(timing["max_ms"], "comparison.timing.max_ms")

    normalization = NormalizationConfig()
    if "normalization" in document:
        normalization = _build_normalization(
            _section(document, "normalization", _NORMALIZATION_KEYS)
        )

    redaction = RedactionConfig()
    if "redaction" in document:
        section = _section(document, "redaction", _REDACTION_KEYS)
        redaction = RedactionConfig(
            sensitive_headers=tuple(
                h.lower()
                for h in _str_list(section.get("sensitive_headers"), "redaction.sensitive_headers")
            )
            if "sensitive_headers" in section
            else RedactionConfig().sensitive_headers,
            sensitive_json_pointers=tuple(
                _str_list(
                    section.get("sensitive_json_pointers"), "redaction.sensitive_json_pointers"
                )
            )
            if "sensitive_json_pointers" in section
            else RedactionConfig().sensitive_json_pointers,
            header_name_substrings=tuple(
                s.lower()
                for s in _str_list(
                    section.get("header_name_substrings"), "redaction.header_name_substrings"
                )
            )
            if "header_name_substrings" in section
            else RedactionConfig().header_name_substrings,
        )

    reporting = ReportingConfig()
    if "reporting" in document:
        section = _section(document, "reporting", _REPORTING_KEYS)
        reporting = ReportingConfig(
            json=_bool(section.get("json", True), "reporting.json") if "json" in section else True,
            junit_xml=_bool(section.get("junit_xml", True), "reporting.junit_xml")
            if "junit_xml" in section
            else True,
            terminal=_bool(section.get("terminal", True), "reporting.terminal")
            if "terminal" in section
            else True,
            include_body_previews=_bool(
                section.get("include_body_previews", False),
                "reporting.include_body_previews",
            )
            if "include_body_previews" in section
            else False,
        )

    old_target: TargetConfig | None = None
    new_target: TargetConfig | None = None
    if "targets" in document:
        targets = document["targets"]
        if not isinstance(targets, dict):
            raise ConfigError("'targets' must be a mapping with 'old' and/or 'new'")
        _reject_unknown(targets, _TARGET_SIDES, location="targets")
        if "old" in targets:
            old_target = _build_target(targets["old"], location="targets.old", side="old")
        if "new" in targets:
            new_target = _build_target(targets["new"], location="targets.new", side="new")

    return AppConfig(
        timeout_seconds=timeout,
        follow_redirects=follow_redirects,
        compare_status=compare_status,
        compare_headers=compare_headers,
        compare_body=compare_body,
        max_differences_per_case=max_differences,
        response_size_limit_bytes=size_limit,
        timing_mode=timing_mode,  # type: ignore[arg-type]
        timing_max_ms=timing_max_ms,
        safety=safety,
        normalization=normalization,
        redaction=redaction,
        reporting=reporting,
        old_target=old_target,
        new_target=new_target,
    )


def merge_cli_over_file(file_config: AppConfig, cli: dict[str, Any]) -> AppConfig:
    """Apply CLI-provided overrides on top of a parsed config file.

    ``cli`` only contains keys the user actually passed (or explicit flags).
    Safety flags are grant-only: ``True`` in either layer wins.
    """
    from dataclasses import replace

    merged = file_config
    simple_fields = (
        "timeout_seconds",
        "follow_redirects",
        "compare_status",
        "compare_headers",
        "compare_body",
        "max_differences_per_case",
        "response_size_limit_bytes",
        "timing_mode",
        "timing_max_ms",
    )
    updates = {key: cli[key] for key in simple_fields if cli.get(key) is not None}
    if updates:
        merged = replace(merged, **updates)

    safety = merged.safety
    if cli.get("allow_mutating_requests") or cli.get("allow_remote_targets"):
        safety = replace(
            safety,
            allow_mutating_requests=safety.allow_mutating_requests
            or bool(cli.get("allow_mutating_requests")),
            allow_remote_targets=safety.allow_remote_targets
            or bool(cli.get("allow_remote_targets")),
        )
        merged = replace(merged, safety=safety)

    if cli.get("old_target") is not None:
        merged = replace(merged, old_target=cli["old_target"])
    if cli.get("new_target") is not None:
        merged = replace(merged, new_target=cli["new_target"])
    if cli.get("redaction_headers"):
        merged = replace(
            merged,
            redaction=replace(
                merged.redaction,
                sensitive_headers=tuple(
                    dict.fromkeys(
                        [
                            *merged.redaction.sensitive_headers,
                            *(h.lower() for h in cli["redaction_headers"]),
                        ]
                    )
                ),
            ),
        )
    return merged


def config_fingerprint(config: AppConfig) -> str:
    """Stable, credential-free hash of effective settings for report metadata."""
    payload = {
        "timeout_seconds": config.timeout_seconds,
        "follow_redirects": config.follow_redirects,
        "compare_status": config.compare_status,
        "compare_headers": config.compare_headers,
        "compare_body": config.compare_body,
        "max_differences_per_case": config.max_differences_per_case,
        "response_size_limit_bytes": config.response_size_limit_bytes,
        "timing_mode": config.timing_mode,
        "timing_max_ms": config.timing_max_ms,
        "safety": {
            "allow_mutating_requests": config.safety.allow_mutating_requests,
            "allow_remote_targets": config.safety.allow_remote_targets,
        },
        "normalization": {
            "ignored_headers": list(config.normalization.ignored_headers),
            "ignored_json_pointers": list(config.normalization.ignored_json_pointers),
            "whitespace_text": config.normalization.whitespace_text,
            "array_order": config.normalization.array_order,
        },
        "redaction": {
            "sensitive_headers": list(config.redaction.sensitive_headers),
            "sensitive_json_pointers": list(config.redaction.sensitive_json_pointers),
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _build_target(raw: Any, *, location: str, side: str) -> TargetConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{location} must be a mapping")
    _reject_unknown(raw, _TARGET_KEYS, location=location)
    kind = raw.get("kind")
    if kind is None:
        kind = "http" if raw.get("url") else None
    if kind not in ("http", "wsgi", "asgi"):
        raise ConfigError(f"{location}.kind must be one of 'http', 'wsgi', 'asgi'")
    url = raw.get("url")
    app = raw.get("app")
    if kind == "http":
        if app is not None:
            raise ConfigError(f"{location}: 'app' is not valid for kind 'http' (use 'url')")
        if not isinstance(url, str) or not url:
            raise ConfigError(f"{location}.url is required for kind 'http'")
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"{location}.url must start with http:// or https://")
    else:
        if url is not None:
            raise ConfigError(f"{location}: 'url' is not valid for kind {kind!r} (use 'app')")
        if not isinstance(app, str) or not app:
            raise ConfigError(f"{location}.app is required for kind {kind!r}")
        if not _APP_SPEC_RE.match(app):
            raise ConfigError(
                f"{location}.app must look like 'package.module:attribute', got {app!r}"
            )
    factory = _bool(raw.get("factory", False), f"{location}.factory")
    verify_tls = _bool(raw.get("verify_tls", True), f"{location}.verify_tls")
    timeout = (
        _positive_number(raw["timeout_seconds"], f"{location}.timeout_seconds")
        if "timeout_seconds" in raw
        else None
    )
    if "lifespan" in raw:
        if kind != "asgi":
            raise ConfigError(f"{location}.lifespan only applies to kind 'asgi'")
        lifespan = raw["lifespan"]
        if lifespan not in ("auto", "required", "off"):
            hint = (
                " — quote it: YAML parses unquoted `off`/`on` as booleans"
                if isinstance(lifespan, bool)
                else ""
            )
            raise ConfigError(
                f"{location}.lifespan must be one of 'auto', 'required', 'off', "
                f"got {lifespan!r}{hint}"
            )
    else:
        lifespan = "auto"
    label = raw.get("label", side)
    if not isinstance(label, str) or not label:
        raise ConfigError(f"{location}.label must be a non-empty string")
    return TargetConfig(
        kind=cast("TargetKind", kind),
        url=url,
        app=app,
        factory=factory,
        verify_tls=verify_tls,
        timeout_seconds=timeout,
        lifespan=cast("LifespanMode", lifespan),
        label=label,
    )


def _build_normalization(section: dict[str, Any]) -> NormalizationConfig:
    # Absent key → keep built-in defaults; present key (even empty) replaces them.
    if "ignored_headers" in section:
        ignored_headers = tuple(
            h.lower()
            for h in _str_list(section["ignored_headers"], "normalization.ignored_headers")
        )
    else:
        ignored_headers = NormalizationConfig().ignored_headers
    ignored_pointers = tuple(
        _str_list(section.get("ignored_json_pointers"), "normalization.ignored_json_pointers")
    )
    for pointer in ignored_pointers:
        if not pointer.startswith("/"):
            raise ConfigError(f"JSON Pointer must start with '/': {pointer!r}")
    whitespace_raw = section.get("whitespace", {})
    if isinstance(whitespace_raw, dict):
        _reject_unknown(whitespace_raw, _WHITESPACE_KEYS, location="normalization.whitespace")
        whitespace_text = _bool(whitespace_raw.get("text", False), "normalization.whitespace.text")
    elif isinstance(whitespace_raw, bool):
        whitespace_text = whitespace_raw
    else:
        raise ConfigError("normalization.whitespace must be a mapping")
    array_order_raw = section.get("array_order", {})
    if not isinstance(array_order_raw, dict):
        raise ConfigError("normalization.array_order must be a mapping")
    array_order: dict[str, str] = {}
    for pointer, mode in array_order_raw.items():
        if mode not in ("preserve", "sort"):
            raise ConfigError(
                f"normalization.array_order[{pointer!r}] must be 'preserve' or 'sort'"
            )
        array_order[str(pointer)] = mode
    return NormalizationConfig(
        ignored_headers=ignored_headers,
        ignored_json_pointers=ignored_pointers,
        whitespace_text=whitespace_text,
        array_order=array_order,
    )


def _with_source(config: AppConfig, path: str) -> AppConfig:
    from dataclasses import replace

    return replace(config, source_path=path)


def _section(document: dict[str, Any], key: str, allowed: set[str]) -> dict[str, Any]:
    raw = document.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"'{key}' must be a mapping")
    _reject_unknown(raw, allowed, location=key)
    return raw


def _reject_unknown(mapping: Any, allowed: set[str], *, location: str) -> None:
    if not isinstance(mapping, dict):
        raise ConfigError(f"{location} must be a mapping")
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ConfigError(
            f"unknown field(s) in {location}: {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )


def _bool(value: Any, location: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{location} must be a boolean")
    return value


def _positive_number(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{location} must be a positive number")
    return float(value)


def _positive_int(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{location} must be a positive integer")
    return value


def _str_list(value: Any, location: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{location} must be a list of strings")
    return list(value)
