"""Secret redaction for reports, terminal output, and exception messages.

Comparison runs on *raw* values so that differences are still detected; redaction
is applied when values are handed to display/report layers. The placeholder for
all masked values is :data:`REDACTED`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from apiparity.models import RedactionConfig

__all__ = [
    "REDACTED",
    "escape_xml",
    "is_sensitive_header",
    "is_sensitive_pointer",
    "is_sensitive_query_key",
    "redact_headers",
    "redact_json",
    "redact_url",
    "sanitize_exception_message",
]

REDACTED = "[REDACTED]"

_SENSITIVE_QUERY_SUBSTRINGS = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "apikey",
    "api_key",
    "api-key",
    "credential",
    "authorization",
    "signature",
)

_USERINFO_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<userinfo>[^/@\s]+)@")
_QUERY_RE = re.compile(r"(?P<prefix>(?:^|[?&])(?P<key>[^=&#\s]+)=)(?P<value>[^&#\s]*)")


def is_sensitive_header(name: str, config: RedactionConfig) -> bool:
    lowered = name.lower()
    if lowered in config.sensitive_headers:
        return True
    return any(token and token in lowered for token in config.header_name_substrings)


def is_sensitive_pointer(pointer: str, config: RedactionConfig) -> bool:
    return pointer in config.sensitive_json_pointers


def is_sensitive_query_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in _SENSITIVE_QUERY_SUBSTRINGS)


def redact_headers(
    headers: Mapping[str, Any] | Iterable[tuple[str, Any]],
    config: RedactionConfig,
) -> dict[str, Any]:
    """Return a copy of ``headers`` with sensitive values masked (preserving multi-values)."""
    items = headers.items() if isinstance(headers, Mapping) else headers
    out: dict[str, Any] = {}
    for name, value in items:
        if is_sensitive_header(str(name), config):
            out[name] = (
                REDACTED if not isinstance(value, (list, tuple)) else [REDACTED for _ in value]
            )
        else:
            out[name] = value
    return out


def redact_json(value: Any, config: RedactionConfig, *, pointer: str = "") -> Any:
    """Return a copy of a JSON structure with sensitive pointers masked.

    Matching is *exact per node*: a node whose full JSON Pointer is listed in
    ``sensitive_json_pointers`` is replaced by ``[REDACTED]`` (subtree included).
    """
    if pointer and is_sensitive_pointer(pointer, config):
        return REDACTED
    if isinstance(value, dict):
        return {
            key: redact_json(item, config, pointer=f"{pointer}/{_escape_token(str(key))}")
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            redact_json(item, config, pointer=f"{pointer}/{index}")
            for index, item in enumerate(value)
        ]
    return value


def redact_url(url: str) -> str:
    """Mask userinfo credentials and sensitive query-parameter values in a URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return _USERINFO_RE.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", url)

    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = host  # userinfo deliberately dropped

    def mask_query(match: re.Match[str]) -> str:
        key = match.group("key")
        value = REDACTED if is_sensitive_query_key(key) else match.group("value")
        return f"{match.group('prefix')}{value}"

    query = _QUERY_RE.sub(mask_query, parts.query)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def sanitize_exception_message(message: str, *, limit: int = 500) -> str:
    """Strip credentials from an exception message before display or persistence."""
    text = _USERINFO_RE.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", message)

    def mask_query(match: re.Match[str]) -> str:
        key = match.group("key")
        value = REDACTED if is_sensitive_query_key(key) else match.group("value")
        return f"{match.group('prefix')}{value}"

    text = _QUERY_RE.sub(mask_query, text)
    text = text.replace("\n", " ").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def escape_xml(text: str) -> str:
    """XML-escape for report attributes and text nodes (also strips control chars)."""
    cleaned = "".join(ch for ch in text if ch in "\t\n\r" or (ord(ch) >= 32 and ord(ch) != 127))
    return (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def dumps_redacted(value: Any, config: RedactionConfig, *, indent: int | None = None) -> str:
    """Serialize a JSON structure with redaction applied (used by report previews)."""
    return json.dumps(redact_json(value, config), indent=indent, ensure_ascii=False, default=str)
