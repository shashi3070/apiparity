"""Unit tests for JSON/JUnit/terminal report writers."""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

from apiparity.models import (
    CaseResult,
    Difference,
    RedactionConfig,
    ReportingConfig,
    ResponseSnapshot,
    RunResult,
    RunSummary,
)
from apiparity.redaction import REDACTED
from apiparity.reports import (
    build_json_report,
    build_junit_tree,
    render_terminal,
    write_reports,
)
from apiparity.reports.terminal_report import format_difference


def snap(
    *,
    status: int = 200,
    headers: tuple[tuple[str, tuple[str, ...]], ...] = (),
    body: bytes | None = b'{"status": "ok", "credentials": {"token": "hunter2"}}',
    error_kind: str = "none",
    transport_error: str | None = None,
) -> ResponseSnapshot:
    body = body or b""
    return ResponseSnapshot(
        status_code=status,
        headers=headers,
        content_type="application/json" if body else None,
        body_bytes=body,
        body_truncated=False,
        body_size=len(body),
        duration_ms=3.0,
        transport_error=transport_error,
        error_kind=error_kind,  # type: ignore[arg-type]
    )


def make_result(*, exit_code: int = 0) -> RunResult:
    secret_headers = (("authorization", ("Bearer hunter2",)),)
    match_case = CaseResult(
        case_id="passing-case",
        status="match",
        baseline=snap(headers=secret_headers),
        candidate=snap(headers=secret_headers),
        duration_ms=6.0,
    )
    mismatch_case = CaseResult(
        case_id="mismatch-case",
        status="mismatch",
        differences=(
            Difference(
                category="json_value",
                severity="error",
                message="value differs\x07",
                case_id="mismatch-case",
                path="/customer/status",
                baseline_value="active",
                candidate_value="inactive",
            ),
        ),
        baseline=snap(),
        candidate=snap(),
        duration_ms=8.0,
    )
    failed_case = CaseResult(
        case_id="failed-case",
        status="failed",
        differences=(
            Difference(
                category="transport_error",
                severity="error",
                message="baseline request failed (transport): refused",
                case_id="failed-case",
            ),
        ),
        baseline=snap(status=None, body=None, error_kind="transport", transport_error="refused"),
        candidate=None,
        duration_ms=1.0,
    )
    skipped_case = CaseResult(
        case_id="skipped-case",
        status="skipped",
        skipped_reason="disabled in corpus (enabled: false)",
    )
    cases = (match_case, mismatch_case, failed_case, skipped_case)
    return RunResult(
        summary=RunSummary(
            total=4,
            matched=1,
            mismatched=1,
            failed=1,
            skipped=1,
            started_at="2026-10-20T00:00:00Z",
            finished_at="2026-10-20T00:00:01Z",
            exit_code=exit_code,
        ),
        cases=cases,
        target_descriptions={
            "baseline": "wsgi:legacy_flask.app:app",
            "candidate": "asgi:migrated_fastapi.app:app",
        },
        corpus_path="corpus.yaml",
        config_fingerprint="fp-123",
        normalization_rules_applied={"normalization": {}},
        tool_version="0.1.0",
    )


