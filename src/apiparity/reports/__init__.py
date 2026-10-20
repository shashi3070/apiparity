"""Report writers: JSON (canonical), JUnit XML (CI), terminal (humans)."""

from __future__ import annotations

from pathlib import Path

from apiparity.models import RedactionConfig, ReportingConfig, RunResult
from apiparity.reports.json_report import build_json_report, write_json_report
from apiparity.reports.junit_report import build_junit_tree, write_junit_report
from apiparity.reports.terminal_report import render_terminal

__all__ = [
    "build_json_report",
    "build_junit_tree",
    "render_terminal",
    "write_json_report",
    "write_junit_report",
    "write_reports",
]


def write_reports(
    result: RunResult,
    output_dir: Path,
    *,
    reporting: ReportingConfig,
    redaction: RedactionConfig,
    include_body_previews: bool = False,
) -> list[Path]:
    """Write enabled file reports into ``output_dir``; returns written paths."""
    written: list[Path] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    if reporting.json:
        written.append(
            write_json_report(
                result,
                output_dir / "report.json",
                redaction=redaction,
                include_body_previews=include_body_previews,
            )
        )
    if reporting.junit_xml:
        written.append(write_junit_report(result, output_dir / "junit.xml"))
    return written
