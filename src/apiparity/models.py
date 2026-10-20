"""Typed data models shared across apiparity.

These are plain stdlib dataclasses (no pydantic) to keep the runtime dependency
budget at ``httpx`` + ``PyYAML`` only. Validation lives in :mod:`apiparity.corpus`
and :mod:`apiparity.config`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TargetKind = Literal["http", "wsgi", "asgi"]
LifespanMode = Literal["auto", "required", "off"]
ErrorKind = Literal["none", "transport", "timeout", "decode", "too_large"]
CaseStatus = Literal["match", "mismatch", "failed", "skipped"]
Severity = Literal["error", "warning", "info"]
TimingMode = Literal["off", "warn", "fail"]

DifferenceCategory = Literal[
    "status_code",
    "header_added",
    "header_removed",
    "header_value",
    "body_type",
    "json_key_added",
    "json_key_removed",
    "json_value",
    "json_type",
    "text_body",
    "binary_body",
    "transport_error",
    "timeout",
    "response_too_large",
    "normalization_note",
    "timing",
]

KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"})

#: Methods replayable without the mutating opt-in (the HTTP "safe" set the
#: safety guard treats as read-only; TRACE is excluded deliberately — it is
#: never emitted by real clients and stays behind the opt-in).
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class FileUpload:
    """A multipart file part; ``path`` is corpus-root relative."""

    field: str
    path: str
    content_type: str | None = None


@dataclass(frozen=True)
class RequestCase:
    """One validated logical request replayed against both targets."""

    id: str
    method: str
    path: str
    description: str | None = None
    query: dict[str, list[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    json_body: Any | None = None
    #: True when the corpus specified a ``json`` field — ``json: null`` is an
    #: explicit JSON-null body, distinct from "no body at all".
    has_json_body: bool = False
    data: str | None = None
    files: tuple[FileUpload, ...] = ()
    timeout_seconds: float | None = None
    enabled: bool = True
    compare_headers: bool | None = None
    compare_body: bool | None = None

    def query_pairs(self) -> list[tuple[str, str]]:
        """Flatten query params preserving repeated values and key order."""
        return [(key, value) for key, values in self.query.items() for value in values]


@dataclass(frozen=True)
class ResponseSnapshot:
    """Normalized observation of one response (or transport failure)."""

    status_code: int | None
    headers: tuple[tuple[str, tuple[str, ...]], ...]
    content_type: str | None
    body_bytes: bytes | None
    body_truncated: bool
    body_size: int
    duration_ms: float
    transport_error: str | None
    error_kind: ErrorKind = "none"

    def header_map(self) -> dict[str, tuple[str, ...]]:
        return {name: tuple(values) for name, values in self.headers}

    @property
    def ok(self) -> bool:
        return self.error_kind == "none" and self.status_code is not None


@dataclass(frozen=True)
class Difference:
    """One structured behavioral (or infrastructure) difference."""

    category: DifferenceCategory
    severity: Severity
    message: str
    case_id: str
    path: str | None = None
    baseline_value: Any = None
    candidate_value: Any = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class CaseResult:
    """Outcome for a single request case."""

    case_id: str
    status: CaseStatus
    differences: tuple[Difference, ...] = ()
    baseline: ResponseSnapshot | None = None
    candidate: ResponseSnapshot | None = None
    skipped_reason: str | None = None
    duration_ms: float = 0.0

    @property
    def error_count(self) -> int:
        return sum(1 for diff in self.differences if diff.severity == "error")


@dataclass(frozen=True)
class RunSummary:
    """Aggregate counters for a run."""

    total: int
    matched: int
    mismatched: int
    failed: int
    skipped: int
    started_at: str
    finished_at: str
    exit_code: int


@dataclass(frozen=True)
class RunResult:
    """Complete, report-ready result of one comparison run."""

    summary: RunSummary
    cases: tuple[CaseResult, ...]
    target_descriptions: dict[str, str]
    corpus_path: str
    config_fingerprint: str
    normalization_rules_applied: dict[str, Any]
    tool_version: str
    schema_version: int = 1

    @property
    def mismatches(self) -> tuple[CaseResult, ...]:
        return tuple(c for c in self.cases if c.status == "mismatch")

    @property
    def result_kind(self) -> Literal["pass", "fail", "error"]:
        if self.summary.exit_code == 0:
            return "pass"
        if self.summary.exit_code == 1:
            return "fail"
        return "error"


# --------------------------------------------------------------------------- #
# Configuration models
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TargetConfig:
    """How to reach one implementation (URL or imported app)."""

    kind: TargetKind = "http"
    url: str | None = None
    app: str | None = None
    factory: bool = False
    verify_tls: bool = True
    timeout_seconds: float | None = None  # None → effective config/corpus default
    lifespan: LifespanMode = "auto"  # ASGI only: lifespan policy (see asgi_target)
    label: str = "target"

    def describe(self) -> str:
        """Credential-free identifier used in reports (userinfo masked,
        sensitive query-parameter values redacted)."""
        if self.kind == "http" and self.url:
            # Local import: apiparity.redaction imports models at module level.
            from apiparity.redaction import redact_url

            return redact_url(self.url)
        suffix = " (factory)" if self.factory else ""
        return f"{self.kind}:{self.app}{suffix}"


@dataclass(frozen=True)
class SafetyConfig:
    allow_mutating_requests: bool = False
    allow_remote_targets: bool = False


@dataclass(frozen=True)
class ComparisonConfig:
    compare_status: bool = True
    compare_headers: bool = True
    compare_body: bool = True
    max_differences_per_case: int = 100
    response_size_limit_bytes: int = 5 * 1024 * 1024
    timing_mode: TimingMode = "off"
    timing_max_ms: float | None = None


@dataclass(frozen=True)
class NormalizationConfig:
    ignored_headers: tuple[str, ...] = ("date", "content-length", "server", "connection")
    ignored_json_pointers: tuple[str, ...] = ()
    whitespace_text: bool = False
    array_order: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RedactionConfig:
    sensitive_headers: tuple[str, ...] = (
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
    )
    sensitive_json_pointers: tuple[str, ...] = ("/credentials/token",)
    header_name_substrings: tuple[str, ...] = (
        "token",
        "secret",
        "password",
        "apikey",
        "api-key",
        "credential",
    )


@dataclass(frozen=True)
class ReportingConfig:
    json: bool = True
    junit_xml: bool = True
    terminal: bool = True
    include_body_previews: bool = False


@dataclass(frozen=True)
class AppConfig:
    """Effective project configuration (unresolved sentinels are ``None``)."""

    timeout_seconds: float | None = None
    follow_redirects: bool | None = None
    compare_status: bool | None = None
    compare_headers: bool | None = None
    compare_body: bool | None = None
    max_differences_per_case: int | None = None
    response_size_limit_bytes: int | None = None
    timing_mode: TimingMode | None = None
    timing_max_ms: float | None = None
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    normalization: NormalizationConfig = field(default_factory=NormalizationConfig)
    redaction: RedactionConfig = field(default_factory=RedactionConfig)
    reporting: ReportingConfig = field(default_factory=ReportingConfig)
    old_target: TargetConfig | None = None
    new_target: TargetConfig | None = None
    source_path: str | None = None

    def effective_comparison(
        self, corpus_defaults: dict[str, Any] | None = None
    ) -> ComparisonConfig:
        """Resolve comparison settings: config file > corpus defaults > built-ins."""
        corpus = corpus_defaults or {}

        def pick(file_val: Any, corpus_key: str, builtin: Any) -> Any:
            if file_val is not None:
                return file_val
            if corpus_key in corpus:
                return corpus[corpus_key]
            return builtin

        return ComparisonConfig(
            compare_status=bool(pick(self.compare_status, "compare_status", True)),
            compare_headers=bool(pick(self.compare_headers, "compare_headers", True)),
            compare_body=bool(pick(self.compare_body, "compare_body", True)),
            max_differences_per_case=int(
                pick(self.max_differences_per_case, "max_differences_per_case", 100)
            ),
            response_size_limit_bytes=int(
                pick(
                    self.response_size_limit_bytes,
                    "response_size_limit_bytes",
                    5 * 1024 * 1024,
                )
            ),
            timing_mode=pick(self.timing_mode, "timing_mode", "off"),
            timing_max_ms=pick(self.timing_max_ms, "timing_max_ms", None),
        )

    def effective_timeout(self, corpus_defaults: dict[str, Any] | None = None) -> float:
        if self.timeout_seconds is not None:
            return self.timeout_seconds
        corpus = corpus_defaults or {}
        if "timeout_seconds" in corpus:
            return float(corpus["timeout_seconds"])
        return 10.0
