# apiparity — Detailed Implementation Requirements

> **Purpose of this document:** This is the authoritative, self-contained specification for
> building `apiparity` end to end. A fresh or compacted agent must be able to implement and
> verify the product from THIS file alone, without relying on conversation summaries.
> `IMPLEMENTATION_PROGRESS.md` tracks *status*; this file defines *what must exist*.
>
> **Status:** Approved for implementation. Do not silently drop any requirement. If a
> requirement cannot be completed, record the exact limitation, its effect, and the next step
> in `IMPLEMENTATION_PROGRESS.md`.

---

## 1. Product definition

`apiparity` is an open-source developer tool (Python package + CLI) that verifies that a
*migrated or changed* API implementation behaves like a *baseline* implementation.

- Core value: **convert the implementation however you like; use `apiparity` to prove the new
  one behaves like the old one.**
- It is a **behavioral comparison engine**, NOT a source-code converter. Code conversion /
  LLM patches are explicitly out of scope for MVP (see §20).
- Comparison results must be **deterministic and reproducible**: no LLM calls anywhere in the
  core comparison engine.
- Distribution name: `apiparity` (verified available on PyPI on 2026-10-20 — no existing
  distribution). Re-verify before any public release.

### Primary use cases

- **A.** Flask → FastAPI migration: detect status/validation/JSON-shape/header/auth/pagination
  differences between the old Flask service and the new FastAPI service.
- **B.** General API regression testing between two versions of any HTTP API.
- **C.** CI gate: replay a request corpus, emit reports, fail CI on behavioral regressions.
- **D.** In-process comparison of two imported application objects (WSGI / ASGI) without
  network servers.
- **E.** Controlled comparison of two separately running HTTP endpoints (dev/staging).

---

## 2. Environment and constraints (verified)

| Item | Value |
|---|---|
| OS / shell | Windows 11, PowerShell 5.1 |
| Python | 3.11.3 (`C:\Users\sonal\AppData\Local\Programs\Python\Python311\python.exe`) |
| Installed & usable | `httpx 0.28.1`, `PyYAML 6.0.3`, `fastapi 0.128.8`, `starlette 0.52.1`, `flask 3.1.3` (installed during Phase 1 as a **test-only** dep), `pytest 9.0.3`, `pytest-cov 7.1.0`, `pytest-asyncio 1.4.0`, `ruff 0.15.13`, `mypy 2.1.0`, `build 1.5.0`, `coverage 7.14.0`, `uvicorn 0.49.0` |
| Network | PyPI reachable (pip works) |
| Git | Workspace root is NOT a git repo. Do not `git init`, commit, push, or publish unless the user explicitly asks. |
| House conventions | Sibling projects (`uni-connect`, `ai-accel`) use setuptools + `src/` layout + `pyproject.toml`. Match this. |

**Runtime dependency budget (hard rule):** only `httpx` and `PyYAML`. Everything else
(stdlib `json`, `argparse`, `dataclasses`, `hashlib`, `xml`, `asyncio`, `urllib.parse`,
`importlib`, `pathlib`) must be standard library. `pydantic` must NOT be a runtime dependency
— use `dataclasses` + explicit hand-written validation. Flask/FastAPI are optional extras only.

---

## 3. Authoritative directory layout

Create exactly this tree. Do not add empty modules — every module listed has a defined
responsibility in §5 and must contain real code.

```text
apiparity/
├── pyproject.toml
├── README.md
├── LICENSE                      (MIT)
├── CHANGELOG.md
├── SECURITY.md
├── CONTRIBUTING.md
├── REQUIREMENTS.md              (this file)
├── IMPLEMENTATION_PROGRESS.md   (live status log)
├── src/
│   └── apiparity/
│       ├── __init__.py          public API re-exports + __version__
│       ├── __main__.py          python -m apiparity → cli.main()
│       ├── cli.py               argparse CLI: validate, compare
│       ├── config.py            project config file (YAML/JSON) parsing + precedence
│       ├── models.py            all typed dataclasses (see §6)
│       ├── corpus.py            request-corpus parsing + validation (see §7)
│       ├── variables.py         ${VAR} environment interpolation
│       ├── safety.py            mutating/remote target guards, safety summary
│       ├── exceptions.py        exception hierarchy (see §10)
│       ├── runner.py            orchestration: execute corpus, produce RunSummary
│       ├── normalization.py     response normalization rules
│       ├── comparison.py        structural diff engine → Difference list
│       ├── redaction.py         secret masking (reports/terminal/logs)
│       ├── reports/
│       │   ├── __init__.py      report writer dispatch
│       │   ├── json_report.py   canonical machine-readable report
│       │   ├── junit_report.py  JUnit XML for CI
│       │   └── terminal_report.py  human console summary
│       └── targets/
│           ├── __init__.py
│           ├── base.py          Target ABC / protocol + shared snapshot helpers
│           ├── http_target.py   HTTPX-based remote/local endpoint target
│           ├── wsgi_target.py   in-process WSGI target (httpx.WSGITransport)
│           ├── asgi_target.py   in-process ASGI target (httpx.ASGITransport + manual lifespan)
│           └── loader.py        module:attr import, factory handling, kind resolution
├── tests/
│   ├── conftest.py
│   ├── unit/                    (test_*.py per module, see §18.1)
│   ├── integration/             (see §18.2)
│   ├── security/                (see §18.4)
│   ├── fixtures/
│   │   ├── legacy_flask/        baseline Flask app package
│   │   └── migrated_fastapi/    candidate FastAPI app package (+ deliberate variants)
│   └── test_cli.py
├── examples/
│   ├── requests.yaml
│   ├── config.yaml
│   └── README.md
├── docs/
│   ├── architecture.md
│   ├── configuration.md
│   ├── normalization.md
│   ├── safety.md
│   └── troubleshooting.md
└── .github/
    └── workflows/
        └── ci.yml
```

