"""Unit tests for runner orchestration logic (apiparity.runner)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apiparity.config import load_config_text
from apiparity.corpus import load_corpus
from apiparity.exceptions import ConfigError, CorpusError, SafetyError, TargetInitError
from apiparity.models import AppConfig, CaseResult, TargetConfig
from apiparity.runner import _merge_normalization, compare_sync, compute_exit_code, run_comparison

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _corpus(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestComputeExitCode:
    def test_all_match(self) -> None:
        cases = [CaseResult(case_id="a", status="match"), CaseResult(case_id="b", status="match")]
        assert compute_exit_code(cases) == 0

    def test_skips_allowed(self) -> None:
        cases = [
            CaseResult(case_id="a", status="match"),
            CaseResult(case_id="b", status="skipped", skipped_reason="disabled"),
        ]
        assert compute_exit_code(cases) == 0

    def test_mismatch_fails(self) -> None:
        assert compute_exit_code([CaseResult(case_id="a", status="mismatch")]) == 1

    def test_failed_fails(self) -> None:
        assert compute_exit_code([CaseResult(case_id="a", status="failed")]) == 1

    def test_empty_is_pass(self) -> None:
        assert compute_exit_code([]) == 0


class TestMergeNormalization:
    def test_union_with_config_first(self, tmp_path: Path) -> None:
        config = load_config_text(
            """
version: 1
normalization:
  ignored_headers: [X-Custom]
  ignored_json_pointers: [/a]
"""
        )
        corpus = load_corpus(
            _corpus(
                tmp_path,
                """
version: 1
normalization:
  ignored_headers: [date]
  ignored_json_pointers: [/b]
  array_order: {/tags: sort}
cases: [{id: x, method: GET, path: /x}]
""",
            )
        )
        merged = _merge_normalization(config, corpus)
        assert "x-custom" in merged.ignored_headers
        assert "date" in merged.ignored_headers
        assert merged.ignored_json_pointers == ("/a", "/b")
        assert merged.array_order == {"/tags": "sort"}


class TestRunComparisonGuards:
    def test_missing_targets_raise_config_error(self, tmp_path: Path) -> None:
        corpus = load_corpus(
            _corpus(tmp_path, "version: 1\ncases: [{id: a, method: GET, path: /a}]\n")
        )
        with pytest.raises(ConfigError, match="both a baseline and a candidate"):
            asyncio.run(run_comparison(corpus=corpus, config=AppConfig()))

    def test_safety_refusal_precedes_target_initialization(self, tmp_path: Path) -> None:
        corpus = load_corpus(
            _corpus(tmp_path, "version: 1\ncases: [{id: a, method: POST, path: /a}]\n")
        )
        config = AppConfig(
            old_target=TargetConfig(kind="wsgi", app="does.not.exist:app"),
            new_target=TargetConfig(kind="wsgi", app="does.not.exist:app"),
        )
        # SafetyError must win: no app import is attempted.
        with pytest.raises(SafetyError):
            asyncio.run(run_comparison(corpus=corpus, config=config))

    def test_target_init_failure_wraps_import_error(self, tmp_path: Path) -> None:
        corpus = load_corpus(
            _corpus(tmp_path, "version: 1\ncases: [{id: a, method: GET, path: /a}]\n")
        )
        config = AppConfig(
            old_target=TargetConfig(kind="wsgi", app="does.not.exist:app"),
            new_target=TargetConfig(kind="wsgi", app="does.not.exist:app"),
        )
        with pytest.raises(TargetInitError, match=r"does\.not\.exist"):
            asyncio.run(run_comparison(corpus=corpus, config=config))

    def test_invalid_corpus_fails_before_anything(self, tmp_path: Path) -> None:
        with pytest.raises(CorpusError):
            load_corpus(_corpus(tmp_path, "version: 1\ncases: []\n"))


class TestCompareSyncApi:
    def test_compare_sync_full_run_equivalent_apps(self, tmp_path: Path) -> None:
        corpus_path = FIXTURES / "corpora" / "equivalent.yaml"
        result = compare_sync(
            corpus=corpus_path,
            config=AppConfig(),
            old_target=TargetConfig(kind="wsgi", app="legacy_flask.app:app", label="baseline"),
            new_target=TargetConfig(kind="asgi", app="migrated_fastapi.app:app", label="candidate"),
        )
        assert result.summary.exit_code == 0
        assert result.summary.mismatched == 0
        assert result.summary.failed == 0
        assert result.summary.total == len(result.cases)
        assert result.tool_version
        assert result.config_fingerprint

    def test_on_case_done_callback(self) -> None:
        seen: list[tuple[str, int, int]] = []

        def on_case_done(case: CaseResult, index: int, total: int) -> None:
            seen.append((case.case_id, index, total))

        result = compare_sync(
            corpus=FIXTURES / "corpora" / "equivalent.yaml",
            old_target=TargetConfig(kind="wsgi", app="legacy_flask.app:app"),
            new_target=TargetConfig(kind="asgi", app="migrated_fastapi.app:app"),
            on_case_done=on_case_done,
        )
        assert [item[0] for item in seen] == [case.case_id for case in result.cases]
        assert all(item[2] == result.summary.total for item in seen)
