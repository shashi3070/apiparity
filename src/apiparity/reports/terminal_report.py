"""Human-readable terminal summary.

Differences are grouped per request case and rendered path-first; full
responses are never dumped. All displayed values come from already-redacted
``Difference`` objects; snapshot headers are redacted again on the way out
(defense in depth).
"""

from __future__ import annotations

from apiparity.models import Difference, RedactionConfig, RunResult
from apiparity.redaction import redact_headers

__all__ = ["format_difference", "render_terminal"]

_MAX_DIFFERENCES_SHOWN = 5
_VALUE_LIMIT = 160

_STATUS_LABEL = {
    "match": "MATCH",
    "mismatch": "MISMATCH",
    "failed": "FAILED",
    "skipped": "SKIPPED",
}


def render_terminal(
    result: RunResult,
    *,
    verbose: bool = False,
    quiet: bool = False,
    redaction: RedactionConfig,
    show_reports: list[str] | None = None,
) -> str:
    """Render the console report (never includes unredacted values)."""
    summary = result.summary
    lines: list[str] = []

    if quiet:
        lines.append(_final_line(result))
        if show_reports:
            lines.append("reports: " + ", ".join(show_reports))
        return "\n".join(lines)

    lines.append(f"apiparity {result.tool_version} — behavioral comparison")
    lines.append(f"  baseline : {result.target_descriptions.get('baseline', '?')}")
    lines.append(f"  candidate: {result.target_descriptions.get('candidate', '?')}")
    lines.append(f"  corpus   : {result.corpus_path} ({summary.total} cases)")
    lines.append("")

    width = max((len(case.case_id) for case in result.cases), default=4)
    for index, case in enumerate(result.cases, start=1):
        if not verbose and case.status == "match":
            continue
        label = _STATUS_LABEL[case.status]
        header = f"  [{index:02d}/{summary.total:02d}] {case.case_id:<{width}}  {label}"
        if case.skipped_reason:
            header += f"  ({case.skipped_reason})"
        lines.append(header)

        if case.status in ("mismatch", "failed") or verbose:
            limit = len(case.differences) if verbose else _MAX_DIFFERENCES_SHOWN
            for diff in case.differences[:limit]:
                lines.append(format_difference(diff, indent=6))
            remaining = len(case.differences) - limit
            if remaining > 0:
                lines.append(f"      … {remaining} more difference(s) — see report.json")
            if verbose:
                lines.extend(_snapshot_lines(case, redaction))

    lines.append("")
    lines.append(
        f"  totals: {summary.total} case(s) · {summary.matched} matched · "
        f"{summary.mismatched} mismatched · {summary.failed} failed · "
        f"{summary.skipped} skipped"
    )
    if show_reports:
        lines.append("  reports: " + ", ".join(show_reports))
    lines.append("")
    lines.append("  " + _final_line(result))
    return "\n".join(lines)


def format_difference(diff: Difference, *, indent: int = 6) -> str:
    """One path-focused difference line (grouped output stays scannable)."""
    pad = " " * indent
    location = f"{diff.path}: " if diff.path else ""
    line = f"{pad}[{diff.severity}] {diff.category} — {location}{diff.message}"
    categories_with_values = ("status_code", "text_body", "binary_body", "timing")
    if (diff.severity != "info" and diff.path is not None) or (
        diff.category in categories_with_values
    ):
        line += (
            f"\n{pad}    baseline={_render(diff.baseline_value)}"
            f"  candidate={_render(diff.candidate_value)}"
        )
    return line


def _snapshot_lines(case: object, redaction: RedactionConfig) -> list[str]:
    lines: list[str] = []
    for label in ("baseline", "candidate"):
        snapshot = getattr(case, label, None)
        if snapshot is None:
            continue
        if snapshot.error_kind in ("transport", "timeout"):
            lines.append(f"      {label}: {snapshot.error_kind} — {snapshot.transport_error}")
            continue
        headers = redact_headers(snapshot.header_map(), redaction)
        header_text = ", ".join(
            f"{name}={list(values)}" for name, values in list(headers.items())[:6]
        )
        lines.append(
            f"      {label}: status={snapshot.status_code} "
            f"bytes={snapshot.body_size} duration={snapshot.duration_ms:.1f}ms"
        )
        if header_text:
            lines.append(f"        headers: {header_text}")
    return lines


def _render(value: object) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float, bool)) or value is None:
        text = repr(value)
    else:
        text = str(value)
    text = text.replace("\n", "\\n")
    return text if len(text) <= _VALUE_LIMIT else text[: _VALUE_LIMIT - 1] + "…"


def _final_line(result: RunResult) -> str:
    exit_code = result.summary.exit_code
    if exit_code == 0:
        status = "PASS"
    elif exit_code == 1:
        status = "FAIL — behavioral differences detected"
    else:
        status = "ERROR — no valid comparison was produced"
    return f"RESULT: {status} (exit {exit_code})"
