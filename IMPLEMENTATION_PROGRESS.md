# apiparity — Implementation Progress

> Live status log. Requirements are defined in `REQUIREMENTS.md` (authoritative).
> Update this file at every phase gate. Never mark a phase done without running its checks.
>
> **Rule:** never claim a test passed unless it was actually executed.

| Field | Value |
|---|---|
| Project root | `D:\Project\python_lib\apiparity` |
| Started | 2026-10-20 |
| Target Python | ≥3.11 (dev env: 3.11.3, Windows/PowerShell) |
| Execution mode | Sequential, single agent |
| Report schema version | 1 |

## Environment facts (verified 2026-10-20)

- PyPI name `apiparity`: **available** (`pip index versions apiparity` → no distribution).
- Installed: python 3.11.3, httpx 0.28.1, PyYAML 6.0.3, fastapi 0.128.8, starlette 0.52.1,
  flask 3.1.3 (test-only, installed this session), pytest 9.0.3, pytest-cov 7.1.0,
  pytest-asyncio 1.4.0, ruff 0.15.13, mypy 2.1.0, build 1.5.0, coverage 7.14.0, uvicorn 0.49.0.
- Workspace root is not a git repo → no commits/publishing unless explicitly authorized.

## Phase status legend

`[ ]` not started · `[~]` in progress · `[x]` done & checks run · `[!]` blocked/limited

---

## Phase 1 — Repository scaffold & plan

- [x] Created folder tree (`src/apiparity`, `reports/`, `targets/`, `tests/{unit,integration,
      security,fixtures/{legacy_flask,migrated_fastapi}}`, `examples/`, `docs/`,
      `.github/workflows`).
- [x] `REQUIREMENTS.md` written (standalone full spec for future/compacted agents).
- [x] This progress file written.
- [x] PyPI name availability check → available.
- [x] `flask 3.1.3` installed (test-only dependency).
- [x] `pyproject.toml`, `LICENSE` (MIT), `.gitignore`, `CHANGELOG.md`, `CONTRIBUTING.md`.
- [x] `docs/architecture.md` ADR decisions recorded. *(completed in Phase 7 docs batch)*

## Phase 2 — Models, configuration, corpus — **DONE & VERIFIED** (106 unit tests pass)

- [x] `models.py` typed dataclasses (REQ §6). *Note: `TargetConfig.timeout_seconds` is
      `float | None = None`; `DifferenceCategory` literal includes `"timing"`.*
- [x] `exceptions.py` hierarchy (REQ §10).
- [x] `corpus.py` YAML+JSON parse + strict validation (REQ §7).
- [x] `variables.py` `${VAR}` recursion + missing-var failure before execution.
- [x] `config.py` config file + precedence (REQ §8).
- [x] Unit tests: corpus/config/variables — all rejection paths → **106 passed** (2026-10-20).

## Phase 3 — Target adapters — **DONE & VERIFIED** (Phase 6 suites)

- [x] `targets/base.py` Target ABC (startup/execute/shutdown/describe) + `read_capped_*`
      + `_build_snapshot` + `effective_timeout` (per-case → target → config default).
- [x] `targets/http_target.py` httpx async client, timeouts, TLS on, no redirects, no retries,
      `trust_env=False` (system proxies never intercept comparison traffic — found by the
      HTTP-mode integration test on a host with a WinINET proxy).
- [x] `targets/wsgi_target.py` in-process WSGI via `httpx.WSGITransport` + `asyncio.to_thread`.
      **Fix (Phase 6):** PATH_INFO middleware re-encodes httpx's decoded Unicode path per
      PEP 3333 (`path.encode("utf-8").decode("latin-1")`) — httpx's WSGITransport violates
      the spec and Werkzeug mangles non-ASCII path params without it.
- [x] `targets/asgi_target.py` in-process ASGI **+ manual lifespan driver** (ASGITransport
      does not fire lifespan) incl. startup-failure path.
- [x] `targets/loader.py` `module:attr` + `--factory` + kind resolution; `TargetInitError`.
- [x] `safety.py` mutating/remote guards + safety summary (fail closed).
- [x] Target tests: connection refusal, timeout (stalled-socket server), lifespan
      startup-failure, factory init, WSGI↔ASGI mixed run, HTTP↔HTTP against real
      Werkzeug+uvicorn servers — **all pass (2026-10-20)**.

