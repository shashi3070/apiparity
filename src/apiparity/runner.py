"""Sequential comparison orchestration and the public execution API.

Flow (see REQUIREMENTS.md §4): validate → interpolate → safety → initialize
targets → execute corpus in order → normalize → compare → summarize → exit code
→ deterministic cleanup (always, even on failure).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from apiparity.comparison import compare_snapshots
from apiparity.config import config_fingerprint
from apiparity.corpus import Corpus, load_corpus
from apiparity.exceptions import ConfigError, TargetInitError
from apiparity.models import (
    AppConfig,
    CaseResult,
    CaseStatus,
    ComparisonConfig,
    Difference,
    NormalizationConfig,
    RedactionConfig,
    RequestCase,
    RunResult,
    RunSummary,
    TargetConfig,
)
from apiparity.normalization import describe_rules, normalize_snapshot
from apiparity.safety import SafetyReport, evaluate_safety
from apiparity.targets.base import Target
from apiparity.targets.loader import resolve_target

__all__ = ["compare", "compare_sync", "compute_exit_code", "run_comparison"]

#: Invoked after each executed case as ``callback(result, index, total)``.
CaseCallback = Callable[[CaseResult, int, int], None]


def compute_exit_code(cases: Sequence[CaseResult]) -> int:
    """0 = everything matched (skips allowed); 1 = mismatch or failed execution."""
    for case in cases:
        if case.status in ("mismatch", "failed"):
            return 1
    return 0


def _merge_normalization(config: AppConfig, corpus: Corpus) -> NormalizationConfig:
    """Config-file rules first, corpus rules appended (union, order preserved)."""
    file_rules = config.normalization
    corpus_rules = corpus.normalization
    ignored_headers = tuple(
        dict.fromkeys(
            [
                *(h.lower() for h in file_rules.ignored_headers),
                *(h.lower() for h in corpus_rules.ignored_headers),
            ]
        )
    )
    ignored_pointers = tuple(
        dict.fromkeys([*file_rules.ignored_json_pointers, *corpus_rules.ignored_json_pointers])
    )
    array_order = {**file_rules.array_order, **corpus_rules.array_order}
    return NormalizationConfig(
        ignored_headers=ignored_headers,
        ignored_json_pointers=ignored_pointers,
        whitespace_text=file_rules.whitespace_text or corpus_rules.whitespace_text,
        array_order=array_order,
    )


async def run_comparison(
    *,
    corpus: Corpus,
    config: AppConfig,
    old_target: TargetConfig | None = None,
    new_target: TargetConfig | None = None,
    env: Mapping[str, str] | None = None,
    on_case_done: CaseCallback | None = None,
) -> RunResult:
    """Execute the corpus against both targets and return structured results.

    Raises before any request is sent for: invalid targets (:class:`ConfigError`),
    safety refusals (:class:`SafetyError`), and target initialization failures
    (:class:`TargetInitError`). ``env`` is accepted for API symmetry with
    :func:`apiparity.corpus.load_corpus` (variables are resolved at corpus load).
    """
    del env  # variables are resolved while the corpus loads
    baseline_cfg = old_target or config.old_target
    candidate_cfg = new_target or config.new_target
    if baseline_cfg is None or candidate_cfg is None:
        raise ConfigError(
            "both a baseline and a candidate target are required "
            "(--old-url/--old-app and --new-url/--new-app, or targets.<side> in the config file)"
        )

    comparison = config.effective_comparison(corpus.defaults)
    normalization = _merge_normalization(config, corpus)

    safety_report: SafetyReport = evaluate_safety(
        corpus.cases,
        [("baseline", baseline_cfg), ("candidate", candidate_cfg)],
        config.safety,
    )

    baseline = resolve_target(
        baseline_cfg,
        side="baseline",
        size_limit=comparison.response_size_limit_bytes,
        follow_redirects=bool(config.follow_redirects or corpus.defaults.get("follow_redirects")),
        corpus_dir=corpus.directory,
        default_timeout=config.effective_timeout(corpus.defaults),
    )
    candidate = resolve_target(
        candidate_cfg,
        side="candidate",
        size_limit=comparison.response_size_limit_bytes,
        follow_redirects=bool(config.follow_redirects or corpus.defaults.get("follow_redirects")),
        corpus_dir=corpus.directory,
        default_timeout=config.effective_timeout(corpus.defaults),
    )

    started_at = _utc_now()
    results: list[CaseResult] = []
    total = len(corpus.cases)

    try:
        await _startup_targets(baseline, candidate)
        for index, case in enumerate(corpus.cases, start=1):
            result = await _run_case(
                case,
                baseline=baseline,
                candidate=candidate,
                comparison=comparison,
                normalization=normalization,
                redaction=config.redaction,
            )
            results.append(result)
            if on_case_done is not None:
                on_case_done(result, index, total)
    finally:
        await _shutdown_targets(baseline, candidate)

    finished_at = _utc_now()
    summary = _build_summary(results, started_at, finished_at)

    return RunResult(
        summary=summary,
        cases=tuple(results),
        target_descriptions={
            "baseline": baseline.describe(),
            "candidate": candidate.describe(),
        },
        corpus_path=str(corpus.source_path),
        config_fingerprint=config_fingerprint(config),
        normalization_rules_applied={
            "normalization": describe_rules(normalization),
            "comparison": {
                "compare_status": comparison.compare_status,
                "compare_headers": comparison.compare_headers,
                "compare_body": comparison.compare_body,
                "max_differences_per_case": comparison.max_differences_per_case,
                "response_size_limit_bytes": comparison.response_size_limit_bytes,
                "timing_mode": comparison.timing_mode,
                "timing_max_ms": comparison.timing_max_ms,
            },
            "safety": {
                "allow_mutating_requests": config.safety.allow_mutating_requests,
                "allow_remote_targets": config.safety.allow_remote_targets,
                "mutating_case_ids": list(safety_report.mutating_case_ids),
                "remote_targets": list(safety_report.remote_targets),
            },
        },
        tool_version=_version(),
    )


def compare_sync(
    *,
    corpus: str | Path | Corpus,
    config: AppConfig | None = None,
    old_target: TargetConfig | None = None,
    new_target: TargetConfig | None = None,
    env: Mapping[str, str] | None = None,
    on_case_done: CaseCallback | None = None,
) -> RunResult:
    """Synchronous convenience wrapper around :func:`compare`."""
    return asyncio.run(
        compare(
            corpus=corpus,
            config=config,
            old_target=old_target,
            new_target=new_target,
            env=env,
            on_case_done=on_case_done,
        )
    )


async def compare(
    *,
    corpus: str | Path | Corpus,
    config: AppConfig | None = None,
    old_target: TargetConfig | None = None,
    new_target: TargetConfig | None = None,
    env: Mapping[str, str] | None = None,
    on_case_done: CaseCallback | None = None,
) -> RunResult:
    """Load (if needed) and compare a request corpus against two targets.

    Raises:
        CorpusError: invalid corpus content.
        ConfigError: missing/invalid configuration or target specification.
        SafetyError: mutating or remote guard refused the run.
        TargetInitError: either target failed to initialize.
    """
    resolved_corpus = corpus if isinstance(corpus, Corpus) else load_corpus(corpus, env=env)
    resolved_config = config if config is not None else AppConfig()
    return await run_comparison(
        corpus=resolved_corpus,
        config=resolved_config,
        old_target=old_target,
        new_target=new_target,
        env=env,
        on_case_done=on_case_done,
    )


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


async def _startup_targets(baseline: Target, candidate: Target) -> None:
    """Start baseline first; any failure tears down whatever started."""
    started: list[Target] = []
    try:
        await baseline.startup()
        started.append(baseline)
        await candidate.startup()
        started.append(candidate)
    except TargetInitError:
        await _shutdown_many(started)
        raise
    except Exception as exc:
        await _shutdown_many(started)
        side = "candidate" if baseline in started else "baseline"
        raise TargetInitError(side, f"unexpected startup failure: {type(exc).__name__}") from exc


async def _shutdown_many(targets: Sequence[Target]) -> None:
    for target in reversed(list(targets)):
        try:
            await target.shutdown()
        except Exception:
            pass


async def _shutdown_targets(baseline: Target, candidate: Target) -> None:
    await _shutdown_many([candidate, baseline])


async def _run_case(
    case: RequestCase,
    *,
    baseline: Target,
    candidate: Target,
    comparison: ComparisonConfig,
    normalization: NormalizationConfig,
    redaction: RedactionConfig,
) -> CaseResult:
    if not case.enabled:
        return CaseResult(
            case_id=case.id,
            status="skipped",
            skipped_reason="disabled in corpus (enabled: false)",
        )

    baseline_snapshot = await baseline.execute(case)
    candidate_snapshot = await candidate.execute(case)

    normalized_baseline = normalize_snapshot(baseline_snapshot, normalization)
    normalized_candidate = normalize_snapshot(candidate_snapshot, normalization)

    differences: tuple[Difference, ...] = tuple(
        compare_snapshots(
            normalized_baseline,
            normalized_candidate,
            case=case,
            comparison=comparison,
            normalization=normalization,
            redaction=redaction,
        )
    )

    infra_failed = any(
        snap.error_kind in ("transport", "timeout")
        for snap in (baseline_snapshot, candidate_snapshot)
    )
    if infra_failed:
        status: CaseStatus = "failed"
    elif any(diff.severity == "error" for diff in differences):
        status = "mismatch"
    else:
        status = "match"

    return CaseResult(
        case_id=case.id,
        status=status,
        differences=differences,
        baseline=normalized_baseline,
        candidate=normalized_candidate,
        duration_ms=round(normalized_baseline.duration_ms + normalized_candidate.duration_ms, 3),
    )


def _build_summary(cases: Sequence[CaseResult], started_at: str, finished_at: str) -> RunSummary:
    matched = sum(1 for case in cases if case.status == "match")
    mismatched = sum(1 for case in cases if case.status == "mismatch")
    failed = sum(1 for case in cases if case.status == "failed")
    skipped = sum(1 for case in cases if case.status == "skipped")
    return RunSummary(
        total=len(cases),
        matched=matched,
        mismatched=mismatched,
        failed=failed,
        skipped=skipped,
        started_at=started_at,
        finished_at=finished_at,
        exit_code=compute_exit_code(cases),
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _version() -> str:
    from apiparity import __version__

    return __version__
