"""Unit tests for the deterministic comparison engine (apiparity.comparison)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from apiparity.comparison import MISSING, compare_snapshots
from apiparity.models import (
    ComparisonConfig,
    ErrorKind,
    NormalizationConfig,
    RedactionConfig,
    RequestCase,
    ResponseSnapshot,
)
from apiparity.redaction import REDACTED


def snap(
    *,
    status: int | None = 200,
    headers: tuple[tuple[str, tuple[str, ...]], ...] = (),
    content_type: str | None = "application/json",
    body: bytes | None = b"",
    truncated: bool = False,
    duration: float = 1.0,
    error_kind: ErrorKind = "none",
    transport_error: str | None = None,
) -> ResponseSnapshot:
    return ResponseSnapshot(
        status_code=status,
        headers=headers,
        content_type=content_type,
        body_bytes=body,
        body_truncated=truncated,
        body_size=len(body) if body is not None else 0,
        duration_ms=duration,
        transport_error=transport_error,
        error_kind=error_kind,
    )


def jbody(value: Any) -> bytes:
    return json.dumps(value).encode("utf-8")


def diff(
    baseline: ResponseSnapshot,
    candidate: ResponseSnapshot,
    *,
    comparison: ComparisonConfig | None = None,
    normalization: NormalizationConfig | None = None,
    redaction: RedactionConfig | None = None,
    case: RequestCase | None = None,
):
    return compare_snapshots(
        baseline,
        candidate,
        case=case or RequestCase(id="case-1", method="GET", path="/x"),
        comparison=comparison or ComparisonConfig(),
        normalization=normalization or NormalizationConfig(),
        redaction=redaction or RedactionConfig(),
    )


class TestIdenticalResponses:
    def test_identical_json_no_differences(self) -> None:
        body = jbody({"a": 1, "b": [1, 2]})
        assert diff(snap(body=body), snap(body=body)) == []

    def test_identical_empty_bodies(self) -> None:
        assert diff(snap(body=None), snap(body=None)) == []

    def test_identical_status_and_headers(self) -> None:
        headers = (("content-type", ("application/json",)), ("x-request-id", ("abc",)))
        body = jbody({"ok": True})
        assert diff(snap(headers=headers, body=body), snap(headers=headers, body=body)) == []


class TestStatusComparison:
    def test_status_differs(self) -> None:
        differences = diff(snap(status=200), snap(status=404))
        assert len(differences) == 1
        (only,) = differences
        assert only.category == "status_code"
        assert only.severity == "error"
        assert only.baseline_value == 200
        assert only.candidate_value == 404

    def test_compare_status_disabled(self) -> None:
        comparison = ComparisonConfig(compare_status=False)
        assert diff(snap(status=200), snap(status=500), comparison=comparison) == []

    def test_status_skipped_when_snapshot_has_no_code(self) -> None:
        assert diff(snap(status=None), snap(status=200)) == []


class TestHeaderComparison:
    def test_header_removed(self) -> None:
        baseline = snap(headers=(("x-one", ("1",)),))
        candidate = snap(headers=())
        (only,) = diff(baseline, candidate)
        assert only.category == "header_removed"
        assert only.path == "header:x-one"
        assert only.candidate_value == MISSING

    def test_header_added(self) -> None:
        baseline = snap(headers=())
        candidate = snap(headers=(("x-two", ("2",)),))
        (only,) = diff(baseline, candidate)
        assert only.category == "header_added"
        assert only.baseline_value == MISSING
        assert only.candidate_value == ["2"]

    def test_header_value_differs_multi_value(self) -> None:
        baseline = snap(headers=(("x-multi", ("a", "b")),))
        candidate = snap(headers=(("x-multi", ("a", "c")),))
        (only,) = diff(baseline, candidate)
        assert only.category == "header_value"
        assert only.baseline_value == ["a", "b"]
        assert only.candidate_value == ["a", "c"]

    def test_sensitive_header_value_is_redacted(self) -> None:
        baseline = snap(headers=(("authorization", ("Bearer hunter2",)),))
        candidate = snap(headers=(("authorization", ("Bearer other",)),))
        (only,) = diff(baseline, candidate)
        assert only.category == "header_value"
        assert only.message == "Sensitive field differs"
        assert only.baseline_value == REDACTED
        assert only.candidate_value == REDACTED

    def test_sensitive_header_removed_is_redacted(self) -> None:
        baseline = snap(headers=(("x-api-key", ("secret-value",)),))
        candidate = snap(headers=())
        (only,) = diff(baseline, candidate)
        assert only.category == "header_removed"
        assert only.baseline_value == REDACTED

    def test_sensitive_header_added_is_redacted(self) -> None:
        baseline = snap(headers=())
        candidate = snap(headers=(("set-cookie", ("sid=1", "flags=a")),))
        (only,) = diff(baseline, candidate)
        assert only.category == "header_added"
        assert only.candidate_value == REDACTED

    def test_case_can_disable_header_comparison(self) -> None:
        case = RequestCase(id="c", method="GET", path="/x", compare_headers=False)
        baseline = snap(headers=(("x-one", ("1",)),))
        candidate = snap(headers=(("x-one", ("2",)),))
        assert diff(baseline, candidate, case=case) == []


class TestJsonStructuralDiff:
    def test_removed_nested_key_reports_pointer(self) -> None:
        baseline = snap(body=jbody({"customer": {"name": "Ada", "status": "active"}}))
        candidate = snap(body=jbody({"customer": {"name": "Ada"}}))
        (only,) = diff(baseline, candidate)
        assert only.category == "json_key_removed"
        assert only.path == "/customer/status"
        assert only.baseline_value == "active"
        assert only.candidate_value == MISSING

    def test_added_nested_key(self) -> None:
        baseline = snap(body=jbody({"a": {}}))
        candidate = snap(body=jbody({"a": {"b": 1}}))
        (only,) = diff(baseline, candidate)
        assert only.category == "json_key_added"
        assert only.path == "/a/b"

    def test_value_differs_at_pointer(self) -> None:
        baseline = snap(body=jbody({"count": 1}))
        candidate = snap(body=jbody({"count": 2}))
        (only,) = diff(baseline, candidate)
        assert only.category == "json_value"
        assert only.path == "/count"
        assert only.baseline_value == 1
        assert only.candidate_value == 2

    def test_type_change(self) -> None:
        baseline = snap(body=jbody({"count": 1}))
        candidate = snap(body=jbody({"count": "1"}))
        (only,) = diff(baseline, candidate)
        assert only.category == "json_type"
        assert only.details == {"baseline_type": "number", "candidate_type": "string"}

    def test_null_versus_missing(self) -> None:
        baseline = snap(body=jbody({"x": None}))
        candidate = snap(body=jbody({}))
        (only,) = diff(baseline, candidate)
        assert only.category == "json_key_removed"
        assert only.path == "/x"
        assert only.baseline_value is None

    def test_boolean_vs_number_is_type_change(self) -> None:
        baseline = snap(body=jbody({"flag": True}))
        candidate = snap(body=jbody({"flag": 1}))
        (only,) = diff(baseline, candidate)
        assert only.category == "json_type"

    def test_array_element_diff(self) -> None:
        baseline = snap(body=jbody({"items": [1, 2, 3]}))
        candidate = snap(body=jbody({"items": [1, 9, 3]}))
        (only,) = diff(baseline, candidate)
        assert only.path == "/items/1"

    def test_array_length_differs(self) -> None:
        baseline = snap(body=jbody({"items": [1, 2]}))
        candidate = snap(body=jbody({"items": [1]}))
        (only,) = diff(baseline, candidate)
        assert only.message == "array length differs"
        assert only.path == "/items"

    def test_array_order_preserved_by_default(self) -> None:
        baseline = snap(body=jbody({"tags": ["b", "a"]}))
        candidate = snap(body=jbody({"tags": ["a", "b"]}))
        differences = diff(baseline, candidate)
        assert [d.path for d in differences] == ["/tags/0", "/tags/1"]

    def test_array_order_sort_policy(self) -> None:
        normalization = NormalizationConfig(array_order={"/tags": "sort"})
        baseline = snap(body=jbody({"tags": ["b", "a"]}))
        candidate = snap(body=jbody({"tags": ["a", "b"]}))
        assert diff(baseline, candidate, normalization=normalization) == []

    def test_pointer_escaping_for_special_keys(self) -> None:
        baseline = snap(body=jbody({"a/b": 1, "c~d": 2}))
        candidate = snap(body=jbody({"a/b": 9, "c~d": 9}))
        paths = {d.path for d in diff(baseline, candidate)}
        assert paths == {"/a~1b", "/c~0d"}

    def test_ignored_pointer_skips_subtree(self) -> None:
        normalization = NormalizationConfig(ignored_json_pointers=("/metadata",))
        baseline = snap(body=jbody({"metadata": {"version": "1"}, "id": 1}))
        candidate = snap(body=jbody({"id": 1}))
        assert diff(baseline, candidate, normalization=normalization) == []

    def test_ignored_pointer_nested_walk(self) -> None:
        normalization = NormalizationConfig(ignored_json_pointers=("/metadata/version",))
        baseline = snap(body=jbody({"metadata": {"version": "1", "other": 2}}))
        candidate = snap(body=jbody({"metadata": {"other": 3}}))
        (only,) = diff(baseline, candidate, normalization=normalization)
        assert only.path == "/metadata/other"

    def test_sensitive_json_pointer_value_redacted(self) -> None:
        redaction = RedactionConfig(sensitive_json_pointers=("/credentials/token",))
        baseline = snap(body=jbody({"credentials": {"token": "hunter2"}}))
        candidate = snap(body=jbody({"credentials": {"token": "rotated"}}))
        (only,) = diff(baseline, candidate, redaction=redaction)
        assert only.message == "Sensitive field differs"
        assert only.baseline_value == REDACTED
        assert only.candidate_value == REDACTED

    def test_sensitive_json_removed_key_message(self) -> None:
        redaction = RedactionConfig(sensitive_json_pointers=("/credentials/token",))
        baseline = snap(body=jbody({"credentials": {"token": "hunter2"}}))
        candidate = snap(body=jbody({"credentials": {}}))
        (only,) = diff(baseline, candidate, redaction=redaction)
        assert "Sensitive field differs" in only.message
        assert only.baseline_value == REDACTED

    def test_sensitive_json_added_key_message(self) -> None:
        redaction = RedactionConfig(sensitive_json_pointers=("/credentials/token",))
        baseline = snap(body=jbody({"credentials": {}}))
        candidate = snap(body=jbody({"credentials": {"token": "hunter2"}}))
        (only,) = diff(baseline, candidate, redaction=redaction)
        assert "Sensitive field differs" in only.message
        assert only.candidate_value == REDACTED

    def test_long_string_preview_is_bounded(self) -> None:
        long_value = "x" * 500
        baseline = snap(body=jbody({"s": long_value}))
        candidate = snap(body=jbody({"s": long_value + "!"}))
        (only,) = diff(baseline, candidate)
        assert len(only.baseline_value) <= 301
        assert only.baseline_value.endswith("…")


class TestBodyComparison:
    def test_body_present_in_only_one_response(self) -> None:
        baseline = snap(body=jbody({"a": 1}))
        candidate = snap(body=None)
        (only,) = diff(baseline, candidate)
        assert only.category == "body_type"
        assert only.baseline_value == "present"
        assert only.candidate_value == "absent"

    def test_parseability_differs(self) -> None:
        baseline = snap(body=jbody({"a": 1}), content_type="application/json")
        candidate = snap(body=b"not json", content_type="text/plain")
        (only,) = diff(baseline, candidate)
        assert only.category == "body_type"
        assert "parseability" in only.message

    def test_text_body_differs_with_digest(self) -> None:
        baseline = snap(body=b"hello", content_type="text/plain")
        candidate = snap(body=b"goodbye", content_type="text/plain")
        (only,) = diff(baseline, candidate)
        assert only.category == "text_body"
        expected = hashlib.sha256(b"hello").hexdigest()
        assert only.baseline_value == {"sha256": expected, "bytes": 5}

    def test_text_whitespace_normalization_off_by_default(self) -> None:
        baseline = snap(body=b"a  b", content_type="text/plain")
        candidate = snap(body=b"a b", content_type="text/plain")
        assert len(diff(baseline, candidate)) == 1

    def test_text_whitespace_normalization_enabled(self) -> None:
        normalization = NormalizationConfig(whitespace_text=True)
        baseline = snap(body=b"a  b", content_type="text/plain")
        candidate = snap(body=b"a b", content_type="text/plain")
        assert diff(baseline, candidate, normalization=normalization) == []

    def test_binary_body_differs(self) -> None:
        baseline = snap(body=b"\xff\xfe", content_type="application/octet-stream")
        candidate = snap(body=b"\xff\xfd", content_type="application/octet-stream")
        (only,) = diff(baseline, candidate)
        assert only.category == "binary_body"
        assert only.baseline_value["sha256"] == hashlib.sha256(b"\xff\xfe").hexdigest()

    def test_binary_body_identical(self) -> None:
        body = b"\x00\xff\xfe"
        baseline = snap(body=body, content_type="application/octet-stream")
        candidate = snap(body=body, content_type="application/octet-stream")
        assert diff(baseline, candidate) == []

    def test_case_can_disable_body_comparison(self) -> None:
        case = RequestCase(id="c", method="GET", path="/x", compare_body=False)
        baseline = snap(body=jbody({"a": 1}))
        candidate = snap(body=jbody({"a": 2}))
        assert diff(baseline, candidate, case=case) == []

    def test_truncated_body_cannot_pass(self) -> None:
        baseline = snap(body=jbody({"a": 1}), truncated=True)
        candidate = snap(body=jbody({"a": 1}))
        categories = {d.category for d in diff(baseline, candidate)}
        assert "response_too_large" in categories

    def test_charset_parsing(self) -> None:
        baseline = snap(body="café".encode(), content_type="text/plain; charset=utf-8")
        candidate = snap(body="café".encode(), content_type="text/plain; charset=utf-8")
        assert diff(baseline, candidate) == []

    def test_unknown_charset_falls_back_to_binary(self) -> None:
        baseline = snap(body=b"\x80\x81", content_type="text/plain; charset=not-a-charset")
        candidate = snap(body=b"\x80\x82", content_type="text/plain; charset=not-a-charset")
        (only,) = diff(baseline, candidate)
        assert only.category == "binary_body"


class TestInfrastructureFailures:
    def test_candidate_transport_error(self) -> None:
        baseline = snap(status=200)
        candidate = snap(status=None, error_kind="transport", transport_error="refused")
        (only,) = diff(baseline, candidate)
        assert only.category == "transport_error"
        assert "infrastructure failure" in only.message

    def test_both_transport_errors_recorded(self) -> None:
        baseline = snap(status=None, error_kind="transport", transport_error="base down")
        candidate = snap(status=None, error_kind="timeout", transport_error="too slow")
        categories = {d.category for d in diff(baseline, candidate)}
        assert categories == {"transport_error", "timeout"}

    def test_infra_early_return_skips_other_checks(self) -> None:
        baseline = snap(status=200)
        candidate = snap(status=None, error_kind="timeout", transport_error="slow")
        differences = diff(baseline, candidate)
        assert len(differences) == 1
        assert differences[0].category == "timeout"


class TestTiming:
    def test_timing_off_produces_no_difference(self) -> None:
        comparison = ComparisonConfig(timing_mode="off", timing_max_ms=1.0)
        baseline = snap(duration=0.5)
        candidate = snap(duration=50.0)
        assert diff(baseline, candidate, comparison=comparison) == []

    def test_timing_warn_is_warning(self) -> None:
        comparison = ComparisonConfig(timing_mode="warn", timing_max_ms=1.0)
        baseline = snap(duration=0.5)
        candidate = snap(duration=50.0)
        (only,) = diff(baseline, candidate, comparison=comparison)
        assert only.category == "timing"
        assert only.severity == "warning"

    def test_timing_fail_is_error(self) -> None:
        comparison = ComparisonConfig(timing_mode="fail", timing_max_ms=1.0)
        baseline = snap(duration=0.5)
        candidate = snap(duration=50.0)
        (only,) = diff(baseline, candidate, comparison=comparison)
        assert only.severity == "error"

    def test_timing_within_threshold(self) -> None:
        comparison = ComparisonConfig(timing_mode="fail", timing_max_ms=100.0)
        assert diff(snap(duration=5.0), snap(duration=50.0), comparison=comparison) == []

    def test_timing_mode_without_threshold_is_off(self) -> None:
        comparison = ComparisonConfig(timing_mode="fail", timing_max_ms=None)
        assert diff(snap(duration=1.0), snap(duration=999.0), comparison=comparison) == []


class TestDifferenceCap:
    def test_header_cap_appends_truncation_note(self) -> None:
        comparison = ComparisonConfig(max_differences_per_case=2)
        baseline = snap(
            headers=(
                ("x-one", ("1",)),
                ("x-two", ("2",)),
                ("x-three", ("3",)),
                ("x-four", ("4",)),
            )
        )
        candidate = snap(headers=())
        differences = diff(baseline, candidate, comparison=comparison)
        assert len(differences) == 3
        assert differences[-1].category == "normalization_note"
        assert differences[-1].severity == "info"

    def test_json_cap_bounded_without_note(self) -> None:
        # JSON recursion short-circuits once full (bounds work); the cap still holds.
        comparison = ComparisonConfig(max_differences_per_case=2)
        baseline = snap(body=jbody({"a": 1, "b": 2, "c": 3, "d": 4}))
        candidate = snap(body=jbody({"a": 9, "b": 9, "c": 9, "d": 9}))
        differences = diff(baseline, candidate, comparison=comparison)
        assert len(differences) == 2
        assert all(d.severity == "error" for d in differences)

    def test_no_note_when_under_cap(self) -> None:
        comparison = ComparisonConfig(max_differences_per_case=10)
        baseline = snap(body=jbody({"a": 1}))
        candidate = snap(body=jbody({"a": 2}))
        differences = diff(baseline, candidate, comparison=comparison)
        assert all(d.category != "normalization_note" for d in differences)


@pytest.mark.parametrize(
    ("baseline_body", "candidate_body"),
    [
        (jbody({"s": "x"}), jbody({"s": "y"})),
        (jbody({"n": 1}), jbody({"n": 2})),
        (jbody({"arr": [1]}), jbody({"arr": [2]})),
    ],
)
def test_value_differs_parametrized(baseline_body: bytes, candidate_body: bytes) -> None:
    differences = diff(snap(body=baseline_body), snap(body=candidate_body))
    assert len(differences) == 1
    assert differences[0].severity == "error"