## Phase 4 — Comparison engine — **DONE & VERIFIED** (Phase 6 suites)

- [x] `normalization.py` ignored headers/JSON pointers, whitespace/array rules, size limit.
- [x] `comparison.py` status, headers (case-insensitive, multi-value), JSON structural diff
      with RFC 6901 pointers, null-vs-missing, type changes, text/binary, max-diff cap,
      timing rules (off by default).
- [x] `redaction.py` default sensitive headers/patterns/pointers, `[REDACTED]`, exception
      sanitization; diff detection preserved with masked display values.
- [x] Unit tests: nested diffs, ignored pointers, cap, redaction, size limit, categories,
      header multi-values, binary bodies, unicode paths — **all pass (2026-10-20)**.
      **Fix (Phase 6):** `redaction.py` `_QUERY_RE` never masked the *first* query param
      (regex lacked `^` alternative in the prefix group).

## Phase 5 — CLI and reports — **DONE & VERIFIED** (Phase 6 suites)

- [x] `cli.py` `validate` + `compare`, all flags (REQ §14.2), exit codes 0/1/2;
      `--include-bodies`, `--output`; progress lines gated on `--quiet`.
- [x] `__main__.py` for `python -m apiparity`.
- [x] `runner.py` sequential orchestration, per-case results even on failure, cleanup always;
      passes `default_timeout` through to `resolve_target`.
- [x] `reports/json_report.py` schema v1 canonical report.
- [x] `reports/junit_report.py` JUnit XML with correct escaping.
- [x] `reports/terminal_report.py` grouped, redacted, quiet/verbose modes.
- [x] `examples/requests.yaml`, `examples/config.yaml`, `examples/README.md` — written and
      **run end-to-end** (validate + compare against fixture apps → exit 0).
      **Fix (Phase 6):** config-file `normalization` section without `ignored_headers`
      used to clobber the built-in default header ignores (`date`, `content-length`,
      `server`, `connection`) — absent key now keeps defaults, explicit key replaces.
- [x] `tests/test_cli.py` exit-code contract tests (29 tests) — **all pass (2026-10-20)**.

## Phase 6 — Integration & security tests — **DONE & VERIFIED** (318 tests total)

- [x] `tests/fixtures/legacy_flask` equivalent app (health, nested JSON, query defaults,
      repeated query, path params, body validation, 404, auth failure, exception endpoint,
      state-changing store, dynamic metadata, session/secret endpoint).
- [x] `tests/fixtures/migrated_fastapi` equivalent app + **deliberate mismatch variant**
      (`app_mismatch.py`: flipped `/customer/status`, missing auth check, missing routes;
      `/api/boom` + handlers present so only deliberate divergences appear).
- [x] `tests/fixtures/corpora/{equivalent,mutating,secrets}.yaml`.
- [x] Integration (`tests/integration/test_integration.py`, 10 tests): equivalent WSGI↔ASGI
      → 9/9 match, exit 0; mismatch variant → exit 1 with exact `/customer/status`
      `json_value` pointer (active→inactive) + auth-failure 401-vs-200; mutating corpus
      with opt-in → all match; factory init; HTTP↔HTTP against real Werkzeug+uvicorn
      loopback servers; ASGI lifespan startup failure → `TargetInitError`; connection
      refusal → `transport` error, exit 1; stalled-socket timeout → `timeout` error,
      exit 1; invalid corpus → zero `Target.execute` calls (monkeypatched spy).
- [x] Security (`tests/integration/test_security.py`, 5 tests): secrets absent from
      report.json/junit.xml/terminal even with `--include-bodies`; mutating refused
      before any target init; remote refused; URL userinfo never leaks into reports;
      failed run never reports success.
- [x] CLI (`tests/test_cli.py`, 29 tests): exit-code contract for validate/compare on
      equivalent (0), mismatch (1), invalid config/corpus/safety (2); config-file targets;
      CLI-over-config safety override; JSON-only reporting; secrets absent from console
      and files; `python -m apiparity --version` subprocess.
- [x] Additional fixes found by Phase 6 suites:
      - default `ignored_headers` now includes `connection` (hop-by-hop; Werkzeug sends
        `connection: close`, uvicorn keep-alives — not an application difference);
      - fixture bugs: `json_body`→`json` corpus field, FastAPI `list[str]` query param
        needs `Query(default=...)`, `/api/private` needed `response_model=None`.


