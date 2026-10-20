"""Deterministic response comparison.

Produces structured :class:`~apiparity.models.Difference` objects from two
normalized :class:`~apiparity.models.ResponseSnapshot` values. Rules:

* transport/timeout failures are *infrastructure* observations, never proof of
  behavioral differences — the runner classifies those cases as ``failed``;
* JSON is compared structurally (RFC 6901 pointers, object order ignored, array
  order preserved unless configured otherwise, ``null`` ≠ missing);
* sensitive paths/headers are still *detected* when they differ, but their
  values are replaced with ``[REDACTED]`` before a Difference leaves this module;
* comparison is bounded: ``max_differences_per_case`` caps output so one huge
  mismatch cannot produce an unbounded report.

No LLM or non-deterministic logic may ever be introduced here.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from apiparity.models import (
    ComparisonConfig,
    Difference,
    DifferenceCategory,
    NormalizationConfig,
    RedactionConfig,
    RequestCase,
    ResponseSnapshot,
)
from apiparity.normalization import array_sort_key, is_ignored_pointer, normalize_text
from apiparity.redaction import REDACTED, is_sensitive_header, is_sensitive_pointer

__all__ = ["MISSING", "compare_snapshots"]

#: Display sentinel for a key that is absent (distinct from JSON ``null``).
MISSING = "(missing)"

_PREVIEW_LIMIT = 300
_CHARSET_RE = re.compile(r"charset=([\w\-.]+)", re.IGNORECASE)


class _Collector:
    """Bounded difference sink (adds an explicit truncation note when capped)."""

    def __init__(self, maximum: int, case_id: str) -> None:
        self.maximum = max(1, maximum)
        self.case_id = case_id
        self.items: list[Difference] = []
        self.truncated = False

    @property
    def full(self) -> bool:
        return len(self.items) >= self.maximum

    def add(self, difference: Difference) -> None:
        if self.full:
            self.truncated = True
            return
        self.items.append(difference)

    def finish(self) -> list[Difference]:
        if self.truncated:
            self.items.append(
                Difference(
                    category="normalization_note",
                    severity="info",
                    case_id=self.case_id,
                    message=(
                        f"difference limit reached: showing first {self.maximum} "
                        "(comparison.max_differences_per_case)"
                    ),
                )
            )
        return self.items


def compare_snapshots(
    baseline: ResponseSnapshot,
    candidate: ResponseSnapshot,
    *,
    case: RequestCase,
    comparison: ComparisonConfig,
    normalization: NormalizationConfig,
    redaction: RedactionConfig,
) -> list[Difference]:
    """Compare one case's two observations and return structured differences."""
    collector = _Collector(comparison.max_differences_per_case, case.id)

    infra = _infra_differences(baseline, candidate, case.id)
    if infra:
        for difference in infra:
            collector.add(difference)
        return collector.finish()

    if comparison.compare_status:
        _diff_status(baseline, candidate, case.id, collector)

    do_headers = (
        comparison.compare_headers if case.compare_headers is None else case.compare_headers
    )
    if do_headers:
        _diff_headers(baseline, candidate, case.id, redaction, collector)

    do_body = comparison.compare_body if case.compare_body is None else case.compare_body
    if do_body:
        if baseline.body_truncated or candidate.body_truncated:
            collector.add(
                Difference(
                    category="response_too_large",
                    severity="error",
                    case_id=case.id,
                    message=(
                        "response exceeds comparison.response_size_limit_bytes; "
                        "full comparison was impossible, so this case cannot pass"
                    ),
                    baseline_value={"bytes_compared": _body_len(baseline)},
                    candidate_value={"bytes_compared": _body_len(candidate)},
                )
            )
        _diff_body(baseline, candidate, case.id, normalization, redaction, collector)

    _diff_timing(baseline, candidate, case.id, comparison, collector)

    return collector.finish()


# --------------------------------------------------------------------------- #
# Infrastructure failures
# --------------------------------------------------------------------------- #


