"""Command-line interface: ``apiparity validate`` and ``apiparity compare``.

Exit-code contract (documented in ``--help`` and README):

* ``0`` — every enabled case compared and matched;
* ``1`` — one or more behavioral mismatches or configured failures;
* ``2`` — invalid configuration/corpus, safety refusal, target initialization
  failure, report-write failure, or any other problem that prevented a valid
  comparison.

``main()`` returns the exit code; it never calls :func:`sys.exit` itself (the
console-script wrapper and ``__main__`` do).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from apiparity import __version__
from apiparity.config import _APP_SPEC_RE, load_config_file, merge_cli_over_file
from apiparity.corpus import load_corpus
from apiparity.exceptions import ApiParityError, ConfigError
from apiparity.models import AppConfig, CaseResult, LifespanMode, TargetConfig, TargetKind
from apiparity.redaction import sanitize_exception_message
from apiparity.reports import render_terminal, write_reports
from apiparity.runner import run_comparison
from apiparity.safety import evaluate_safety

__all__ = ["EXIT_ERROR", "EXIT_MISMATCH", "EXIT_OK", "build_parser", "main"]

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_ERROR = 2

_STATUS_LABEL = {
    "match": "match",
    "mismatch": "MISMATCH",
    "failed": "FAILED",
    "skipped": "skipped",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apiparity",
        description=(
            "Behavioral parity checker: replay one request corpus against a baseline and a "
            "candidate implementation and report exactly where behavior differs."
        ),
        epilog=(
            "exit codes: 0 = all required comparisons passed, 1 = behavioral mismatches or "
            "configured failures found, 2 = invalid configuration, safety refusal, target "
            "initialization failure, or another execution problem."
        ),
    )
    parser.add_argument("--version", action="version", version=f"apiparity {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="{validate,compare}")

    validate = subparsers.add_parser(
        "validate",
        help="validate configuration, corpus, and safety posture without sending requests",
    )
    _add_common_arguments(validate)
    _add_target_arguments(validate)
    _add_safety_arguments(validate)

    compare = subparsers.add_parser(
        "compare",
        help="run the corpus against both implementations and produce reports",
    )
    _add_common_arguments(compare)
    _add_target_arguments(compare)
    _add_safety_arguments(compare)
    compare.add_argument(
        "--output",
        "-o",
        default="build/apiparity",
        metavar="DIR",
        help="directory for report.json and junit.xml (default: %(default)s)",
    )
    compare.add_argument(
        "--include-bodies",
        action="store_true",
        help="include redacted JSON body previews in report.json (off by default)",
    )
    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--corpus",
        required=True,
        metavar="PATH",
        help="request corpus file (.yaml/.yml/.json)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="project configuration file (.yaml/.yml/.json)",
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="per-case detail including snapshot metadata",
    )
    verbosity.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="only errors and the final status line",
    )


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--old-url", metavar="URL", help="baseline HTTP endpoint")
    parser.add_argument("--new-url", metavar="URL", help="candidate HTTP endpoint")
    parser.add_argument(
        "--old-app",
        metavar="MODULE:ATTR",
        help="baseline in-process application (e.g. legacy_flask.app:app)",
    )
    parser.add_argument(
        "--new-app",
        metavar="MODULE:ATTR",
        help="candidate in-process application (e.g. migrated_fastapi.app:app)",
    )
    parser.add_argument(
        "--old-kind",
        choices=("http", "wsgi", "asgi"),
        help="baseline target type (required with --old-app)",
    )
    parser.add_argument(
        "--new-kind",
        choices=("http", "wsgi", "asgi"),
        help="candidate target type (required with --new-app)",
    )
    parser.add_argument(
        "--old-factory",
        action="store_true",
        help="call the object referenced by --old-app as an application factory",
    )
    parser.add_argument(
        "--new-factory",
        action="store_true",
        help="call the object referenced by --new-app as an application factory",
    )
    parser.add_argument(
        "--old-lifespan",
        choices=("auto", "required", "off"),
        help=(
            "baseline ASGI lifespan policy: auto = skip lifespan-less apps but fail on "
            "startup errors, required = demand a lifespan protocol, off = never run it "
            "(default: auto; only with --old-kind asgi)"
        ),
    )
    parser.add_argument(
        "--new-lifespan",
        choices=("auto", "required", "off"),
        help=("candidate ASGI lifespan policy (default: auto; only with --new-kind asgi)"),
    )


def _add_safety_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-mutating-requests",
        action="store_true",
        help="permit POST/PUT/PATCH/DELETE cases (cannot be rolled back)",
    )
    parser.add_argument(
        "--allow-remote-targets",
        action="store_true",
        help="permit HTTP targets outside loopback",
    )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "validate":
            return _command_validate(args)
        return _command_compare(args)
    except ApiParityError as exc:
        _print_error(sanitize_exception_message(str(exc)))
        return EXIT_ERROR
    except KeyboardInterrupt:
        _print_error("interrupted")
        return EXIT_ERROR
    except Exception as exc:
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        detail = sanitize_exception_message(f"{type(exc).__name__}: {exc}")
        _print_error(f"unexpected internal error: {detail}")
        return EXIT_ERROR


def _command_validate(args: argparse.Namespace) -> int:
    config = _load_merged_config(args)
    corpus = load_corpus(args.corpus)
    baseline = _resolve_side_target(args, "old")
    candidate = _resolve_side_target(args, "new")
    baseline = baseline or config.old_target
    candidate = candidate or config.new_target

    sides: list[tuple[str, TargetConfig]] = []
    if baseline is not None:
        sides.append(("baseline", replace(baseline, label="baseline")))
    if candidate is not None:
        sides.append(("candidate", replace(candidate, label="candidate")))

    if sides:
        report = evaluate_safety(corpus.cases, sides, config.safety)
        print(report.render())
    else:
        print("targets not configured — safety evaluation skipped")
        print("  (pass --old-* / --new-* to evaluate target guards in dry-run mode)")

    enabled = len(corpus.enabled_cases)
    print(
        f"corpus OK: {len(corpus.cases)} case(s), {enabled} enabled, "
        f"{len(corpus.cases) - enabled} disabled"
    )
    print(f"config OK: {config.source_path or 'built-in defaults only'}")
    return EXIT_OK


def _command_compare(args: argparse.Namespace) -> int:
    config = _load_merged_config(args)
    corpus = load_corpus(args.corpus)

    baseline = _resolve_side_target(args, "old") or config.old_target
    candidate = _resolve_side_target(args, "new") or config.new_target
    if baseline is None or candidate is None:
        raise ConfigError(
            "both a baseline and a candidate target are required: pass --old-url/--old-app "
            "and --new-url/--new-app (or set targets.old/targets.new in the config file)"
        )
    baseline = replace(baseline, label="baseline")
    candidate = replace(candidate, label="candidate")

    safety_report = evaluate_safety(
        corpus.cases,
        [("baseline", baseline), ("candidate", candidate)],
        config.safety,
    )
    print(safety_report.render())
    print()

    on_case_done = None if args.quiet else _progress_printer()

    result = asyncio.run(
        run_comparison(
            corpus=corpus,
            config=config,
            old_target=baseline,
            new_target=candidate,
            on_case_done=on_case_done,
        )
    )

    try:
        written = write_reports(
            result,
            Path(args.output),
            reporting=config.reporting,
            redaction=config.redaction,
            include_body_previews=config.reporting.include_body_previews or args.include_bodies,
        )
    except OSError as exc:
        raise ConfigError(f"failed to write reports to {args.output}: {exc}") from exc

    if config.reporting.terminal:
        print()
        print(
            render_terminal(
                result,
                verbose=args.verbose,
                quiet=args.quiet,
                redaction=config.redaction,
                show_reports=[str(path) for path in written],
            )
        )
    return result.summary.exit_code


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load_merged_config(args: argparse.Namespace) -> AppConfig:
    config = load_config_file(args.config) if args.config else AppConfig()
    overrides: dict[str, Any] = {
        "allow_mutating_requests": bool(getattr(args, "allow_mutating_requests", False)),
        "allow_remote_targets": bool(getattr(args, "allow_remote_targets", False)),
    }
    return merge_cli_over_file(config, overrides)


def _resolve_side_target(args: argparse.Namespace, side: str) -> TargetConfig | None:
    """Build a target config from ``--{side}-*`` flags (``None`` when absent)."""
    url: str | None = getattr(args, f"{side}_url")
    app: str | None = getattr(args, f"{side}_app")
    kind: str | None = getattr(args, f"{side}_kind")
    factory: bool = bool(getattr(args, f"{side}_factory"))
    lifespan: str | None = getattr(args, f"{side}_lifespan")
    label = "baseline" if side == "old" else "candidate"

    if url and app:
        raise ConfigError(f"--{side}-url and --{side}-app are mutually exclusive")
    if not url and not app:
        if kind is not None:
            raise ConfigError(f"--{side}-kind requires --{side}-url or --{side}-app")
        if factory:
            raise ConfigError(f"--{side}-factory requires --{side}-app")
        if lifespan is not None:
            raise ConfigError(f"--{side}-lifespan requires --{side}-app")
        return None

    if url:
        if kind in ("wsgi", "asgi"):
            raise ConfigError(
                f"--{side}-kind {kind!r} cannot be combined with --{side}-url "
                "(URL targets are always http)"
            )
        if factory:
            raise ConfigError(f"--{side}-factory only applies to --{side}-app targets")
        if lifespan is not None:
            raise ConfigError(f"--{side}-lifespan only applies to --{side}-app targets")
        return TargetConfig(kind="http", url=url, verify_tls=True, label=label)

    assert app is not None
    if kind is None:
        raise ConfigError(
            f"--{side}-kind is required with --{side}-app: the application type "
            "(wsgi or asgi) cannot be detected reliably"
        )
    if kind == "http":
        raise ConfigError(f"--{side}-kind http requires --{side}-url instead of --{side}-app")
    if not _APP_SPEC_RE.match(app):
        raise ConfigError(f"--{side}-app must look like 'package.module:attribute', got {app!r}")
    if lifespan is not None and kind != "asgi":
        raise ConfigError(f"--{side}-lifespan only applies to --{side}-kind asgi")
    return TargetConfig(
        kind=cast(TargetKind, kind),
        app=app,
        factory=factory,
        lifespan=cast(LifespanMode, lifespan or "auto"),
        label=label,
    )


def _progress_printer() -> Callable[[CaseResult, int, int], None]:
    def on_case_done(case: CaseResult, index: int, total: int) -> None:
        print(f"  [{index:02d}/{total:02d}] {case.case_id:<30} {_STATUS_LABEL[case.status]}")

    return on_case_done


def _print_error(message: str) -> None:
    print(f"apiparity: error: {message}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
