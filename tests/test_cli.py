"""CLI tests: argparse surface, exit-code contract (0/1/2), and report output.

These drive ``apiparity.cli.main`` in-process (fast, no subprocess) plus one
subprocess check of the ``python -m apiparity`` entry point. Exit codes are
asserted exactly — they are a documented contract (REQUIREMENTS.md §14).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from apiparity.cli import EXIT_ERROR, EXIT_MISMATCH, EXIT_OK, main

FIXTURES = Path(__file__).parent / "fixtures"
CORPORA = FIXTURES / "corpora"
EQUIVALENT = str(CORPORA / "equivalent.yaml")
MUTATING = str(CORPORA / "mutating.yaml")
SECRETS = str(CORPORA / "secrets.yaml")

OLD_APP = "legacy_flask.app:app"
NEW_APP = "migrated_fastapi.app:app"
NEW_MISMATCH = "migrated_fastapi.app_mismatch:app"


def _app_args(
    old_app: str = OLD_APP,
    new_app: str = NEW_APP,
    old_kind: str = "wsgi",
    new_kind: str = "asgi",
) -> list[str]:
    return [
        "--old-app",
        old_app,
        "--old-kind",
        old_kind,
        "--new-app",
        new_app,
        "--new-kind",
        new_kind,
    ]


# --------------------------------------------------------------------------- #
# --version and parser surface
# --------------------------------------------------------------------------- #


def test_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == EXIT_OK
    assert "apiparity" in capsys.readouterr().out


def test_no_subcommand_is_an_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == EXIT_ERROR


def test_top_level_help_lists_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "exit codes" in out
    assert "validate" in out
    assert "compare" in out


def test_compare_help_lists_key_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["compare", "--help"])
    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "--allow-mutating-requests" in out
    assert "--output" in out
    assert "--include-bodies" in out


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #


def test_validate_equivalent_corpus_ok(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["validate", "--corpus", EQUIVALENT, *_app_args()])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "corpus OK" in out
    assert "Safety summary" in out


def test_validate_without_targets_still_checks_corpus(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["validate", "--corpus", EQUIVALENT])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "safety evaluation skipped" in out
    assert "corpus OK" in out


def test_validate_mutating_corpus_refused_without_opt_in(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["validate", "--corpus", MUTATING, *_app_args()])
    assert rc == EXIT_ERROR
    assert "mutating" in capsys.readouterr().err


def test_validate_mutating_corpus_ok_with_opt_in(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["validate", "--corpus", MUTATING, "--allow-mutating-requests", *_app_args()])
    assert rc == EXIT_OK
    assert "ALLOWED" in capsys.readouterr().out


def test_validate_invalid_corpus_is_exit_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\ncases: []\n", encoding="utf-8")
    rc = main(["validate", "--corpus", str(bad)])
    assert rc == EXIT_ERROR


def test_validate_missing_corpus_file_is_exit_2(tmp_path: Path) -> None:
    rc = main(["validate", "--corpus", str(tmp_path / "nope.yaml")])
    assert rc == EXIT_ERROR


def test_validate_remote_target_refused(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "validate",
            "--corpus",
            EQUIVALENT,
            "--old-url",
            "http://example.com",
            "--new-url",
            "http://127.0.0.1:9",
        ]
    )
    assert rc == EXIT_ERROR
    assert "loopback" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# target argument validation
# --------------------------------------------------------------------------- #


def test_url_and_app_are_mutually_exclusive() -> None:
    rc = main(
        [
            "validate",
            "--corpus",
            EQUIVALENT,
            "--old-url",
            "http://127.0.0.1:9",
            "--old-app",
            OLD_APP,
            "--old-kind",
            "wsgi",
        ]
    )
    assert rc == EXIT_ERROR


def test_kind_required_with_app() -> None:
    rc = main(["validate", "--corpus", EQUIVALENT, "--old-app", OLD_APP])
    assert rc == EXIT_ERROR


def test_kind_http_requires_url() -> None:
    rc = main(
        [
            "validate",
            "--corpus",
            EQUIVALENT,
            "--old-app",
            OLD_APP,
            "--old-kind",
            "http",
        ]
    )
    assert rc == EXIT_ERROR


def test_factory_requires_app() -> None:
    rc = main(
        [
            "validate",
            "--corpus",
            EQUIVALENT,
            "--old-url",
            "http://127.0.0.1:9",
            "--old-factory",
        ]
    )
    assert rc == EXIT_ERROR


def test_bad_app_spec_is_exit_2() -> None:
    rc = main(
        [
            "validate",
            "--corpus",
            EQUIVALENT,
            "--old-app",
            "not-a-spec",
            "--old-kind",
            "wsgi",
        ]
    )
    assert rc == EXIT_ERROR


def test_lifespan_requires_app() -> None:
    rc = main(
        [
            "validate",
            "--corpus",
            EQUIVALENT,
            "--old-url",
            "http://127.0.0.1:9",
            "--old-lifespan",
            "off",
        ]
    )
    assert rc == EXIT_ERROR


def test_lifespan_rejected_for_non_asgi_kind() -> None:
    rc = main(
        [
            "validate",
            "--corpus",
            EQUIVALENT,
            "--old-app",
            OLD_APP,
            "--old-kind",
            "wsgi",
            "--old-lifespan",
            "off",
        ]
    )
    assert rc == EXIT_ERROR


def test_lifespan_accepted_with_asgi_app(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["validate", "--corpus", EQUIVALENT, *_app_args(), "--new-lifespan", "off"])
    assert rc == EXIT_OK
    assert "corpus OK" in capsys.readouterr().out


def test_unknown_module_is_exit_2(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    # validate never imports the app (that happens at compare startup),
    # so the unknown-module failure surfaces on compare with exit 2.
    rc = main(
        [
            "compare",
            "--corpus",
            EQUIVALENT,
            "--output",
            str(tmp_path / "o"),
            *_app_args(old_app="nope.nope:app"),
        ]
    )
    assert rc == EXIT_ERROR
    assert "nope.nope" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #


def test_compare_equivalent_apps_exit_0_and_reports(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    rc = main(["compare", "--corpus", EQUIVALENT, "--output", str(outdir), "--quiet", *_app_args()])
    assert rc == EXIT_OK
    assert (outdir / "report.json").is_file()
    assert (outdir / "junit.xml").is_file()
    report = json.loads((outdir / "report.json").read_text(encoding="utf-8"))
    assert report["result"] == "pass"
    assert report["exit_code"] == 0
    assert report["summary"]["matched"] == 9


def test_compare_mismatch_exit_1_and_pointer_in_report(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    rc = main(
        [
            "compare",
            "--corpus",
            EQUIVALENT,
            "--output",
            str(outdir),
            "--quiet",
            *_app_args(new_app=NEW_MISMATCH),
        ]
    )
    assert rc == EXIT_MISMATCH
    report = json.loads((outdir / "report.json").read_text(encoding="utf-8"))
    assert report["result"] == "fail"
    assert report["exit_code"] == 1
    customer = next(c for c in report["cases"] if c["id"] == "customer")
    pointers = [d["path"] for d in customer["differences"]]
    assert "/customer/status" in pointers


def test_compare_without_targets_is_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["compare", "--corpus", EQUIVALENT])
    assert rc == EXIT_ERROR
    assert "target" in capsys.readouterr().err


def test_compare_mutating_refused_without_opt_in(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rc = main(["compare", "--corpus", MUTATING, "--output", str(tmp_path / "o"), *_app_args()])
    assert rc == EXIT_ERROR
    assert "mutating" in capsys.readouterr().err


def test_compare_mutating_with_opt_in_passes(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    rc = main(
        [
            "compare",
            "--corpus",
            MUTATING,
            "--allow-mutating-requests",
            "--output",
            str(outdir),
            "--quiet",
            *_app_args(),
        ]
    )
    assert rc == EXIT_OK
    report = json.loads((outdir / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["matched"] == 3


def test_compare_json_only_reporting(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("reporting:\n  junit_xml: false\n", encoding="utf-8")
    outdir = tmp_path / "out"
    rc = main(
        [
            "compare",
            "--corpus",
            EQUIVALENT,
            "--config",
            str(config),
            "--output",
            str(outdir),
            "--quiet",
            *_app_args(),
        ]
    )
    assert rc == EXIT_OK
    assert (outdir / "report.json").is_file()
    assert not (outdir / "junit.xml").exists()


def test_compare_config_file_supplies_targets(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "targets:\n"
        "  old:\n"
        f"    kind: wsgi\n    app: {OLD_APP}\n"
        "  new:\n"
        f"    kind: asgi\n    app: {NEW_APP}\n",
        encoding="utf-8",
    )
    outdir = tmp_path / "out"
    rc = main(
        [
            "compare",
            "--corpus",
            EQUIVALENT,
            "--config",
            str(config),
            "--output",
            str(outdir),
            "--quiet",
        ]
    )
    assert rc == EXIT_OK


def test_compare_cli_flag_overrides_config_safety(tmp_path: Path) -> None:
    """--allow-mutating-requests must unlock a mutating corpus even without config."""
    config = tmp_path / "config.yaml"
    config.write_text(
        "targets:\n"
        "  old:\n"
        f"    kind: wsgi\n    app: {OLD_APP}\n"
        "  new:\n"
        f"    kind: asgi\n    app: {NEW_APP}\n",
        encoding="utf-8",
    )
    outdir = tmp_path / "out"
    rc = main(
        [
            "compare",
            "--corpus",
            MUTATING,
            "--config",
            str(config),
            "--allow-mutating-requests",
            "--output",
            str(outdir),
            "--quiet",
        ]
    )
    assert rc == EXIT_OK


def test_compare_progress_lines_with_verbose(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rc = main(
        [
            "compare",
            "--corpus",
            EQUIVALENT,
            "--output",
            str(tmp_path / "o"),
            *_app_args(),
        ]
    )
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "health" in out
    assert "match" in out.lower()


def test_compare_quiet_output_is_minimal(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rc = main(
        [
            "compare",
            "--corpus",
            SECRETS,
            "--output",
            str(tmp_path / "o"),
            "--quiet",
            *_app_args(),
        ]
    )
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out
    assert "session" not in out  # quiet hides per-case lines


def test_compare_secrets_absent_from_console_and_files(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    outdir = tmp_path / "out"
    rc = main(
        [
            "compare",
            "--corpus",
            SECRETS,
            "--include-bodies",
            "--output",
            str(outdir),
            "--verbose",
            *_app_args(),
        ]
    )
    assert rc == EXIT_OK
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    report_text = (outdir / "report.json").read_text(encoding="utf-8")
    junit_text = (outdir / "junit.xml").read_text(encoding="utf-8")
    for secret in ("hunter2", "secret-key-123"):
        assert secret not in combined
        assert secret not in report_text
        assert secret not in junit_text


# --------------------------------------------------------------------------- #
# subprocess entry point
# --------------------------------------------------------------------------- #


def test_python_dash_m_version() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "apiparity", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == EXIT_OK
    assert "apiparity" in proc.stdout
