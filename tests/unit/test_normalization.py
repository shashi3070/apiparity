"""Unit tests for response normalization rules (apiparity.normalization)."""

from __future__ import annotations

import json

from apiparity.models import NormalizationConfig, ResponseSnapshot
from apiparity.normalization import (
    array_sort_key,
    describe_rules,
    filter_headers,
    is_ignored_pointer,
    normalize_snapshot,
    normalize_text,
)


def snap_with_headers(*names: str) -> ResponseSnapshot:
    headers = tuple((name, ("v",)) for name in names)
    return ResponseSnapshot(
        status_code=200,
        headers=headers,
        content_type="application/json",
        body_bytes=b"{}",
        body_truncated=False,
        body_size=2,
        duration_ms=1.0,
        transport_error=None,
        error_kind="none",
    )


class TestFilterHeaders:
    def test_drops_ignored_case_insensitive(self) -> None:
        headers = (("date", ("x",)), ("content-type", ("application/json",)))
        assert filter_headers(headers, ("Date",)) == (("content-type", ("application/json",)),)

    def test_keeps_all_when_nothing_ignored(self) -> None:
        headers = (("date", ("x",)),)
        assert filter_headers(headers, ()) == headers


class TestNormalizeSnapshot:
    def test_removes_ignored_headers(self) -> None:
        rules = NormalizationConfig(ignored_headers=("date", "content-length"))
        result = normalize_snapshot(snap_with_headers("date", "content-type"), rules)
        assert result.header_map().keys() == {"content-type"}

    def test_returns_same_object_when_nothing_filtered(self) -> None:
        snapshot = snap_with_headers("content-type")
        rules = NormalizationConfig(ignored_headers=("date",))
        assert normalize_snapshot(snapshot, rules) is snapshot

    def test_empty_ignore_list_returns_same_object(self) -> None:
        snapshot = snap_with_headers("date")
        rules = NormalizationConfig(ignored_headers=())
        assert normalize_snapshot(snapshot, rules) is snapshot

    def test_preserves_other_fields(self) -> None:
        rules = NormalizationConfig(ignored_headers=("date",))
        result = normalize_snapshot(snap_with_headers("date", "content-type"), rules)
        assert result.status_code == 200
        assert result.body_bytes == b"{}"
        assert result.duration_ms == 1.0


class TestIsIgnoredPointer:
    def test_exact_match(self) -> None:
        assert is_ignored_pointer("/metadata", ("/metadata",))

    def test_subtree_semantics(self) -> None:
        assert is_ignored_pointer("/metadata/request_id", ("/metadata",))

    def test_prefix_but_not_boundary_is_not_ignored(self) -> None:
        assert not is_ignored_pointer("/metadatax", ("/metadata",))

    def test_empty_ignored_list(self) -> None:
        assert not is_ignored_pointer("/anything", ())


class TestNormalizeText:
    def test_off_by_default(self) -> None:
        assert normalize_text("a  \n b", NormalizationConfig()) == "a  \n b"

    def test_collapses_whitespace(self) -> None:
        rules = NormalizationConfig(whitespace_text=True)
        assert normalize_text("  a   b \n c ", rules) == "a b c"


class TestArraySortKey:
    def test_dict_key_order_irrelevant(self) -> None:
        assert array_sort_key({"b": 1, "a": 2}) == array_sort_key({"a": 2, "b": 1})

    def test_deterministic_for_scalars(self) -> None:
        assert array_sort_key(3) == json.dumps(3, sort_keys=True)
        assert array_sort_key("x") == json.dumps("x", sort_keys=True)


class TestDescribeRules:
    def test_machine_readable_description(self) -> None:
        rules = NormalizationConfig(
            ignored_headers=("date",),
            ignored_json_pointers=("/metadata",),
            whitespace_text=True,
            array_order={"/tags": "sort"},
        )
        described = describe_rules(rules)
        assert described["ignored_headers"] == ["date"]
        assert described["ignored_json_pointers"] == ["/metadata"]
        assert described["whitespace_text"] is True
        assert described["array_order"] == {"/tags": "sort"}
