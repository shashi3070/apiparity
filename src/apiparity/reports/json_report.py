"""Canonical machine-readable JSON report (schema v1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apiparity.models import RedactionConfig, ResponseSnapshot, RunResult
from apiparity.redaction import redact_headers, redact_json

__all__ = ["JSON_SCHEMA_VERSION", "build_json_report", "write_json_report"]

JSON_SCHEMA_VERSION = 1
_PREVIEW_CHARS = 500


def build_json_report(
    result: RunResult,
    *,
    redaction: RedactionConfig,
    include_body_previews: bool = False,
) -> dict[str, Any]:
    """Build the full report document (pure — no file I/O, no secrets)."""
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "tool_version": result.tool_version,
        "started_at": result.summary.started_at,
        "finished_at": result.summary.finished_at,
        "result": result.result_kind,
        "exit_code": result.summary.exit_code,
        "targets": dict(result.target_descriptions),
        "corpus": {"path": result.corpus_path},
        "config": {
            "fingerprint": result.config_fingerprint,
            "rules_applied": result.normalization_rules_applied,
        },
        "summary": {
            "total": result.summary.total,
            "matched": result.summary.matched,
            "mismatched": result.summary.mismatched,
            "failed": result.summary.failed,
            "skipped": result.summary.skipped,
        },
        "cases": [
            _case_json(case, redaction=redaction, include_body_previews=include_body_previews)
            for case in result.cases
        ],
    }


def write_json_report(
    result: RunResult,
    path: Path,
    *,
    redaction: RedactionConfig,
    include_body_previews: bool = False,
) -> Path:
    document = build_json_report(
        result, redaction=redaction, include_body_previews=include_body_previews
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    return path


def _case_json(
    case: Any, *, redaction: RedactionConfig, include_body_previews: bool
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": case.case_id,
        "status": case.status,
        "duration_ms": case.duration_ms,
        "differences": [_difference_json(diff) for diff in case.differences],
    }
    if case.skipped_reason:
        payload["skipped_reason"] = case.skipped_reason
    if case.baseline is not None:
        payload["baseline"] = _snapshot_json(
            case.baseline, redaction=redaction, include_body_previews=include_body_previews
        )
    if case.candidate is not None:
        payload["candidate"] = _snapshot_json(
            case.candidate, redaction=redaction, include_body_previews=include_body_previews
        )
    return payload


def _difference_json(diff: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "category": diff.category,
        "severity": diff.severity,
        "message": diff.message,
    }
    if diff.path is not None:
        payload["path"] = diff.path
    payload["baseline_value"] = diff.baseline_value
    payload["candidate_value"] = diff.candidate_value
    if diff.details:
        payload["details"] = diff.details
    return payload


def _snapshot_json(
    snapshot: ResponseSnapshot,
    *,
    redaction: RedactionConfig,
    include_body_previews: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status_code": snapshot.status_code,
        "content_type": snapshot.content_type,
        "headers": {
            name: list(values)
            for name, values in redact_headers(snapshot.header_map(), redaction).items()
        },
        "body_size": snapshot.body_size,
        "body_truncated": snapshot.body_truncated,
        "duration_ms": snapshot.duration_ms,
        "error_kind": snapshot.error_kind,
    }
    if snapshot.transport_error:
        payload["transport_error"] = snapshot.transport_error
    if include_body_previews and snapshot.body_bytes is not None:
        preview = _body_preview(snapshot.body_bytes, snapshot.content_type, redaction)
        if preview is not None:
            payload["body_preview"] = preview
    return payload


def _body_preview(body: bytes, content_type: str | None, redaction: RedactionConfig) -> str | None:
    """Only JSON bodies are previewable — text/binary previews cannot be redacted safely."""
    if content_type and "json" not in content_type.lower():
        return None
    try:
        text = body.decode("utf-8")
        parsed = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        return None
    redacted = redact_json(parsed, redaction)
    rendered = json.dumps(redacted, indent=2, ensure_ascii=False, default=str)
    if len(rendered) > _PREVIEW_CHARS:
        return rendered[:_PREVIEW_CHARS] + "…"
    return rendered
