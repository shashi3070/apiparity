"""JUnit XML report for CI systems.

One ``<testcase>`` per request case: behavioral mismatches become ``<failure>``
and infrastructure problems become ``<error>`` so CI dashboards distinguish
"the API changed" from "the run broke". All content is XML-escaped and
control-character cleaned — differences are born redacted in the comparison
layer, so no secrets reach this writer.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from apiparity.models import CaseResult, RunResult

__all__ = ["build_junit_tree", "write_junit_report"]

_INVALID_XML_CHARS = frozenset(
    {0x00 + i for i in range(0x20) if i not in (0x09, 0x0A, 0x0D)} | {0xFFFE, 0xFFFF}
)


def _safe(text: str) -> str:
    """Strip characters XML 1.0 cannot represent (keeps tab/newline/carriage return)."""
    return "".join(ch for ch in text if ord(ch) not in _INVALID_XML_CHARS and ord(ch) < 0x110000)


def build_junit_tree(result: RunResult) -> ET.ElementTree:
    summary = result.summary
    failures = sum(1 for case in result.cases if case.status == "mismatch")
    errors = sum(1 for case in result.cases if case.status == "failed")
    skipped = sum(1 for case in result.cases if case.status == "skipped")
    total_seconds = sum(case.duration_ms for case in result.cases) / 1000.0

    suites = ET.Element(
        "testsuites",
        {
            "name": "apiparity",
            "tests": str(summary.total),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
            "time": f"{total_seconds:.3f}",
        },
    )
    suite = ET.SubElement(
        suites,
        "testsuite",
        {
            "name": "apiparity",
            "tests": str(summary.total),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
            "timestamp": summary.started_at,
            "time": f"{total_seconds:.3f}",
        },
    )

    for case in result.cases:
        description = result.target_descriptions.get("baseline", "run")
        testcase = ET.SubElement(
            suite,
            "testcase",
            {
                "name": _safe(case.case_id),
                "classname": f"apiparity.{_safe(description)}",
                "time": f"{case.duration_ms / 1000.0:.3f}",
            },
        )
        if case.status == "mismatch":
            message, detail = _difference_text(case)
            failure = ET.SubElement(testcase, "failure", {"message": _safe(message)})
            failure.text = _safe(detail)
        elif case.status == "failed":
            message, detail = _failure_text(case)
            error = ET.SubElement(testcase, "error", {"message": _safe(message)})
            error.text = _safe(detail)
        elif case.status == "skipped":
            ET.SubElement(testcase, "skipped", {"message": _safe(case.skipped_reason or "skipped")})

    return ET.ElementTree(suites)


def write_junit_report(result: RunResult, path: Path) -> Path:
    tree = build_junit_tree(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def _difference_text(case: CaseResult) -> tuple[str, str]:
    error_diffs = [d for d in case.differences if d.severity == "error"]
    shown = error_diffs[:20]
    message = f"{len(error_diffs)} behavioral difference(s)"
    lines = [
        f"[{d.severity}] {d.category}{f' @ {d.path}' if d.path else ''}: {d.message}" for d in shown
    ]
    for diff in shown:
        lines.append(
            f"    baseline={_render(diff.baseline_value)} candidate={_render(diff.candidate_value)}"
        )
    if len(error_diffs) > len(shown):
        lines.append(f"    … {len(error_diffs) - len(shown)} more difference(s) not shown")
    return message, "\n".join(lines)


def _failure_text(case: CaseResult) -> tuple[str, str]:
    lines: list[str] = []
    for label, snapshot in (("baseline", case.baseline), ("candidate", case.candidate)):
        if snapshot is None:
            continue
        if snapshot.error_kind in ("transport", "timeout"):
            lines.append(
                f"{label}: {snapshot.error_kind} — {snapshot.transport_error or 'no detail'}"
            )
        else:
            lines.append(f"{label}: status={snapshot.status_code}")
    message = "infrastructure failure (not a behavioral difference)"
    return message, "\n".join(lines)


def _render(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 300 else text[:300] + "…"
