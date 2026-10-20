from __future__ import annotations

from pathlib import Path

import pytest

from apiparity.corpus import build_corpus, load_corpus, load_corpus_text
from apiparity.exceptions import CorpusError, VariableResolutionError

VALID_YAML = """
version: 1
defaults:
  timeout_seconds: 5
  follow_redirects: false
  compare_headers: true
  compare_body: true
normalization:
  ignored_headers: [Date, server]
  ignored_json_pointers:
    - /metadata/request_id
cases:
  - id: health
    description: health check
    method: get
    path: /health
  - id: search
    method: GET
    path: /api/items
    query:
      limit: 20
      tags: [a, b]
      active: true
    headers:
      Accept: application/json
  - id: create
    method: POST
    path: /api/orders
    json:
      customer_id: "123"
      quantity: 2
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestValidCorpora:
    def test_yaml_corpus_parses(self, tmp_path: Path) -> None:
        corpus = load_corpus(_write(tmp_path, "requests.yaml", VALID_YAML))
        assert corpus.version == 1
        assert [case.id for case in corpus.cases] == ["health", "search", "create"]
        assert corpus.defaults["timeout_seconds"] == 5
        assert corpus.normalization.ignored_headers == ("date", "server")
        assert corpus.normalization.ignored_json_pointers == ("/metadata/request_id",)

    def test_method_uppercased(self, tmp_path: Path) -> None:
        corpus = load_corpus(_write(tmp_path, "requests.yaml", VALID_YAML))
        assert corpus.cases[0].method == "GET"

    def test_query_preserves_repeated_values_and_order(self, tmp_path: Path) -> None:
        corpus = load_corpus(_write(tmp_path, "requests.yaml", VALID_YAML))
        search = corpus.cases[1]
        assert search.query_pairs() == [
            ("limit", "20"),
            ("tags", "a"),
            ("tags", "b"),
            ("active", "true"),
        ]

    def test_json_corpus(self, tmp_path: Path) -> None:
        text = '{"version": 1, "cases": [{"id": "a", "method": "GET", "path": "/a"}]}'
        corpus = load_corpus(_write(tmp_path, "requests.json", text))
        assert corpus.cases[0].id == "a"

    def test_disabled_case_flag(self, tmp_path: Path) -> None:
        text = """
version: 1
cases:
  - id: a
    method: GET
    path: /a
    enabled: false
"""
        corpus = load_corpus(_write(tmp_path, "c.yaml", text))
        assert corpus.cases[0].enabled is False
        assert corpus.enabled_cases == ()

    def test_case_timeout_and_compare_overrides(self, tmp_path: Path) -> None:
        text = """
version: 1
cases:
  - id: a
    method: GET
    path: /a
    timeout_seconds: 2.5
    compare_body: false
"""
        corpus = load_corpus(_write(tmp_path, "c.yaml", text))
        assert corpus.cases[0].timeout_seconds == 2.5
        assert corpus.cases[0].compare_body is False


class TestSchemaFailures:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="not found"):
            load_corpus(tmp_path / "absent.yaml")

    def test_missing_version(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="version"):
            load_corpus(_write(tmp_path, "c.yaml", "cases: [{id: a, method: GET, path: /a}]"))

    def test_wrong_version(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="version"):
            load_corpus(_write(tmp_path, "c.yaml", "version: 2\ncases: []"))

    def test_unknown_top_level_key(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="unknown field"):
            load_corpus(
                _write(
                    tmp_path,
                    "c.yaml",
                    "version: 1\nsurprise: true\ncases: [{id: a, method: GET, path: /a}]",
                )
            )

    def test_empty_cases(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="non-empty"):
            load_corpus(_write(tmp_path, "c.yaml", "version: 1\ncases: []"))

    def test_cases_not_a_list(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="list"):
            load_corpus(_write(tmp_path, "c.yaml", "version: 1\ncases: {}"))

    def test_duplicate_ids(self, tmp_path: Path) -> None:
        text = """
version: 1
cases:
  - {id: dup, method: GET, path: /a}
  - {id: dup, method: GET, path: /b}
