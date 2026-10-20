"""Request-corpus loading and strict validation (YAML and JSON).

Corpus contract (see ``REQUIREMENTS.md`` §7):

* top-level keys: ``version`` (required, ``1``), ``defaults``, ``cases``,
  ``normalization`` — anything else is rejected;
* every case is validated field by field; duplicate ids and conflicting body
  representations are rejected;
* ``${VAR}`` placeholders are resolved *after* structural validation and
  *before* any request could run;
* file-upload paths must stay inside the corpus directory (no traversal).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from apiparity.exceptions import CorpusError
from apiparity.models import (
    KNOWN_METHODS,
    FileUpload,
    NormalizationConfig,
    RequestCase,
)
from apiparity.variables import resolve_variables

__all__ = ["Corpus", "load_corpus", "load_corpus_text"]

_TOP_LEVEL_KEYS = {"version", "defaults", "cases", "normalization"}
_DEFAULT_KEYS = {
    "timeout_seconds",
    "follow_redirects",
    "compare_headers",
    "compare_body",
    "compare_status",
    "max_differences_per_case",
    "response_size_limit_bytes",
}
_CASE_KEYS = {
    "id",
    "description",
    "method",
    "path",
    "query",
    "headers",
    "json",
    "data",
    "files",
    "timeout_seconds",
    "enabled",
    "compare_headers",
    "compare_body",
}
_NORMALIZATION_KEYS = {
    "ignored_headers",
    "ignored_json_pointers",
    "whitespace",
    "array_order",
    "timing",
}
_TIMING_KEYS = {"mode", "max_ms"}
_FILE_KEYS = {"field", "path", "content_type"}

_MUTUALLY_EXCLUSIVE_BODIES = ("json", "data", "files")


@dataclass(frozen=True)
class Corpus:
    """A validated, variable-resolved request corpus."""

    version: int
    defaults: dict[str, Any]
    cases: tuple[RequestCase, ...]
    normalization: NormalizationConfig
    source_path: Path
    directory: Path

    @property
    def enabled_cases(self) -> tuple[RequestCase, ...]:
        return tuple(case for case in self.cases if case.enabled)


def load_corpus(
    path: str | Path,
    *,
    env: Mapping[str, str] | None = None,
) -> Corpus:
    """Load and validate a YAML or JSON corpus from disk."""
    corpus_path = Path(path)
    if not corpus_path.is_file():
        raise CorpusError(f"corpus file not found: {corpus_path}", location=str(corpus_path))
    try:
        text = corpus_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorpusError(f"cannot read corpus file: {exc}", location=str(corpus_path)) from exc
    return load_corpus_text(text, source_path=corpus_path, env=env)


def load_corpus_text(
    text: str,
    *,
    source_path: Path,
    env: Mapping[str, str] | None = None,
) -> Corpus:
    """Parse and validate corpus content (format detected by file suffix)."""
    document = _parse_document(text, source_path)
    if not isinstance(document, dict):
        raise CorpusError("corpus root must be a mapping/object", location=str(source_path))
    return build_corpus(document, source_path=source_path, env=env)


def build_corpus(
    document: dict[str, Any],
    *,
    source_path: Path,
    env: Mapping[str, str] | None = None,
) -> Corpus:
    """Validate an already-parsed corpus document. Exposed for unit tests."""
    _reject_unknown(document, _TOP_LEVEL_KEYS, location="<root>")

    version = document.get("version")
    if version != 1:
        raise CorpusError("unsupported or missing 'version' (expected 1)", location="<root>")

    defaults = _validate_defaults(document.get("defaults", {}))
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CorpusError("'cases' must be a non-empty list", location="<root>")

    normalization_doc = document.get("normalization", {})
    if not isinstance(normalization_doc, dict):
        raise CorpusError("'normalization' must be a mapping", location="normalization")
    normalization, extra_defaults = _validate_normalization(normalization_doc)
    defaults.update(extra_defaults)

    # Structural validation first, then secret resolution, then env-sensitive checks.
    built_cases: list[RequestCase] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        location = f"cases[{index}]"
        if not isinstance(raw_case, dict):
            raise CorpusError("each case must be a mapping", location=location)
        case_id = raw_case.get("id")
        label = f"{location} (id={case_id!r})" if isinstance(case_id, str) else location
        _reject_unknown(raw_case, _CASE_KEYS, location=label)
        case = _build_case(raw_case, location=label, defaults=defaults)
        if case.id in seen_ids:
            raise CorpusError(f"duplicate case id {case.id!r}", location=label)
        seen_ids.add(case.id)
        built_cases.append(case)

    resolved_cases = tuple(
        _resolve_case(case, env=env, source_path=source_path) for case in built_cases
    )
    return Corpus(
        version=1,
        defaults=defaults,
        cases=resolved_cases,
        normalization=normalization,
        source_path=source_path,
        directory=source_path.parent,
    )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _parse_document(text: str, source_path: Path) -> Any:
    suffix = source_path.suffix.lower()
    try:
        if suffix == ".json":
            return json.loads(text)
        if suffix in {".yaml", ".yml"}:
            return yaml.safe_load(text)
        # Unknown suffix: try YAML first (it is a JSON superset for our needs).
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError:
            return json.loads(text)
    except yaml.YAMLError as exc:
        raise CorpusError(f"invalid YAML: {_short(exc)}", location=str(source_path)) from exc
    except json.JSONDecodeError as exc:
        raise CorpusError(
            f"invalid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}",
            location=str(source_path),
        ) from exc


def _short(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    return text[:200]


# --------------------------------------------------------------------------- #
# Section validators
# --------------------------------------------------------------------------- #


def _validate_defaults(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CorpusError("'defaults' must be a mapping", location="defaults")
    _reject_unknown(raw, _DEFAULT_KEYS, location="defaults")
    out: dict[str, Any] = {}
    if "timeout_seconds" in raw:
        out["timeout_seconds"] = _positive_number(
            raw["timeout_seconds"], "defaults.timeout_seconds"
        )
    for key in ("follow_redirects", "compare_headers", "compare_body", "compare_status"):
        if key in raw:
            out[key] = _require_bool(raw[key], f"defaults.{key}")
    for key in ("max_differences_per_case", "response_size_limit_bytes"):
        if key in raw:
            value = raw[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise CorpusError(f"'{key}' must be a positive integer", location=f"defaults.{key}")
            out[key] = value
    return out


def _validate_normalization(raw: dict[str, Any]) -> tuple[NormalizationConfig, dict[str, Any]]:
    _reject_unknown(raw, _NORMALIZATION_KEYS, location="normalization")
    ignored_headers = _string_list(raw.get("ignored_headers"), "normalization.ignored_headers")
    ignored_pointers = _string_list(
        raw.get("ignored_json_pointers"), "normalization.ignored_json_pointers"
    )
    for pointer in ignored_pointers:
        if not pointer.startswith("/"):
            raise CorpusError(
                f"JSON Pointer must start with '/': {pointer!r}",
                location="normalization.ignored_json_pointers",
            )
    whitespace_raw = raw.get("whitespace", {})
    if isinstance(whitespace_raw, bool):
        whitespace_text = whitespace_raw
    elif isinstance(whitespace_raw, dict):
        _reject_unknown(whitespace_raw, {"text"}, location="normalization.whitespace")
        whitespace_text = _require_bool(
            whitespace_raw.get("text", False), "normalization.whitespace.text"
        )
    else:
        raise CorpusError("'whitespace' must be a mapping", location="normalization.whitespace")

    array_order_raw = raw.get("array_order", {})
    if not isinstance(array_order_raw, dict):
        raise CorpusError("'array_order' must be a mapping", location="normalization.array_order")
    array_order: dict[str, str] = {}
    for pointer, mode in array_order_raw.items():
        if mode not in ("preserve", "sort"):
            raise CorpusError(
                f"array_order mode must be 'preserve' or 'sort', got {mode!r}",
                location=f"normalization.array_order.{pointer}",
            )
        array_order[str(pointer)] = mode

    extra_defaults: dict[str, Any] = {}
    timing_raw = raw.get("timing", {})
    if not isinstance(timing_raw, dict):
        raise CorpusError("'timing' must be a mapping", location="normalization.timing")
    _reject_unknown(timing_raw, _TIMING_KEYS, location="normalization.timing")
    if "mode" in timing_raw:
        mode = timing_raw["mode"]
        if mode not in ("off", "warn", "fail"):
            raise CorpusError(
                "timing.mode must be one of 'off', 'warn', 'fail'",
                location="normalization.timing.mode",
            )
        extra_defaults["timing_mode"] = mode
    if "max_ms" in timing_raw and timing_raw["max_ms"] is not None:
        extra_defaults["timing_max_ms"] = _positive_number(
            timing_raw["max_ms"], "normalization.timing.max_ms"
        )

    return (
        NormalizationConfig(
            ignored_headers=tuple(h.lower() for h in ignored_headers),
            ignored_json_pointers=tuple(ignored_pointers),
            whitespace_text=whitespace_text,
            array_order=array_order,
        ),
        extra_defaults,
    )


def _build_case(raw: dict[str, Any], *, location: str, defaults: dict[str, Any]) -> RequestCase:
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise CorpusError("'id' must be a non-empty string", location=location)

    method = raw.get("method")
    if not isinstance(method, str) or not method.strip():
        raise CorpusError("'method' is required", location=f"{location}.method")
    method = method.strip().upper()
    if method not in KNOWN_METHODS:
        raise CorpusError(
            f"unknown HTTP method {method!r} (expected one of {sorted(KNOWN_METHODS)})",
            location=f"{location}.method",
        )

    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise CorpusError("'path' is required", location=f"{location}.path")
    _validate_path_shape(path, location=f"{location}.path")

    body_keys = [key for key in _MUTUALLY_EXCLUSIVE_BODIES if raw.get(key) is not None]
    if len(body_keys) > 1:
        raise CorpusError(
            f"conflicting body representations: {', '.join(sorted(body_keys))} "
            "(only one of json/data/files is allowed)",
            location=location,
        )

    query = _validate_query(raw.get("query", {}), location=f"{location}.query")
    headers = _validate_headers(raw.get("headers", {}), location=f"{location}.headers")

    json_body = raw.get("json")
    if json_body is not None:
        try:
            json.dumps(json_body)
        except (TypeError, ValueError) as exc:
            raise CorpusError(
                f"'json' body is not serializable: {exc}", location=f"{location}.json"
            ) from exc

    data = raw.get("data")
    if data is not None and not isinstance(data, str):
        raise CorpusError("'data' must be a string", location=f"{location}.data")

    files = _validate_files(raw.get("files"), location=f"{location}.files")

    timeout: float | None = None
    if raw.get("timeout_seconds") is not None:
        timeout = _positive_number(raw["timeout_seconds"], f"{location}.timeout_seconds")

    enabled = raw.get("enabled", True)
    enabled = _require_bool(enabled, f"{location}.enabled")

    compare_headers = raw.get("compare_headers")
    if compare_headers is not None:
        compare_headers = _require_bool(compare_headers, f"{location}.compare_headers")
    compare_body = raw.get("compare_body")
    if compare_body is not None:
        compare_body = _require_bool(compare_body, f"{location}.compare_body")

    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        raise CorpusError("'description' must be a string", location=f"{location}.description")

    del defaults  # corpus defaults are applied by the runner, not per-case construction
    return RequestCase(
        id=case_id,
        method=method,
        path=path,
        description=description,
        query=query,
        headers=headers,
        json_body=json_body,
        data=data,
        files=files,
        timeout_seconds=timeout,
        enabled=enabled,
        compare_headers=compare_headers,
        compare_body=compare_body,
    )


def _resolve_case(
    case: RequestCase, *, env: Mapping[str, str] | None, source_path: Path
) -> RequestCase:
    """Resolve `${VAR}` placeholders, then re-check env-sensitive constraints."""
    payload = {
        "path": case.path,
        "query": case.query,
        "headers": case.headers,
        "json": case.json_body,
        "data": case.data,
    }
    resolved = resolve_variables(payload, env, location=f"case {case.id!r}")
    path = resolved["path"]
    _validate_path_shape(path, location=f"case {case.id!r}.path")
    query = _validate_query(resolved["query"], location=f"case {case.id!r}.query")
    headers = _validate_headers(resolved["headers"], location=f"case {case.id!r}.headers")

    files = case.files
    if files:
        files = tuple(
            _check_upload_path(upload, corpus_dir=source_path.parent, location=f"case {case.id!r}")
            for upload in files
        )

    return RequestCase(
        id=case.id,
        method=case.method,
        path=path,
        description=case.description,
        query=query,
        headers=headers,
        json_body=resolved["json"],
        data=resolved["data"],
        files=files,
        timeout_seconds=case.timeout_seconds,
        enabled=case.enabled,
        compare_headers=case.compare_headers,
        compare_body=case.compare_body,
    )


# --------------------------------------------------------------------------- #
# Field helpers
# --------------------------------------------------------------------------- #


def _validate_path_shape(path: str, *, location: str) -> None:
    if not path.startswith("/"):
        raise CorpusError(f"path must start with '/': {path!r}", location=location)
    if "://" in path:
        raise CorpusError(
            f"path must be relative, not an absolute URL: {path!r}", location=location
        )
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in path):
        raise CorpusError(
            "path must not contain whitespace or control characters", location=location
        )


def _validate_query(raw: Any, *, location: str) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise CorpusError("'query' must be a mapping", location=location)
    query: dict[str, list[str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise CorpusError("query keys must be non-empty strings", location=location)
        items = value if isinstance(value, list) else [value]
        values: list[str] = []
        for item in items:
            if item is None or isinstance(item, (dict, list)):
                raise CorpusError(
                    f"query value for {key!r} must be a scalar or list of scalars",
                    location=f"{location}.{key}",
                )
            values.append(_scalar_to_str(item, location=f"{location}.{key}"))
        query[key] = values
    return query


def _validate_headers(raw: Any, *, location: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise CorpusError("'headers' must be a mapping", location=location)
    headers: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not _is_token(key):
            raise CorpusError(f"invalid header name: {key!r}", location=location)
        headers[key] = _scalar_to_str(value, location=f"{location}.{key}")
    return headers


def _validate_files(raw: Any, *, location: str) -> tuple[FileUpload, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise CorpusError("'files' must be a non-empty list of uploads", location=location)
    uploads: list[FileUpload] = []
    for index, item in enumerate(raw):
        item_location = f"{location}[{index}]"
        if not isinstance(item, dict):
            raise CorpusError("each file entry must be a mapping", location=item_location)
        _reject_unknown(item, _FILE_KEYS, location=item_location)
        field = item.get("field")
        path = item.get("path")
        if not isinstance(field, str) or not field:
            raise CorpusError("'field' is required", location=item_location)
        if not isinstance(path, str) or not path:
            raise CorpusError("'path' is required", location=item_location)
        content_type = item.get("content_type")
        if content_type is not None and not isinstance(content_type, str):
            raise CorpusError("'content_type' must be a string", location=item_location)
        uploads.append(FileUpload(field=field, path=path, content_type=content_type))
    return tuple(uploads)


def _check_upload_path(upload: FileUpload, *, corpus_dir: Path, location: str) -> FileUpload:
    """Reject absolute or escaping upload paths; require the file to exist."""
    raw_path = Path(upload.path)
    if raw_path.is_absolute() or upload.path.startswith(("~", "\\")) or ":" in upload.path[:3]:
        raise CorpusError(
            f"file path must be relative to the corpus directory: {upload.path!r}",
            location=location,
        )
    root = corpus_dir.resolve()
    candidate = (root / raw_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise CorpusError(
            f"file path escapes the corpus directory: {upload.path!r}",
            location=location,
        )
    if not candidate.is_file():
        raise CorpusError(f"file not found: {upload.path!r}", location=location)
    return upload


def _scalar_to_str(value: Any, *, location: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise CorpusError(f"expected a scalar value, got {type(value).__name__}", location=location)


def _is_token(text: str) -> bool:
    if not text:
        return False
    separators = set('()<>@,;:\\"/[]?={} \t')
    return all(ch.isprintable() and ch not in separators for ch in text)


def _positive_number(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CorpusError("must be a positive number", location=location)
    if value <= 0:
        raise CorpusError("must be a positive number", location=location)
    return float(value)


def _require_bool(value: Any, location: str) -> bool:
    if not isinstance(value, bool):
        raise CorpusError("must be a boolean", location=location)
    return value


def _string_list(value: Any, location: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CorpusError("must be a list of strings", location=location)
    return list(value)


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], *, location: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise CorpusError(
            f"unknown field(s): {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})",
            location=location,
        )