class TestJsonReport:
    def test_schema_and_summary(self) -> None:
        report = build_json_report(make_result(exit_code=1), redaction=RedactionConfig())
        assert report["schema_version"] == 1
        assert report["exit_code"] == 1
        assert report["result"] == "fail"
        assert report["summary"] == {
            "total": 4,
            "matched": 1,
            "mismatched": 1,
            "failed": 1,
            "skipped": 1,
        }
        assert len(report["cases"]) == 4

    def test_secret_header_redacted(self) -> None:
        report = build_json_report(make_result(), redaction=RedactionConfig())
        match = report["cases"][0]
        assert match["baseline"]["headers"]["authorization"] == [REDACTED]

    def test_no_body_preview_by_default(self) -> None:
        report = build_json_report(make_result(), redaction=RedactionConfig())
        assert "body_preview" not in report["cases"][0]["baseline"]

    def test_body_preview_redacts_sensitive_pointers(self) -> None:
        report = build_json_report(
            make_result(),
            redaction=RedactionConfig(),
            include_body_previews=True,
        )
        preview = report["cases"][0]["baseline"]["body_preview"]
        assert "hunter2" not in preview
        assert REDACTED in preview

    def test_non_json_body_has_no_preview(self) -> None:
        result = make_result()
        first = result.cases[0]
        replaced = CaseResult(
            case_id=first.case_id,
            status=first.status,
            baseline=snap(body=b"\x00\x01", headers=()),
            candidate=snap(body=b"\x00\x01", headers=()),
            duration_ms=first.duration_ms,
        )
        tweaked = RunResult(
            summary=result.summary,
            cases=(replaced, *result.cases[1:]),
            target_descriptions=result.target_descriptions,
            corpus_path=result.corpus_path,
            config_fingerprint=result.config_fingerprint,
            normalization_rules_applied=result.normalization_rules_applied,
            tool_version=result.tool_version,
        )
        report = build_json_report(tweaked, redaction=RedactionConfig(), include_body_previews=True)
        assert "body_preview" not in report["cases"][0]["baseline"]

    def test_skipped_reason_included(self) -> None:
        report = build_json_report(make_result(), redaction=RedactionConfig())
        skipped = report["cases"][3]
        assert skipped["status"] == "skipped"
        assert "disabled" in skipped["skipped_reason"]

    def test_write_json_report(self, tmp_path: Path) -> None:
        from apiparity.reports.json_report import write_json_report

        path = write_json_report(
            make_result(), tmp_path / "out" / "report.json", redaction=RedactionConfig()
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["schema_version"] == 1


class TestJUnitReport:
    def test_counts(self) -> None:
        tree = build_junit_tree(make_result(exit_code=1))
        root = tree.getroot()
        assert root.get("tests") == "4"
        assert root.get("failures") == "1"
        assert root.get("errors") == "1"
        assert root.get("skipped") == "1"

    def test_failure_and_error_elements(self) -> None:
        tree = build_junit_tree(make_result(exit_code=1))
        xml = ET.tostring(tree.getroot(), encoding="unicode")
        assert "<failure" in xml
        assert "<error" in xml
        assert "<skipped" in xml
        assert "disabled in corpus" in xml

    def test_control_chars_stripped(self) -> None:
        tree = build_junit_tree(make_result(exit_code=1))
        xml = ET.tostring(tree.getroot(), encoding="unicode")
        assert "\x07" not in xml

    def test_failure_text_contains_pointer(self) -> None:
        tree = build_junit_tree(make_result(exit_code=1))
        xml = ET.tostring(tree.getroot(), encoding="unicode")
        assert "/customer/status" in xml

    def test_write_junit_report(self, tmp_path: Path) -> None:
        from apiparity.reports.junit_report import write_junit_report

        path = write_junit_report(make_result(), tmp_path / "nested" / "junit.xml")
        parsed = ET.parse(path)
        assert parsed.getroot().get("name") == "apiparity"


class TestTerminalReport:
    def test_full_render_hides_matches_shows_totals(self) -> None:
        text = render_terminal(make_result(exit_code=1), redaction=RedactionConfig())
        assert "wsgi:legacy_flask.app:app" in text
        assert "mismatch-case" in text
        assert "failed-case" in text
        assert "skipped-case" in text
        assert "passing-case" not in text
        assert "1 matched" in text
        assert "RESULT: FAIL" in text

    def test_verbose_shows_matches_and_snapshots(self) -> None:
        text = render_terminal(make_result(exit_code=1), verbose=True, redaction=RedactionConfig())
        assert "passing-case" in text
        assert "status=200" in text

    def test_quiet_mode(self) -> None:
        text = render_terminal(
            make_result(exit_code=0),
            quiet=True,
            redaction=RedactionConfig(),
            show_reports=["r.json"],
        )
        assert text.startswith("RESULT: PASS")
        assert "reports: r.json" in text
        assert "totals:" not in text

    def test_report_paths_shown(self) -> None:
        text = render_terminal(
            make_result(), redaction=RedactionConfig(), show_reports=["a/report.json"]
        )
        assert "reports: a/report.json" in text

    def test_format_difference_includes_values_for_path_errors(self) -> None:
        diff = Difference(
            category="json_value",
            severity="error",
            message="value differs",
            case_id="c",
            path="/x",
            baseline_value=1,
            candidate_value=2,
        )
        line = format_difference(diff)
        assert "baseline=1" in line
        assert "candidate=2" in line

    def test_format_difference_info_without_path(self) -> None:
        diff = Difference(
            category="normalization_note",
            severity="info",
            message="truncated",
            case_id="c",
        )
        line = format_difference(diff)
        assert "baseline=" not in line


class TestWriteReports:
    def test_writes_both_by_default(self, tmp_path: Path) -> None:
        written = write_reports(
            make_result(),
            tmp_path / "out",
            reporting=ReportingConfig(),
            redaction=RedactionConfig(),
        )
        names = {path.name for path in written}
        assert names == {"report.json", "junit.xml"}
        for path in written:
            assert path.exists()

    def test_respects_reporting_flags(self, tmp_path: Path) -> None:
        written = write_reports(
            make_result(),
            tmp_path / "out",
            reporting=ReportingConfig(json=True, junit_xml=False),
            redaction=RedactionConfig(),
        )
        assert [path.name for path in written] == ["report.json"]

    def test_nothing_when_all_disabled(self, tmp_path: Path) -> None:
        written = write_reports(
            make_result(),
            tmp_path / "out",
            reporting=ReportingConfig(json=False, junit_xml=False),
            redaction=RedactionConfig(),
        )
        assert written == []