"""
        with pytest.raises(CorpusError, match="duplicate case id"):
            load_corpus(_write(tmp_path, "c.yaml", text))

    @pytest.mark.parametrize("method", ["FETCH", "", "GET_X", 123])
    def test_invalid_method(self, tmp_path: Path, method: object) -> None:
        text = f"version: 1\ncases: [{{id: a, method: {method!r}, path: /a}}]"
        with pytest.raises(CorpusError, match="method"):
            load_corpus(_write(tmp_path, "c.yaml", text))

    @pytest.mark.parametrize("path", ["api/x", "https://example.com/x", "/a b", "/a\nb"])
    def test_invalid_path(self, tmp_path: Path, path: str) -> None:
        import json as _json

        text = "version: 1\ncases: " + _json.dumps([{"id": "a", "method": "GET", "path": path}])
        with pytest.raises(CorpusError, match="path"):
            load_corpus(_write(tmp_path, "c.json", text))

    def test_conflicting_bodies(self, tmp_path: Path) -> None:
        text = """
version: 1
cases:
  - id: a
    method: POST
    path: /a
    json: {x: 1}
    data: "raw"
"""
        with pytest.raises(CorpusError, match="conflicting body"):
            load_corpus(_write(tmp_path, "c.yaml", text))

    @pytest.mark.parametrize("timeout", ["0", "-1", '"10"', "true"])
    def test_invalid_timeout(self, tmp_path: Path, timeout: str) -> None:
        text = f"version: 1\ncases: [{{id: a, method: GET, path: /a, timeout_seconds: {timeout}}}]"
        with pytest.raises(CorpusError, match="timeout_seconds"):
            load_corpus(_write(tmp_path, "c.yaml", text))

    def test_unknown_case_field(self, tmp_path: Path) -> None:
        text = "version: 1\ncases: [{id: a, method: GET, path: /a, sneaky: 1}]"
        with pytest.raises(CorpusError, match="unknown field"):
            load_corpus(_write(tmp_path, "c.yaml", text))

    def test_non_scalar_query_value(self, tmp_path: Path) -> None:
        text = """
version: 1
cases:
  - id: a
    method: GET
    path: /a
    query: {bad: {nested: 1}}
"""
        with pytest.raises(CorpusError, match="scalar"):
            load_corpus(_write(tmp_path, "c.yaml", text))

    def test_invalid_header_name(self, tmp_path: Path) -> None:
        text = """
version: 1
cases:
  - id: a
    method: GET
    path: /a
    headers: {"Bad Header": v}
"""
        with pytest.raises(CorpusError, match="header name"):
            load_corpus(_write(tmp_path, "c.yaml", text))

    def test_invalid_yaml_syntax(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="invalid YAML"):
            load_corpus(_write(tmp_path, "c.yaml", "version: 1\ncases: [unclosed"))

    def test_invalid_json_reports_line(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="line"):
            load_corpus(_write(tmp_path, "c.json", '{"version": 1,,}'))

    def test_normalization_bad_pointer(self, tmp_path: Path) -> None:
        text = """
version: 1
normalization: {ignored_json_pointers: ["metadata/id"]}
cases: [{id: a, method: GET, path: /a}]
"""
        with pytest.raises(CorpusError, match="JSON Pointer"):
            load_corpus(_write(tmp_path, "c.yaml", text))

    def test_normalization_bad_array_order_mode(self, tmp_path: Path) -> None:
        text = """
version: 1
normalization: {array_order: {"/items": shuffle}}
cases: [{id: a, method: GET, path: /a}]
"""
        with pytest.raises(CorpusError, match="array_order"):
            load_corpus(_write(tmp_path, "c.yaml", text))

    def test_normalization_bad_timing_mode(self, tmp_path: Path) -> None:
        text = """
version: 1
normalization: {timing: {mode: sometimes}}
cases: [{id: a, method: GET, path: /a}]
"""
        with pytest.raises(CorpusError, match=r"timing\.mode"):
            load_corpus(_write(tmp_path, "c.yaml", text))


class TestFileUploads:
    def _corpus_with_upload(self, path_value: str) -> str:
        return f"""
version: 1
cases:
  - id: upload
    method: POST
    path: /upload
    files: [{{field: doc, path: "{path_value}"}}]