Add `tests/fixtures/legacy_flask/__init__.py`, `tests/fixtures/migrated_fastapi/__init__.py`
etc. as needed so `module:app` imports work; add a `pyproject.toml`-independent import path by
making fixtures importable (tests use `sys.path` fixture or `conftest.py` path injection —
decide one approach and document it in `docs/architecture.md`).

---

## 4. Main execution flow (must be preserved)

1. Load and validate configuration (defaults ← config file ← CLI flags; see §8.4 precedence).
2. Load and validate the request corpus (fail fast; **no outbound requests on invalid input**).
3. Resolve `${VAR}` environment placeholders (fail before execution if a variable is missing;
   never log resolved secrets).
4. Safety checks (mutating-method guard, remote-target guard) → print safety summary.
5. Initialize baseline and candidate targets. **If either fails: stop, exit code 2, report
   which target failed + sanitized error + corrective action. Never fall back silently to a
   different target type. Never present a partially initialized run as success.**
6. Execute each case sequentially in corpus order against baseline, then candidate.
7. Normalize observed responses (read-only; never mutate app state).
8. Compare normalized responses → structured `Difference`s.
9. Store per-case `CaseResult` (store a result even when a target fails).
10. Generate reports (terminal always; JSON/JUnit per config) into `--output` directory.
11. Map outcome to exit code (§14.3).
12. Close all resources (clients, transports, ASGI lifespan shutdown) deterministically, even on
    failure paths.

---

## 5. Module contracts (symbol-level)

| Module | Must provide (at minimum) |
|---|---|
| `models.py` | dataclasses: `RequestCase`, `TargetConfig`, `ComparisonConfig`, `NormalizationConfig`, `RedactionConfig`, `SafetyConfig`, `ReportingConfig`, `AppConfig`, `ResponseSnapshot`, `Difference`, `CaseResult`, `RunSummary`, `RunResult` |
| `corpus.py` | `load_corpus(path: Path) -> Corpus` (YAML or JSON by extension/content), full validation per §7 |
| `variables.py` | `resolve_variables(obj, env, *, context) -> obj` recursive `${NAME}` interpolation; `MissingVariableError` |
| `config.py` | `AppConfig.from_file(path)`, `merge_config(cli_args, file_cfg) -> AppConfig` with §8.4 precedence |
| `safety.py` | `evaluate_safety(targets, cases, cfg) -> SafetyReport`; `MUTATING_METHODS`; raises `SafetyError` on refusal |
| `targets/base.py` | `class Target(ABC)` with `async def execute(case: RequestCase) -> ResponseSnapshot`, `async def startup()`, `async def shutdown()`, `describe() -> str` (credential-free identifier) |
| `targets/loader.py` | `resolve_target(cfg: TargetConfig) -> Target`; `import_app(spec, factory: bool)`; raises `TargetInitError` |
| `runner.py` | `async def run_comparison(...) -> RunResult`; `def run_comparison_sync(...) -> RunResult` |
| `comparison.py` | `compare_snapshots(baseline, candidate, cfg) -> list[Difference]` |
| `normalization.py` | `normalize_snapshot(snapshot, rules) -> ResponseSnapshot` |
| `redaction.py` | `redact_headers`, `redact_json`, `redact_value`, `sanitize_exception(msg)`; placeholder `"[REDACTED]"` |
| `reports/*.py` | `write_json_report(result, path)`, `write_junit_report(result, path)`, `render_terminal(result, verbose) -> str` |
| `cli.py` | `main(argv: list[str] \| None = None) -> int` (returns exit code; never calls `sys.exit` internally except via `__main__`) |
| `exceptions.py` | `ApiParityError` base → `ConfigError`, `CorpusError`, `VariableResolutionError`, `SafetyError`, `TargetInitError`, `ExecutionError` |

