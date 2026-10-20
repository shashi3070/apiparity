"""Response normalization rules.

Normalization is *read-only*: it never mutates the application's response or any
application state. It only produces a derived snapshot (ignored headers removed)
plus predicates the comparison stage consults (JSON Pointer exclusions,
whitespace policy, array-order policy). Every rule is surfaced in run metadata.
"""

from __future__ import annotations

import json
from typing import Any

from apiparity.models import NormalizationConfig, ResponseSnapshot

__all__ = [
    "array_sort_key",
    "describe_rules",
    "filter_headers",
    "is_ignored_pointer",
    "normalize_snapshot",
    "normalize_text",
]


def filter_headers(
    headers: tuple[tuple[str, tuple[str, ...]], ...],
    ignored: tuple[str, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Drop configured ignored headers (names are already lower-cased)."""
    ignored_set = {name.lower() for name in ignored}
    return tuple((name, values) for name, values in headers if name.lower() not in ignored_set)


def normalize_snapshot(snapshot: ResponseSnapshot, rules: NormalizationConfig) -> ResponseSnapshot:
    """Return a derived snapshot with ignored headers removed (never mutates input)."""
    if not rules.ignored_headers:
        return snapshot
    filtered = filter_headers(snapshot.headers, rules.ignored_headers)
    if filtered == snapshot.headers:
        return snapshot
    return ResponseSnapshot(
        status_code=snapshot.status_code,
        headers=filtered,
        content_type=snapshot.content_type,
        body_bytes=snapshot.body_bytes,
        body_truncated=snapshot.body_truncated,
        body_size=snapshot.body_size,
        duration_ms=snapshot.duration_ms,
        transport_error=snapshot.transport_error,
        error_kind=snapshot.error_kind,
    )


def is_ignored_pointer(pointer: str, ignored: tuple[str, ...]) -> bool:
    """True when ``pointer`` equals or is nested under an ignored JSON Pointer.

    Subtree semantics: ignoring ``/metadata`` also ignores ``/metadata/request_id``.
    """
    return any(pointer == candidate or pointer.startswith(candidate + "/") for candidate in ignored)


def normalize_text(text: str, rules: NormalizationConfig) -> str:
    """Apply the configured whitespace policy (off by default — no silent reformatting)."""
    if not rules.whitespace_text:
        return text
    return " ".join(text.split())


def array_sort_key(value: Any) -> str:
    """Deterministic ordering for ``array_order: sort`` policies."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def describe_rules(rules: NormalizationConfig) -> dict[str, Any]:
    """Machine-readable description of applied rules for run/report metadata."""
    return {
        "ignored_headers": list(rules.ignored_headers),
        "ignored_json_pointers": list(rules.ignored_json_pointers),
        "whitespace_text": rules.whitespace_text,
        "array_order": dict(rules.array_order),
    }