def _infra_differences(
    baseline: ResponseSnapshot,
    candidate: ResponseSnapshot,
    case_id: str,
) -> list[Difference]:
    differences: list[Difference] = []
    for side, snapshot in (("baseline", baseline), ("candidate", candidate)):
        if snapshot.error_kind in ("transport", "timeout"):
            if snapshot.error_kind == "timeout":
                category: DifferenceCategory = "timeout"
            else:
                category = "transport_error"
            detail = snapshot.transport_error or "no detail"
            differences.append(
                Difference(
                    category=category,
                    severity="error",
                    case_id=case_id,
                    message=(
                        f"{side} request failed ({snapshot.error_kind}): {detail} "
                        "— infrastructure failure, not a behavioral difference"
                    ),
                    details={"side": side},
                )
            )
    return differences


def _body_len(snapshot: ResponseSnapshot) -> int:
    return len(snapshot.body_bytes) if snapshot.body_bytes is not None else 0


# --------------------------------------------------------------------------- #
# Status / headers / timing
# --------------------------------------------------------------------------- #


def _diff_status(
    baseline: ResponseSnapshot,
    candidate: ResponseSnapshot,
    case_id: str,
    collector: _Collector,
) -> None:
    if baseline.status_code is None or candidate.status_code is None:
        return
    if baseline.status_code != candidate.status_code:
        collector.add(
            Difference(
                category="status_code",
                severity="error",
                case_id=case_id,
                path=None,
                message="HTTP status code differs",
                baseline_value=baseline.status_code,
                candidate_value=candidate.status_code,
            )
        )


def _diff_headers(
    baseline: ResponseSnapshot,
    candidate: ResponseSnapshot,
    case_id: str,
    redaction: RedactionConfig,
    collector: _Collector,
) -> None:
    base_map = baseline.header_map()
    cand_map = candidate.header_map()

    for name, base_values in base_map.items():
        sensitive = is_sensitive_header(name, redaction)
        if name not in cand_map:
            collector.add(
                Difference(
                    category="header_removed",
                    severity="error",
                    case_id=case_id,
                    path=f"header:{name}",
                    message=f"header {name!r} present in baseline, missing in candidate",
                    baseline_value=REDACTED if sensitive else list(base_values),
                    candidate_value=MISSING,
                )
            )
        elif base_values != cand_map[name]:
            same_after_redaction = sensitive
            collector.add(
                Difference(
                    category="header_value",
                    severity="error",
                    case_id=case_id,
                    path=f"header:{name}",
                    message=(
                        "Sensitive field differs"
                        if same_after_redaction
                        else f"header {name!r} value differs"
                    ),
                    baseline_value=REDACTED if sensitive else list(base_values),
                    candidate_value=REDACTED if sensitive else list(cand_map[name]),
                )
            )

    for name, cand_values in cand_map.items():
        if name in base_map:
            continue
        sensitive = is_sensitive_header(name, redaction)
        collector.add(
            Difference(
                category="header_added",
                severity="error",
                case_id=case_id,
                path=f"header:{name}",
                message=f"header {name!r} present in candidate, missing in baseline",
                baseline_value=MISSING,
                candidate_value=REDACTED if sensitive else list(cand_values),
            )
        )


def _diff_timing(
    baseline: ResponseSnapshot,
    candidate: ResponseSnapshot,
    case_id: str,
    comparison: ComparisonConfig,
    collector: _Collector,
) -> None:
    if comparison.timing_mode == "off" or comparison.timing_max_ms is None:
        return
    if candidate.duration_ms <= comparison.timing_max_ms:
        return
    collector.add(
        Difference(
            category="timing",
            severity="error" if comparison.timing_mode == "fail" else "warning",
            case_id=case_id,
            path=None,
            message=(
                f"candidate took {candidate.duration_ms:.1f} ms, exceeding the "
                f"{comparison.timing_max_ms:g} ms threshold (mode={comparison.timing_mode})"
            ),
            baseline_value=baseline.duration_ms,
            candidate_value=candidate.duration_ms,
        )
    )


# --------------------------------------------------------------------------- #
# Bodies
# --------------------------------------------------------------------------- #