## Phase 7 — Packaging, docs, final validation — **DONE & VERIFIED**

- [x] README.md, docs/{architecture,configuration,normalization,safety,troubleshooting}.md,
      SECURITY.md, CONTRIBUTING.md, CHANGELOG.md complete & accurate.
      (Examples comments corrected during doc audit: `array_order` values are
      `preserve|sort`; corpus schema ref is REQUIREMENTS §7; corpus `ignored_headers`
      documented as additive/union vs config-file replace semantics.)
- [x] `.github/workflows/ci.yml` (lint job; pytest matrix ubuntu+windows × 3.11/3.12/3.13;
      build job + clean-venv wheel-smoke job downloading the built wheel).
- [x] `ruff check .` → 0 findings.
- [x] `ruff format --check .` → clean.
- [x] `mypy src` → clean.
- [x] `pytest -q --cov=apiparity` → **318 passed**; comparison 94%, safety 93% (≥90% gate met).
- [x] `python -m build` → sdist + wheel OK (only cosmetic setuptools license-classifier
      deprecation warning; artifacts: apiparity-0.1.0.tar.gz, apiparity-0.1.0-py3-none-any.whl).
- [x] Wheel installed in clean venv → CLI runs outside source checkout: `--version` →
      `apiparity 0.1.0`; `--help` exit 0; `validate` exit 0; full `compare` exit 0
      (8/8 matched) — all invoked from `%TEMP%` with PYTHONPATH=tests/fixtures.
- [x] E2E acceptance (clean venv, wheel install):
      validate → exit 0; compare equivalent → exit 0, 8/8 matched, reports written;
      mutated candidate (`migrated_fastapi.app_mismatch:app`, CLI override requires
      `--new-kind asgi`) → exit 1 with exact `/customer/status` json_value
      `active`→`inactive` plus downstream 422/201-vs-404 diffs on the POST cases
      (mismatch variant has no POST route); report.json parses
      (`schema_version: 1`, `exit_code: 1`, 5 matched/3 mismatched); junit.xml
      well-formed (8 tests, 3 failures).
- [x] Generated reports inspected for leakage: `example-token`/`Bearer ` absent from
      report.json, junit.xml, and terminal captures; `dist/` not committed
      (gitignored; workspace root not a git repo).
- [x] Final delivery summary (REQ §24) produced.

---

## Decision log (ADR-style)

| # | Decision | Rationale |
|---|---|---|
| 1 | stdlib `dataclasses` + hand validation, no pydantic at runtime | REQ §2 dependency budget: runtime = httpx + PyYAML only |
| 2 | Async core (`compare`) + `compare_sync` wrapper | One documented lifecycle; httpx async; CLI uses `asyncio.run` |
| 3 | httpx `WSGITransport` / `ASGITransport` + **manual lifespan driver** | ASGITransport never runs lifespan; REQ §6.3 |
| 4 | argparse CLI, no click/typer | REQ §12: no large framework for parsing |
| 5 | Reports via stdlib `json` / `xml.etree`-level escaping | Small footprint; JUnit escaping under our control |
| 6 | Sequential execution only | Corpus ordering semantics (create-then-read); no concurrency in MVP |
| 7 | Redaction after comparison, before display | Detect sensitive diffs without exposing values (REQ §11) |
| 8 | Fixtures imported via `conftest.py` path injection | Keeps `module:app` loader realistic without installing fixtures |
| 9 | HTTP target uses `trust_env=False` | A parity checker must send requests exactly where the user pointed them; system proxies (WinINET/env) must not intercept or leak comparison traffic (found by HTTP-mode integration test) |
| 10 | WSGI adapter wraps PATH_INFO with UTF-8→latin-1 re-encoding | httpx `WSGITransport` passes the URL-decoded Unicode path, violating PEP 3333; Werkzeug then mangles non-ASCII path params. Middleware restores raw-bytes semantics (identity for ASCII) |
| 11 | `connection` added to default ignored headers | Hop-by-hop transport header (Werkzeug sends `close`, uvicorn keep-alives); not application behavior |
| 12 | Config-file normalization: absent `ignored_headers` keeps built-in defaults; explicit value replaces | Omitting the key from a section that sets other rules must not silently re-enable content-length/date/server diffs |