"""

    def test_valid_upload(self, tmp_path: Path) -> None:
        (tmp_path / "fixture.txt").write_text("hello", encoding="utf-8")
        corpus = load_corpus(_write(tmp_path, "c.yaml", self._corpus_with_upload("fixture.txt")))
        upload = corpus.cases[0].files[0]
        assert upload.field == "doc"
        assert upload.path == "fixture.txt"

    def test_escaping_upload_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        try:
            with pytest.raises(CorpusError, match="escapes the corpus directory"):
                load_corpus(_write(tmp_path, "c.yaml", self._corpus_with_upload("../outside.txt")))
        finally:
            outside.unlink(missing_ok=True)

    def test_absolute_upload_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="relative"):
            load_corpus(_write(tmp_path, "c.yaml", self._corpus_with_upload("C:/temp/x.txt")))

    def test_missing_upload_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError, match="file not found"):
            load_corpus(_write(tmp_path, "c.yaml", self._corpus_with_upload("nope.txt")))

    def test_conflicting_files_and_json(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        text = """
version: 1
cases:
  - id: up
    method: POST
    path: /u
    json: {a: 1}
    files: [{field: f, path: f.txt}]
"""
        with pytest.raises(CorpusError, match="conflicting body"):
            load_corpus(_write(tmp_path, "c.yaml", text))


class TestVariableInterpolation:
    def test_resolves_in_headers_query_and_body(self, tmp_path: Path) -> None:
        text = """
version: 1
cases:
  - id: a
    method: GET
    path: /${SEGMENT}
    headers: {Authorization: "Bearer ${TOKEN}"}
    query: {q: "${Q}"}
    json: {nested: {value: "${TOKEN}"}}
"""
        corpus = load_corpus(
            _write(tmp_path, "c.yaml", text),
            env={"SEGMENT": "api", "TOKEN": "s3cret", "Q": "search"},
        )
        case = corpus.cases[0]
        assert case.path == "/api"
        assert case.headers["Authorization"] == "Bearer s3cret"
        assert case.query["q"] == ["search"]
        assert case.json_body == {"nested": {"value": "s3cret"}}

    def test_missing_variable_fails_before_execution(self, tmp_path: Path) -> None:
        text = """
version: 1
cases:
  - id: a
    method: GET
    path: /a
    headers: {Authorization: "Bearer ${MISSING_TOKEN}"}
"""
        with pytest.raises(VariableResolutionError) as excinfo:
            load_corpus(_write(tmp_path, "c.yaml", text), env={})
        assert "MISSING_TOKEN" in str(excinfo.value)

    def test_error_message_does_not_leak_known_secret(self, tmp_path: Path) -> None:
        text = """
version: 1
cases:
  - id: a
    method: GET
    path: /a
    headers:
      X: "${KNOWN}"
      Y: "${UNKNOWN}"
"""
        with pytest.raises(VariableResolutionError) as excinfo:
            load_corpus(_write(tmp_path, "c.yaml", text), env={"KNOWN": "hunter2"})
        assert "hunter2" not in str(excinfo.value)

    def test_path_revalidated_after_resolution(self, tmp_path: Path) -> None:
        text = """
version: 1
cases:
  - id: a
    method: GET
    path: "${BAD_PATH}"
"""
        with pytest.raises(CorpusError, match="path must start"):
            load_corpus(_write(tmp_path, "c.yaml", text), env={"BAD_PATH": "nope"})

    def test_build_corpus_helper_without_env(self) -> None:
        doc = {
            "version": 1,
            "cases": [{"id": "a", "method": "GET", "path": "/a", "headers": {"X": "${V}"}}],
        }
        from pathlib import Path as P

        corpus = build_corpus(doc, source_path=P("mem.yaml"), env={"V": "1"})
        assert corpus.cases[0].headers["X"] == "1"


def test_load_corpus_text_accepts_json_content(tmp_path: Path) -> None:
    corpus = load_corpus_text(
        '{"version": 1, "cases": [{"id": "a", "method": "GET", "path": "/a"}]}',
        source_path=tmp_path / "x.json",
    )
    assert corpus.cases[0].path == "/a"