def _diff_body(
    baseline: ResponseSnapshot,
    candidate: ResponseSnapshot,
    case_id: str,
    normalization: NormalizationConfig,
    redaction: RedactionConfig,
    collector: _Collector,
) -> None:
    base_body, cand_body = baseline.body_bytes, candidate.body_bytes

    if base_body is None and cand_body is None:
        return
    if base_body is None or cand_body is None:
        collector.add(
            Difference(
                category="body_type",
                severity="error",
                case_id=case_id,
                path=None,
                message="body present in only one response",
                baseline_value=_body_presence(baseline),
                candidate_value=_body_presence(candidate),
            )
        )
        return

    base_json, base_parsed = _try_json(base_body, baseline.content_type)
    cand_json, cand_parsed = _try_json(cand_body, candidate.content_type)

    if base_parsed and cand_parsed:
        _diff_json(
            base_json,
            cand_json,
            "",
            case_id,
            normalization,
            redaction,
            collector,
        )
        return

    if base_parsed != cand_parsed:
        which = "baseline" if base_parsed else "candidate"
        collector.add(
            Difference(
                category="body_type",
                severity="error",
                case_id=case_id,
                path=None,
                message=f"body parseability differs: {which} body is JSON, the other is not",
                baseline_value={"json": base_parsed, "content_type": baseline.content_type},
                candidate_value={"json": cand_parsed, "content_type": candidate.content_type},
            )
        )
        return

    base_text, base_is_text = _try_text(base_body, baseline.content_type)
    cand_text, cand_is_text = _try_text(cand_body, candidate.content_type)

    if base_is_text and cand_is_text:
        norm_a = normalize_text(base_text, normalization)
        norm_b = normalize_text(cand_text, normalization)
        if norm_a != norm_b:
            collector.add(
                Difference(
                    category="text_body",
                    severity="error",
                    case_id=case_id,
                    path=None,
                    message="text body differs",
                    baseline_value=_digest_payload(base_text.encode("utf-8")),
                    candidate_value=_digest_payload(cand_text.encode("utf-8")),
                )
            )
        return

    if base_body != cand_body:
        collector.add(
            Difference(
                category="binary_body",
                severity="error",
                case_id=case_id,
                path=None,
                message="binary body differs",
                baseline_value=_digest_payload(base_body),
                candidate_value=_digest_payload(cand_body),
            )
        )


def _body_presence(snapshot: ResponseSnapshot) -> str:
    return "present" if snapshot.body_bytes is not None else "absent"


def _digest_payload(body: bytes) -> dict[str, Any]:
    return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


def _decode(body: bytes, content_type: str | None) -> str | None:
    charset = "utf-8"
    if content_type:
        match = _CHARSET_RE.search(content_type)
        if match:
            charset = match.group(1)
    try:
        return body.decode(charset)
    except (LookupError, UnicodeDecodeError):
        return None


def _try_json(body: bytes, content_type: str | None) -> tuple[Any, bool]:
    text = _decode(body, content_type)
    if text is None:
        return None, False
    try:
        return json.loads(text), True
    except ValueError:
        return None, False


def _try_text(body: bytes, content_type: str | None) -> tuple[str, bool]:
    text = _decode(body, content_type)
    if text is None:
        return "", False
    return text, True


# --------------------------------------------------------------------------- #
# Structural JSON diff
# --------------------------------------------------------------------------- #