## Risk register

| Risk | Mitigation / status |
|---|---|
| ASGI lifespan semantics | Manual driver + startup-failure test (Phase 3) |
| httpx WSGITransport repeated-query behavior | Explicit query list handling in request build + test |
| Windows path/venv quirks in docs | Use `$LASTEXITCODE` in PowerShell examples |
| Side-effect safety | Fail-closed opt-ins, safety summary, no rollback claims in docs |

## Known limitations / deferred (sync with REQ §22)

- HTML report, code conversion/LLM patches, pytest plugin, snapshot assertions,
  distributed workers, DB snapshots, traffic capture, rollback, non-HTTP protocols,
  body persistence to disk, trusted-mode file uploads. → must appear in README Limitations.

## Test/lint/typecheck results log

| Date | Command | Result |
|---|---|---|
| 2026-10-20 | `python -m pytest tests/unit -q` | **106 passed** (Phase 2 suites) |
| 2026-10-20 | `ruff check .` | All checks passed (0 findings) |
| 2026-10-20 | `ruff format --check .` | 28 files already formatted |
| 2026-10-20 | `mypy src` | Success: no issues found (23 files, strict) |
| 2026-10-20 | `python -m pytest tests/unit --cov=apiparity` | 36% total — Phases 3–5 modules untested (expected; tests land Phase 6) |
| 2026-10-20 | `python -m apiparity --version` / `compare --help` | CLI entry point wired, flags correct |
| 2026-10-20 | `python -m pytest tests/unit -q` (post Phase-6 unit suites) | **272 passed** |
| 2026-10-20 | `python -m pytest tests/integration -q` | **15 passed** (10 integration + 5 security) |
| 2026-10-20 | `python -m pytest tests/test_cli.py -q` | **29 passed** |
| 2026-10-20 | `python -m pytest tests -q --cov=apiparity` | **318 passed**; total 89% — **comparison 94%, safety 93%** (both ≥90% gate met) |
| 2026-10-20 | `ruff check .` (final) | All checks passed |
| 2026-10-20 | `ruff format .` then `--check` (final) | reformatted 5 files; then clean |
| 2026-10-20 | `mypy src` (final) | Success: no issues found (23 files) |
| 2026-10-20 | `python -m build` (final) | Successfully built apiparity-0.1.0.tar.gz + apiparity-0.1.0-py3-none-any.whl |
| 2026-10-20 | `apiparity validate/compare` on `examples/` (with PYTHONPATH=tests/fixtures) | validate exit 0; compare equivalent exit 0; mutating refused without opt-in (exit 2), allowed with flag |
| 2026-10-20 | `python -m build` (Phase 7) | sdist + wheel rebuilt OK (README/docs included in metadata) |
| 2026-10-20 | clean venv wheel smoke (`%TEMP%\opencode\smoke-venv`) | wheel + flask/fastapi/uvicorn installed; `apiparity --version` → 0.1.0; `--help` exit 0; validate exit 0; compare exit 0 (8/8 match) — all from outside the checkout |
| 2026-10-20 | E2E mismatch run (wheel venv, `--new-app migrated_fastapi.app_mismatch:app --new-kind asgi`) | exit 1; exact `/customer/status` json_value `active`→`inactive`; report.json parses (schema_version 1); junit.xml well-formed (8 tests, 3 failures) |
| 2026-10-20 | secret scan of E2E report.json / junit.xml / terminal captures | `example-token`, `hunter2`, `secret-key-123`, `Bearer ` all absent — CLEAN |
| 2026-10-20 | final gate re-run (ruff check / format / mypy / pytest) | all clean; **318 passed**; comparison 94%, safety 93%, total 89% |

### Current phase: **Phases 1–7 complete** — deliverable ready.

All phases done: 318 tests pass (unit + integration + security + CLI); ruff check,
ruff format, mypy (strict) clean; coverage gates met (comparison 94%, safety 93%);
sdist+wheel build; wheel installs and runs in a clean venv outside the checkout;
E2E acceptance passes (equivalent → 0, mutated → 1 with exact pointer, reports parse,
no secret leakage); full documentation set (README, five docs pages, SECURITY,
CONTRIBUTING, CHANGELOG) and CI workflow delivered. Nothing published or committed
(workspace is not a git repo; no PyPI upload) per authorization rules.