All public execution paths must be typed (mypy-clean under the project's mypy config).

---

## 6. Core data models (field-level)

**`RequestCase`**: `id: str`, `description: str|None`, `method: str` (uppercased, validated
against a known HTTP-method set), `path: str` (must start with `/`, no scheme/host), `query:
dict[str, list[str]]`, `headers: dict[str, str]`, `json_body: Any|None`, `data: str|None`,
`files: list[FileUpload]|None`, `timeout_seconds: float|None`, `enabled: bool = True`,
`compare_headers: bool|None`, `compare_body: bool|None`.

**`ResponseSnapshot`**: `status_code: int|None`, `headers: list[tuple[str, list[str]]]`
(lower-cased names, preserved multi-values — do NOT flatten repeated headers),
`content_type: str|None`, `body_bytes: bytes|None`, `body_truncated: bool`,
`body_size: int`, `duration_ms: float`, `transport_error: str|None` (already sanitized),
`error_kind: Literal["none","transport","timeout","decode","too_large"]`.

**`Difference`**: `category: str` (enum, §11), `severity: Literal["error","warning","info"]`,
`path: str|None` (JSON Pointer for body diffs), `message: str`, `baseline_value: Any`,
`candidate_value: Any` (already redacted before leaving the comparison layer for report
output), `case_id: str`, `details: dict|None`.

**`CaseResult`**: `case_id`, `status: Literal["match","mismatch","failed","skipped"]`,
`differences: list[Difference]`, `baseline: ResponseSnapshot|None`,
`candidate: ResponseSnapshot|None`, `skipped_reason: str|None`, `duration_ms`.

**`RunSummary`**: `total`, `matched`, `mismatched`, `failed`, `skipped`, `started_at` (UTC
ISO-8601), `finished_at`, `exit_code`.

**`RunResult`**: `summary: RunSummary`, `cases: list[CaseResult]`, `target_descriptions:
dict[str,str]`, `corpus_path`, `config_fingerprint`, `normalization_rules_applied: dict`,
`tool_version`, `schema_version` (report schema, start at `1`).

Models must be immutable-ish for outputs (`frozen=True` where practical) and must never expose
internal mutable runner state to callers.

---

## 7. Request corpus (input format)

Support **YAML and JSON**. Schema version field `version: 1`.

### 7.1 Top-level keys

`version` (required, must be `1`), `defaults` (optional), `cases` (required, non-empty list),
`normalization` (optional). Unknown top-level keys → `CorpusError` (strict validation; forward
compatibility only if explicitly designed — it is not).

### 7.2 `defaults` (all optional)

`timeout_seconds: float > 0`, `follow_redirects: bool` (default `false`),
`compare_headers: bool` (default `true`), `compare_body: bool` (default `true`).

### 7.3 Case fields

| Field | Rules |
|---|---|
| `id` | required, non-empty, unique across corpus (duplicate → `CorpusError`) |
| `description` | optional string |
| `method` | required; valid HTTP token; stored uppercase; known methods only |
| `path` | required; must start with `/`; reject absolute URLs and whitespace/control chars |
| `query` | optional mapping str → str \| list[str] (repeated values preserved in order) |
| `headers` | optional mapping str → str; header names validated as tokens |
| `json` | optional JSON body (must be serializable; must not coexist with `data`/`files`) |
| `data` | optional raw/form body string (must not coexist with `json`/`files`) |
| `files` | optional list of `{field, path, content_type?}`; `path` resolved relative to corpus dir; **reject any path escaping the corpus/fixture root** (symlink-aware `Path.resolve()` check) unless documented trusted mode is enabled (MVP: no trusted mode — reject) |
| `timeout_seconds` | optional `> 0` override |
| `enabled` | optional bool, default `true`; disabled cases → `skipped` with reason |

Reject: conflicting body representations, malformed YAML/JSON (with line info when available),
invalid types, unknown case fields.

### 7.4 Environment-variable interpolation

- Syntax `${VARIABLE_NAME}` inside **string values** of supported fields (headers, query
  values, json/data bodies, path — recursively through nested structures).
- Resolution uses `os.environ` (injectable for tests).
- Missing variable → `VariableResolutionError` **before any request is executed**.
- Never log or report resolved secret values; error messages may name the variable, not its
  value.

### 7.5 Normalization block (corpus-level)

`ignored_headers: list[str]`, `ignored_json_pointers: list[str]` (JSON Pointer per RFC 6901),
optional `whitespace: {"text": bool}`, `array_order: {pointer: "preserve"|"sort"}`,
`timing: {"max_ms": int, "mode": "off"|"warn"|"fail"}`.

### 7.6 Baseline semantics

Baseline = reference behavior; candidate = new implementation. Never compare against an
imaginary universal standard, and **never silently update/bless the baseline** to make
failures disappear (no snapshot-update feature in MVP).

---

## 8. Configuration file

One project-level file (`--config`, YAML or JSON), schema `version: 1`. Sections:

```yaml
version: 1
defaults: { timeout_seconds: 10, follow_redirects: false }
safety: { allow_mutating_requests: false, allow_remote_targets: false }
comparison:
  compare_status: true
  compare_headers: true
  compare_body: true
  max_differences_per_case: 100
  response_size_limit_bytes: 5242880     # 5 MiB default
  timing: { mode: "off", max_ms: null }  # off | warn | fail
normalization:
  ignored_headers: [date, content-length, server]
  ignored_json_pointers: ["/metadata/request_id"]
redaction:
  sensitive_headers: [authorization, cookie, set-cookie, x-api-key, proxy-authorization]
  sensitive_json_pointers: ["/credentials/token"]
reporting: { json: true, junit_xml: true, terminal: true }
targets:
  old: { kind: http, url: "http://127.0.0.1:5000" }
  new: { kind: http, url: "http://127.0.0.1:8000" }
```

- Validate types of every field; unknown fields → `ConfigError`.
- **Precedence (highest wins):** CLI flags > config file > corpus `defaults` > built-in
  defaults. Case-level fields override corpus defaults. Document this order in
  `docs/configuration.md`.
- Target identity in reports must never embed credentials (strip userinfo from URLs).

---

## 9. Targets and execution modes

### 9.1 HTTP target (`--old-url` / `--new-url`)

- `httpx.AsyncClient` with: explicit `timeout` (connect/read/write/pool), `verify=True` TLS by
  default, `follow_redirects=False` by default.
- Preserve repeated query parameters in order.
- **No automatic retries** (especially non-idempotent methods).
- Capture transport failures distinctly from HTTP responses (`error_kind`).
- Reuse connections within a run; close client in `shutdown()` on all paths.

### 9.2 WSGI target (`--old-app mod:attr [--old-factory] --old-kind wsgi`)

- Import via `loader.import_app`; if `--*-factory`, call the imported object to obtain the app.
  Non-factory: use attribute directly; if it's callable returning an app only under `--factory`.
- Transport: `httpx.WSGITransport(app=...)` dispatched to a dedicated daemon thread; the
  per-request timeout is enforced on the event loop — a handler that never returns is
  abandoned at the deadline (it cannot be interrupted).
- Never fall back to HTTP/ASGI on failure.

### 9.3 ASGI target (`--new-kind asgi`)

- Transport: `httpx.ASGITransport(app=...)`.
- **ASGITransport does NOT run lifespan events** — implement a manual lifespan driver:
  run `app({"type":"lifespan"}, receive, send)` as a task, await `startup` message (or
  `lifespan.startup.failed` → `TargetInitError`), and always send `lifespan.shutdown` in
  `shutdown()`, awaiting completion. Test the startup-failure path.
- Handle apps without lifespan support gracefully, governed by the per-target lifespan
  policy `targets.<side>.lifespan` (CLI `--old-lifespan`/`--new-lifespan`), one of
  `auto` (default) / `required` / `off`:
  - `auto`: a lifespan task that closes cleanly without responding → "no lifespan";
    `lifespan.startup.failed` **or an uncaught exception in the lifespan task** →
    `TargetInitError` (startup errors are never concealed);
  - `required`: additionally fails when no startup message ever arrives;
  - `off`: the protocol is not run at all (apps that crash/hang on lifespan scope);
  - startup timeout → `TargetInitError`; every wait around the lifespan task is bounded.

### 9.4 Loader rules

- Spec format `package.module:attribute`. Import errors → `TargetInitError` with the module
  name, exception type, and corrective action (sanitized; never dump secrets).
- `--old-kind`/`--new-kind` ∈ {`http`, `wsgi`, `asgi`}; default `http` when `--*-url` given.
  If app given without kind → infer is allowed ONLY when unambiguous; otherwise require kind
  explicitly and error clearly.
- Mixed modes are allowed (e.g., WSGI baseline + ASGI candidate).

### 9.5 Initialization failure

Stop before corpus execution; report which target failed, sanitized error, corrective action;
exit code 2.

---

## 10. Exceptions

`ApiParityError` → `ConfigError`, `CorpusError`, `VariableResolutionError`, `SafetyError`,
`TargetInitError`, `ExecutionError`. Validation problems must include the offending location
(file, case id, field path). All messages pass through redaction before display.

---

## 11. Execution semantics

- **Sequential, corpus order, by default** (ordering matters for create-then-read cases). No
  concurrency in MVP.
- For each enabled case: send to baseline → capture; send equivalent request to candidate →
  capture; normalize; compare; store `CaseResult`.
- **Never skip candidate execution because baseline returned 4xx/5xx** — an HTTP error is a
  valid observable result.
- Disabled cases → `skipped` with reason; counted separately.
- Transport failure on one or both targets → `failed` case (not `match`), difference category
  `transport_error`/`timeout` with severity `error`, but classified as infrastructure, not
  business-logic proof. If baseline transport-fails and candidate transport-fails identically,
  still classify as `failed` (not `match`).

### 11.1 Side-effect policy (fail-closed)

- Mutating methods (`POST`, `PUT`, `PATCH`, `DELETE`, and any method outside
  `{GET, HEAD, OPTIONS}`) require `safety.allow_mutating_requests` / `--allow-mutating-requests`.
- Non-loopback HTTP targets (any host not in `{127.0.0.1, ::1, localhost}`) require
  `--allow-remote-targets`. **Never assume a private IP is a test environment.**
- Print a safety summary before the run: target hosts, mutating case count, opt-ins granted.
- No retries of non-idempotent operations; no automatic rollback claims; docs must state that
  baseline/candidate need isolated, identically seeded state for state-changing comparisons,
  and that even GET can have side effects.

---

## 12. Response capture and normalization

Order of operations per response: capture raw → size guard → normalization (ignored headers,
JSON-pointer exclusions, whitespace/array rules) → comparison → redaction (report/terminal).

- **Status:** compared by default; 404 vs 422 and 200 vs 204 are differences unless an
  explicit user exception exists (no exception mechanism in MVP → always differ).
- **Headers:** names case-insensitive (normalize to lowercase); default ignore list ONLY
  `date`, `content-length`, `server` (transport-generated); configurable. Do NOT ignore
  `content-type`, `location`, cache headers, or app headers by default. Repeated values
  compared as ordered lists.
- **JSON (when body parses as JSON):** structural diff; object key order ignored; array order
  preserved by default; detect added keys, removed keys, value changes, type changes,
  null-vs-missing (missing ≠ null), nested diffs; emit RFC 6901 JSON Pointer paths. Apply
  ignored pointers BEFORE producing differences. Cap at `max_differences_per_case` (default
  100) with a final `normalization_note`-category info difference stating truncation.
  Never auto-ignore all of `metadata`, all nulls, or whole bodies.
- **Text:** decode per charset/content-type; default: NO whitespace normalization (opt-in).
- **Binary:** byte equality (or sha256 digest when size limit forces partial capture).
- **Size limit:** default 5 MiB (`response_size_limit_bytes`). If exceeded → capture prefix,
  set `body_truncated`/`error_kind="too_large"`, emit `response_too_large` difference stating
  comparison was incomplete — **never report a match when full comparison was impossible**.
- **Timing:** always captured as diagnostic metadata; only fails the case when
  `timing.mode == "fail"` with `max_ms` (default `off`).

---

## 13. Difference classification

Categories (exact strings): `status_code`, `header_added`, `header_removed`, `header_value`,
`body_type`, `json_key_added`, `json_key_removed`, `json_value`, `json_type`, `text_body`,
`binary_body`, `transport_error`, `timeout`, `response_too_large`, `normalization_note`.

Severities: `error` (behavioral/infra failure), `warning` (timing warn, truncation notes),
`info` (metadata). Transport/config errors must stay distinct from behavioral differences —
a connection failure is never evidence that business logic differs.

---

## 14. CLI

Standard-library `argparse`. Entry points: console script `apiparity` and
`python -m apiparity`.

### 14.1 Commands

```bash
apiparity --help
apiparity --version
apiparity validate --corpus examples/requests.yaml [--config ...]
apiparity compare  --corpus ... [--config ...] [--old-url U | --old-app S] [--new-url U |
                   --new-app S] [--old-kind http|wsgi|asgi] [--new-kind ...] [--old-factory]
                   [--new-factory] [--old-lifespan auto|required|off]
                   [--new-lifespan ...] [--output DIR] [--allow-mutating-requests]
                   [--allow-remote-targets] [--verbose] [--quiet]
```

`validate` parses config + corpus, runs variable resolution and safety evaluation in
*dry* mode (no target init, no requests), prints OK/errors. `compare` runs the full flow.

### 14.2 Flags (all must exist; documented in `--help` and README)

`--corpus`, `--config`, `--old-url`, `--new-url`, `--old-app`, `--new-app`, `--old-kind`,
`--new-kind`, `--old-factory`, `--new-factory`, `--old-lifespan`, `--new-lifespan`,
`--output`, `--allow-mutating-requests`,
`--allow-remote-targets`, `--verbose`, `--quiet`, `--version`.

### 14.3 Exit codes (exact contract)

| Code | Meaning |
|---|---|
| `0` | All enabled cases compared and matched (skipped allowed); no error-severity differences |
| `1` | ≥1 behavioral mismatch or configured failure (incl. timing mode `fail`) |
| `2` | Invalid config/corpus, safety refusal, target initialization failure, unexpected execution error — i.e., **no valid comparison was produced** |

A run that had any failure-to-execute must never return 0.

### 14.4 Console output

Show: total, matched, mismatched, failed, skipped counts; top differences grouped by case and
category; report file paths; final status line. `--quiet` → only errors + final status;
`--verbose` → per-case detail. **Never print secrets** (post-redaction only).

---

## 15. Reports

### 15.1 JSON (canonical)

Keys: `schema_version`, `tool_version`, `started_at`/`finished_at` (UTC ISO-8601), `targets`
(baseline/candidate descriptions without credentials), `corpus` (path + counts), `config`
(normalization + redaction rules applied, comparison flags), `summary` (total/matched/
mismatched/failed/skipped/exit_code), `cases[]` (per-case results + differences), `result`
(`"pass" | "fail" | "error"`). Body previews included ONLY if explicitly enabled (default off).

### 15.2 JUnit XML

One `<testcase>` per request case (classname `apiparity.<case_id>`); mismatches → `<failure>`
with concise path-level messages; infra errors → `<error>`; skipped → `<skipped message>`.
Correct XML escaping everywhere (`&<>"'`); no secrets in attributes or messages.

### 15.3 Terminal

Grouped by case; show JSON Pointer path, baseline vs candidate values (redacted), status/header
diffs compactly. Never dump full responses when a short path diff suffices.

### 15.4 HTML

Deferred to a later release (recorded in README Limitations + progress log). If ever added:
escape all response-derived content, no remote assets.

Output files: `report.json`, `junit.xml` under `--output` (default `build/apiparity`). Never
write raw/unredacted response bodies to disk by default.

---

## 16. Python library API

```python
from apiparity import AppConfig, ComparisonConfig, compare, compare_sync, RunResult

config = AppConfig.from_file("apiparity.yaml")
result = await compare(
    corpus="examples/requests.yaml", config=config, old_target=..., new_target=...
)
# or: result = compare_sync(...)  # synchronous convenience wrapper
print(result.summary.matched, result.cases[0].differences)
```

- Document clearly: **primary API is async**; `compare_sync` provided for convenience.
- One resource-lifecycle policy: the target clients are opened/closed inside `compare*`
  (caller never must close). Never return internal mutable state.
- Raise documented exceptions (`ConfigError`, `CorpusError`, `TargetInitError`, `SafetyError`)
  for invalid input/setup; do not force a CLI subprocess.
- Do not export unstable adapter internals from `apiparity/__init__.py`.

---

## 17. Security and operational requirements (must all be implemented)

1. Never default to production traffic replay.
2. Explicit opt-in for mutating requests; explicit opt-in for remote targets (fail closed).
3. TLS verification on by default; timeouts on every request; no redirects by default.
4. No automatic retries of write requests.
5. Response-size limit enforced.
6. Fixture/corpus file paths cannot escape their configured root.
7. Redaction of credentials in logs, reports, terminal, exception messages (§18).
8. Corpus cannot trigger dynamic command execution (no `eval`/`shell` of corpus content).
9. Document that importing an app module executes its import-time code, and that in-process
   targets may touch real databases/external services.
10. No rollback/isolation claims unless genuinely provided.
11. Malformed input fails before outbound requests.
12. Tests use only synthetic data and local endpoints — no production credentials/endpoints.
13. `SECURITY.md` with a vulnerability-reporting process (private email/GitHub advisory,
    no public issue for vulns, expected response times).

### 17.1 Redaction (release-blocking)

- Default sensitive headers: `authorization`, `cookie`, `set-cookie`, `x-api-key`,
  `proxy-authorization` + configurable extras; also pattern-match header names containing
  `token`, `secret`, `password`, `apikey`/`api-key`, `credential`.
- Default sensitive JSON pointers: `/credentials/token` (configurable) + configurable list.
- Redaction applied **before** values reach reports or terminal output.
- Comparison detects that two sensitive values DIFFER but reports
  `baseline_value: "[REDACTED]"`, `candidate_value: "[REDACTED]"`,
  `message: "Sensitive field differs"`.
- Sanitized transport exception messages (strip URLs' userinfo, headers, and env values).
- Variable-resolution failures must not print credential values.
- `password=hunter2` style query params must be masked in any rendered URL.

---

## 18. Testing strategy (must validate behavior, not imports)

Run with: `pytest -q` (add `--cov=apiparity --cov-report=term-missing` for coverage gates).
Target: **≥90% statement coverage of `comparison.py` and `safety.py`** (and aim for the same
across `corpus.py`, `redaction.py`).

### 18.1 Unit tests (`tests/unit/`)

YAML+JSON corpus parsing; invalid schema/missing fields; duplicate ids; conflicting bodies;
env-var resolution incl. missing-variable failure; request-body validation; header
normalization/case-insensitivity; JSON diff: added/removed nested keys, null-vs-missing, type
mismatch, arrays + configured ordering, ignored pointers, max-differences cap; redaction of
headers/JSON/exceptions; response-size limit handling; error classification; exit-code logic;
JSON & JUnit serialization (incl. escaping); config precedence; safety evaluation.

### 18.2 Integration tests (`tests/integration/`)

Fixture apps (Flask baseline + FastAPI candidate) with behaviorally equivalent endpoints:

1. `GET /health` simple JSON.
2. `GET /api/customers/{id}` nested JSON (`customer.*`, `metadata.*`).
3. Query-parameter defaults (`limit=10` default).
4. Repeated query parameters (`tags=a&tags=b`) — same handling both sides.
5. Path parameter handling incl. unicode/space encoding.
6. Request-body validation (`POST /api/orders`) → identical 422-style errors or documented
   equivalent-shape errors (fixtures must produce IDENTICAL shapes when equivalent).
7. 404 behavior for unknown route.
8. Auth failure (`Authorization` required) → identical 401 shape (redaction-relevant).
9. **Deliberately different** endpoint/variant (e.g., FastAPI variant returns
   `status: "inactive"` where Flask returns `"active"`, or drops `metadata.version`) → must
   produce `mismatch` with the exact JSON Pointer.
10. Endpoint raising an exception → identical 500 handling on both sides when equivalent.
11. State-changing endpoint with isolated per-process in-memory store
    (`POST /api/orders` then `GET` list) — run only with `--allow-mutating-requests`.
12. Dynamic metadata (`/metadata/request_id`, `generated_at`) covered by ignored pointers → match.

Assertions: equivalent fixtures → exit 0 & all matched; mismatching fixture → exit 1 and
specific `Difference` fields; secrets absent from all outputs.

### 18.3 Target tests

HTTP↔HTTP (spin up local servers with `threading`+`uvicorn` for ASGI and Flask's server or a
WSGI server for WSGI — or use in-process targets to avoid ports where possible, but at least
one real HTTP round trip must be covered); WSGI↔ASGI in-process; factory initialization;
missing import → exit 2; invalid target kind; ASGI startup failure; client/resource cleanup
(no unclosed-client warnings); network timeout (bind a non-listening port for refusal;
a server that accepts and stalls for timeout — use a local socket server in-test); connection
refusal classification.

### 18.4 Security tests (`tests/security/`)

Mutating requests rejected without opt-in; remote targets rejected without opt-in; secrets do
not appear in console output, `report.json`, or `junit.xml` (search `hunter2`, `Bearer `,
API keys); large responses within limits (no unbounded memory); invalid corpus → zero outbound
requests (assert via spy/transport counter); a failed run never reports success (exit ≠ 0).

### 18.5 End-to-end acceptance

A script or pytest (marked) that runs in a clean venv:

1. `pip install <built wheel>` (+ `flask` for the legacy fixture).
2. Start/load example Flask + FastAPI apps.
3. `apiparity validate` → 0.
4. Compare equivalent apps → 0, reports written.
5. Mutate one FastAPI response → rerun → exit 1 with a specific mismatch.
6. Inspect `report.json` + `junit.xml`; assert no secrets.

### 18.6 Quality gates (all must pass)

```powershell
ruff check .            # zero findings
ruff format --check .   # clean
mypy src                # clean under configured strictness
pytest -q --cov=apiparity --cov-report=term-missing
python -m build         # sdist + wheel succeed
```

---

## 19. Documentation deliverables

- **README.md**: problem statement & users; short mismatch example; install; quick start;
  supported target types; corpus format; sample output; CI integration; safety warnings;
  current limitations; development setup; contribution instructions.
- **docs/architecture.md**: runner/adapter model; data models; normalization order;
  comparison semantics; error classification; resource init/cleanup; ADR-style decisions
  (dataclasses vs pydantic; async core; httpx transports; manual ASGI lifespan; fixture
  import-path strategy).
- **docs/configuration.md**: full schema, precedence, env-var handling.
- **docs/normalization.md**: every rule, defaults, JSON Pointer syntax, examples.
- **docs/safety.md**: opt-ins, side-effect limits, isolated-state requirement, loopback rules.
- **docs/troubleshooting.md**: transport failures vs behavioral diffs, missing variables,
  import errors, exit codes.
- **SECURITY.md**, **CONTRIBUTING.md**, **CHANGELOG.md** (0.1.0), **LICENSE** (MIT, author
  consistent with repo: Shashi Kundan <shashikundan0001@gmail.com>).
- Explicitly state: **no full-parity guarantees** — coverage of corpus, app state, external
  integrations, and environment limit what any comparison proves.

---

## 20. Packaging & release readiness

- `pyproject.toml`: setuptools ≥68 backend, `name = "apiparity-check"`, `version = "0.1.0"`
  (**distribution name** on PyPI is `apiparity-check` because the bare name `apiparity` was
  rejected as too similar to an existing project; the import package and the console script
  remain `apiparity`). (**single authoritative version location** — define in
  `src/apiparity/__init__.py` via
  `__version__` and reference dynamically, or in pyproject only with `__version__` read from
  metadata; pick one and document it), `requires-python = ">=3.11"`, deps
  `httpx>=0.27,<1`, `PyYAML>=6,<7`.
- Extras: `flask = ["flask>=3.0"]`, `fastapi = ["fastapi>=0.110"]`,
  `dev = ["pytest", "pytest-cov", "pytest-asyncio", "ruff", "mypy", "build", "flask",
  "fastapi", "uvicorn"]`.
- `[project.scripts] apiparity = "apiparity.cli:main"`.
- `tool.setuptools.packages.find` with `where = ["src"]`.
- Ruff config (lint+format) and mypy config (`python_version = 3.11`, `strict`-ish over
  `src/apiparity`) in pyproject.
- CI (`.github/workflows/ci.yml`): matrix Python 3.11/3.12/3.13 on ubuntu (+windows if
  practical): ruff check, ruff format --check, mypy, pytest, `python -m build`.
- Wheel must import and run CLI **outside the source checkout**.
- No committed credentials, no generated reports/dist committed (`.gitignore` at project root:
  `dist/ build/ *.egg-info/ __pycache__/ .pytest_cache/ .coverage htmlcov/ build-out/`).
- **Do not publish to PyPI, commit, push, or tag unless explicitly authorized.**

---

## 21. Definition of done (verify every line)

- [ ] Clean-environment install from built wheel works; CLI runs.
- [ ] `apiparity validate` validates a corpus without executing requests.
- [ ] Two equivalent implementations → all matched, exit 0.
- [ ] A deliberate response change → actionable mismatch (exact JSON Pointer), exit 1.
- [ ] JSON and non-JSON (text/binary) comparisons work.
- [ ] WSGI and ASGI in-process targets work with optional deps installed.
- [ ] HTTP mode works with neither Flask nor FastAPI installed (core deps only).
- [ ] Missing app / invalid config / failed requests classified correctly (exit 2 vs 1 vs 0).
- [ ] Secret redaction verified in console, JSON, JUnit outputs.
- [ ] Mutating-request and remote-target refusals verified.
- [ ] JSON + JUnit reports generated and parsed back successfully in tests.
- [ ] Resource cleanup on success and failure paths (no warnings).
- [ ] `ruff`, `mypy`, `pytest` all pass; ≥90% coverage on `comparison` + `safety`.
- [ ] sdist + wheel build; wheel installs in a clean venv; CLI works from it.
- [ ] All documentation present and accurate; limitations honestly listed.

## 22. Deferred features (must be listed in README "Limitations" and progress log)

Automatic Flask→FastAPI code conversion; LLM-generated patches; browser/HTML dashboard;
distributed replay workers; DB snapshot/restore; production traffic capture; automatic
rollback of side effects; non-HTTP protocols; pytest plugin; snapshot/expected-value
assertions; body persistence to disk; trusted-mode file uploads outside corpus root.

## 23. References for the implementing agent

- Flask testing docs (3.1.x): https://flask.palletsprojects.com/en/stable/testing/
- FastAPI testing: https://fastapi.tiangolo.com/tutorial/test/
- HTTPX transports (WSGITransport / ASGITransport lifecycle): https://www.python-httpx.org/transports/
- Pytest plugin development (future first-class pytest integration): https://docs.pytest.org/
- RFC 6901 JSON Pointer: https://datatracker.ietf.org/doc/html/rfc6901