def _diff_json(
    base_value: Any,
    cand_value: Any,
    path: str,
    case_id: str,
    normalization: NormalizationConfig,
    redaction: RedactionConfig,
    collector: _Collector,
) -> None:
    if path and is_ignored_pointer(path, normalization.ignored_json_pointers):
        return
    if collector.full:
        return

    if isinstance(base_value, list) and isinstance(cand_value, list):
        mode = normalization.array_order.get(path)
        if mode == "sort":
            base_value = sorted(base_value, key=array_sort_key)
            cand_value = sorted(cand_value, key=array_sort_key)
        shared = min(len(base_value), len(cand_value))
        for index in range(shared):
            _diff_json(
                base_value[index],
                cand_value[index],
                f"{path}/{index}",
                case_id,
                normalization,
                redaction,
                collector,
            )
        if len(base_value) != len(cand_value):
            base_p, cand_p, sensitive = _sensitive_values(
                path, len(base_value), len(cand_value), redaction
            )
            collector.add(
                Difference(
                    category="json_value",
                    severity="error",
                    case_id=case_id,
                    path=path or "/",
                    message="array length differs",
                    baseline_value=base_p,
                    candidate_value=cand_p,
                    details={"sensitive": sensitive} if sensitive else None,
                )
            )
        return

    if isinstance(base_value, dict) and isinstance(cand_value, dict):
        base_keys = set(base_value)
        cand_keys = set(cand_value)
        for key in sorted(base_keys - cand_keys):
            child = _child_path(path, str(key))
            if is_ignored_pointer(child, normalization.ignored_json_pointers):
                continue
            base_p, _cand_p, sensitive = _sensitive_values(
                child, _preview(base_value[key]), MISSING, redaction
            )
            collector.add(
                Difference(
                    category="json_key_removed",
                    severity="error",
                    case_id=case_id,
                    path=child,
                    message=(
                        "Sensitive field differs (key present in baseline, missing in candidate)"
                        if sensitive
                        else "key present in baseline, missing in candidate"
                    ),
                    baseline_value=base_p,
                    candidate_value=MISSING,
                )
            )
        for key in sorted(cand_keys - base_keys):
            child = _child_path(path, str(key))
            if is_ignored_pointer(child, normalization.ignored_json_pointers):
                continue
            _base_p, cand_p, sensitive = _sensitive_values(
                child, MISSING, _preview(cand_value[key]), redaction
            )
            collector.add(
                Difference(
                    category="json_key_added",
                    severity="error",
                    case_id=case_id,
                    path=child,
                    message=(
                        "Sensitive field differs (key missing in baseline, present in candidate)"
                        if sensitive
                        else "key missing in baseline, present in candidate"
                    ),
                    baseline_value=MISSING,
                    candidate_value=cand_p,
                )
            )
        for key in sorted(base_keys & cand_keys, key=str):
            _diff_json(
                base_value[key],
                cand_value[key],
                _child_path(path, str(key)),
                case_id,
                normalization,
                redaction,
                collector,
            )
        return

    if type(base_value) is not type(cand_value):
        base_p, cand_p, sensitive = _sensitive_values(
            path, _preview(base_value), _preview(cand_value), redaction
        )
        collector.add(
            Difference(
                category="json_type",
                severity="error",
                case_id=case_id,
                path=path or "/",
                message="Sensitive field differs" if sensitive else "value type differs",
                baseline_value=base_p,
                candidate_value=cand_p,
                details={
                    "baseline_type": _json_type_name(base_value),
                    "candidate_type": _json_type_name(cand_value),
                },
            )
        )
        return

    if base_value != cand_value:
        base_p, cand_p, sensitive = _sensitive_values(
            path, _preview(base_value), _preview(cand_value), redaction
        )
        collector.add(
            Difference(
                category="json_value",
                severity="error",
                case_id=case_id,
                path=path or "/",
                message="Sensitive field differs" if sensitive else "value differs",
                baseline_value=base_p,
                candidate_value=cand_p,
            )
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _child_path(path: str, token: str) -> str:
    escaped = token.replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}"


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _sensitive_values(
    path: str,
    baseline_value: Any,
    candidate_value: Any,
    redaction: RedactionConfig,
) -> tuple[Any, Any, bool]:
    if path and is_sensitive_pointer(path, redaction):
        return REDACTED, REDACTED, True
    return baseline_value, candidate_value, False


def _preview(value: Any) -> Any:
    """Bound the size of values embedded in Difference objects/reports."""
    if isinstance(value, str):
        return value if len(value) <= _PREVIEW_LIMIT else value[:_PREVIEW_LIMIT] + "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover — default=str covers this
        text = str(value)
    return text if len(text) <= _PREVIEW_LIMIT else text[:_PREVIEW_LIMIT] + "…"
