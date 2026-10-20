# apiparity

**Behavioral parity checker for API migrations.** Replay one request corpus against a
baseline (old) and a candidate (new) implementation, and report exactly where behavior
differs — with RFC 6901 JSON Pointer paths, not vague "something changed" output.

Built for teams migrating a Flask (or any WSGI) service to FastAPI (or any ASGI) service,
re-versioning an API, or refactoring a handler: you describe the traffic once, run it
against both implementations, and get an actionable diff report plus CI-friendly exit
codes.

```
  baseline (old app)  ──►  same requests  ──►  candidate (new app)
                                │
                                ▼
              status · headers · JSON structure · text/binary bodies
                                │
                                ▼
        report.json · junit.xml · terminal summary   (secrets redacted)
```

> **No full-parity guarantees.** apiparity proves that the requests *in your corpus*
> behave the same under the conditions of the run. Coverage of the corpus, application
> state, external integrations, and environment limits what any comparison can prove.
> Treat a green run as strong evidence, not proof of total equivalence.

## The problem it solves

Migrating an API framework (Flask → FastAPI, Django → Litestar, v1 → v2 of your own code)
is where regressions hide: a status code quietly changes from 422 to 400, a field renames,
a header disappears, an error body gains a `detail` wrapper. Unit tests cover what you
thought to assert; production traffic covers the rest — after it breaks.

apiparity lets you capture the request shapes that matter (a corpus file you keep in
version control) and replay them against both implementations, diffing every observable
behavior. It works **in-process** (import both apps, no ports, no deploys) or over
**HTTP** (two running services on loopback), and it fails closed: it will not replay
mutating traffic or talk to non-loopback hosts without explicit opt-ins.

## Install

```bash
pip install apiparity
```

Runtime dependencies: `httpx`, `PyYAML` — nothing else. Optional extras for in-process
targets:

```bash
pip install "apiparity[flask]"    # WSGI targets (Flask, Werkzeug, ...)
pip install "apiparity[fastapi]"  # ASGI targets (FastAPI, Starlette, ...)
```

Requires Python ≥ 3.11.

## Quick start

**1. Write a corpus** (`requests.yaml`) — the requests you want compared:

```yaml
version: 1
normalization:
  ignored_json_pointers:
    - /metadata/request_id     # uuid that changes every run
    - /metadata/generated_at   # timestamp
cases:
  - id: customer-detail
    method: GET
    path: /api/customers/c-1001
  - id: create-order
    method: POST
    path: /api/orders
    json: {item: "widget", quantity: 2}
```

**2. Validate without sending anything:**

```bash
apiparity validate --corpus requests.yaml \
    --old-app legacy_flask.app:app --old-kind wsgi \
    --new-app migrated_fastapi.app:app --new-kind asgi \
    --allow-mutating-requests
```

**3. Compare:**

```bash
apiparity compare --corpus requests.yaml \
    --old-app legacy_flask.app:app --old-kind wsgi \
    --new-app migrated_fastapi.app:app --new-kind asgi \
    --allow-mutating-requests --output build/apiparity
```

Equivalent implementations → exit `0`, all cases matched. A behavioral difference →
exit `1` with the exact pointer:

```
  [02/08] customer-detail              MISMATCH
      [error] json_value — /customer/status: value differs
          baseline="active"  candidate="inactive"
```

Invalid config, a safety refusal, or a target that fails to start → exit `2` (no valid
comparison was produced). A run that failed to execute never returns `0`.

This repository ships runnable examples against fixture apps in `tests/fixtures` —
see [`examples/README.md`](examples/README.md).

## Target types

| Kind | Flag | How it runs | Use for |
|---|---|---|---|
| `http` | `--old-url` / `--new-url` | Real HTTP via httpx (loopback enforced by default) | Two deployed/local services |
| `wsgi` | `--old-app mod:attr --old-kind wsgi` | In-process via `httpx.WSGITransport` | Flask, Werkzeug, any WSGI app |
| `asgi` | `--new-app mod:attr --new-kind asgi` | In-process via `httpx.ASGITransport` + manual lifespan driver | FastAPI, Starlette, any ASGI app |

Mixed modes are fine (WSGI baseline vs ASGI candidate — the flagship use case). Apps can
be imported directly (`legacy_flask.app:app`) or via a factory
(`legacy_flask.app:create_app --old-factory`). In-process targets execute the application's
**import-time code** in your process and may touch real databases and external services —
point them at test configuration.

HTTP targets never honor environment/system proxy settings: requests go exactly where you
point them.

## Corpus format (YAML or JSON, schema `version: 1`)

```yaml
version: 1
defaults:                     # optional, applied to every case
  timeout_seconds: 10
  follow_redirects: false
normalization:                # optional, corpus-level rules
  ignored_json_pointers: ["/metadata/request_id"]
cases:
  - id: unique-id             # required, unique
    description: optional text
    method: GET               # GET HEAD OPTIONS POST PUT PATCH DELETE
    path: /api/things         # must start with /
    query: {limit: ["3"], tags: ["a", "b"]}   # repeated values preserved in order
    headers: {Authorization: "Bearer ${API_TOKEN}"}  # ${VAR} from environment
    json: {key: value}        # or data: "raw" or files: [...] — one body only
    timeout_seconds: 5        # optional per-case override
    enabled: true             # false → skipped with reason
```

- **Strict validation**: unknown fields, duplicate ids, conflicting bodies, malformed
  YAML/JSON, and file paths escaping the corpus directory are all rejected **before any
  request is sent**.
- **`${VAR}` interpolation** in string values (headers, query, bodies, paths): a missing
  variable aborts the run; resolved secret values are never logged.
- Full schema: [`docs/configuration.md`](docs/configuration.md) and
  [`REQUIREMENTS.md`](REQUIREMENTS.md) §7.

## Configuration file (optional, `--config apiparity.yaml`)

```yaml
version: 1
targets:
  old: {kind: http, url: "http://127.0.0.1:5000"}
  new: {kind: asgi, app: "migrated_fastapi.app:app"}
comparison:
  max_differences_per_case: 50
  response_size_limit_bytes: 1048576
  timing: {mode: "off", max_ms: null}      # off | warn | fail
normalization:
  ignored_headers: []                      # replaces defaults when set
  ignored_json_pointers: ["/metadata/request_id"]
redaction:
  sensitive_json_pointers: ["/credentials/token", "/data/password"]
safety:
  allow_mutating_requests: false
  allow_remote_targets: false
reporting: {json: true, junit_xml: true, terminal: true}
```

Precedence (highest wins): **CLI flags > config file > corpus `defaults` > built-ins.**
Full schema and every knob: [`docs/configuration.md`](docs/configuration.md).
Normalization rules in depth: [`docs/normalization.md`](docs/normalization.md).

## Reports

| Output | File | Purpose |
|---|---|---|
| JSON | `report.json` | Canonical, schema-versioned (`schema_version: 1`), machine-readable |
| JUnit XML | `junit.xml` | One `<testcase>` per request case — works in any CI test viewer |
| Terminal | stdout | Human summary; `--verbose` for per-case detail, `--quiet` for final line |

- Differences are categorized (`status_code`, `json_key_removed`, `header_value`,
  `text_body`, `transport_error`, `timeout`, ...) with RFC 6901 JSON Pointer paths.
- **Secrets are redacted before anything reaches a report or the terminal**: sensitive
  headers (`authorization`, `cookie`, `set-cookie`, `x-api-key`, `proxy-authorization` +
  name-pattern matches like `*token*`, `*secret*`, `*password*`), configured JSON
  pointers (`/credentials/token` by default), and URL userinfo in error messages are
  masked as `[REDACTED]`. Sensitive values that *differ* are still detected — you see
  that they differ, never what they are.
- Response body previews are **off by default** (`--include-bodies` to enable; still
  redacted).
- JSON bodies are compared structurally (key order ignored, array order preserved by
  default); text bodies decode per charset; binary bodies compare bytewise. Responses
  larger than `response_size_limit_bytes` (5 MiB default) are truncated and the case
  can never report as a match.

## CI integration

Exit codes are the contract:

| Code | Meaning |
|---|---|
| `0` | Every enabled case matched (skipped cases allowed) |
| `1` | ≥ 1 behavioral mismatch or configured failure |
| `2` | Invalid config/corpus, safety refusal, or target init failure — no valid comparison was produced |

```yaml
# GitHub Actions fragment
- run: pip install apiparity flask fastapi uvicorn
- run: apiparity validate --corpus tests/parity/corpus.yaml --config tests/parity/config.yaml
- run: >
    apiparity compare --corpus tests/parity/corpus.yaml --config tests/parity/config.yaml
    --output build/apiparity --quiet
- uses: actions/upload-artifact@v4
  if: always()
  with:
    name: parity-reports
    path: build/apiparity/
```

A full workflow (lint, typecheck, tests, build on 3.11–3.13) lives in
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Safety

apiparity is a *replayer*, and replay has consequences. The guards fail closed —
see [`docs/safety.md`](docs/safety.md) for the full model:

- **Mutating methods** (`POST`/`PUT`/`PATCH`/`DELETE` and anything outside
  `{GET, HEAD, OPTIONS}`) require `--allow-mutating-requests`. There is no rollback;
  a run prints a safety summary naming exactly what it is about to do.
- **Non-loopback HTTP targets** require `--allow-remote-targets`. A private IP is never
  assumed to be a test environment.
- TLS verification on, timeouts on every request, **no redirects** and **no retries**
  by default.
- In-process targets import your application module: import-time code runs, real
  databases may be touched. Use test configuration.
- Baseline and candidate must use **isolated, identically seeded state** for
  state-changing comparisons. Even GET can have side effects.
- apiparity cannot roll back side effects and does not claim to.

Report vulnerabilities per [`SECURITY.md`](SECURITY.md) — never in a public issue.

## Python API

The primary API is async; `compare_sync` is a convenience wrapper:

```python
from apiparity import AppConfig, TargetConfig, compare_sync

config = AppConfig(
    old_target=TargetConfig(kind="wsgi", app="legacy_flask.app:app"),
    new_target=TargetConfig(kind="asgi", app="migrated_fastapi.app:app"),
)
result = compare_sync(corpus="requests.yaml", config=config)
print(result.summary.matched, result.summary.exit_code)
for case in result.mismatches:
    for diff in case.differences:
        print(case.case_id, diff.category, diff.path, diff.baseline_value, diff.candidate_value)
```

Async form: `await compare(corpus=..., config=..., old_target=..., new_target=...)`.
Target clients are opened and closed inside `compare*` — the caller never manages
lifecycle. Invalid input raises `ConfigError` / `CorpusError` / `VariableResolutionError` /
`SafetyError` / `TargetInitError` (all subclasses of `ApiParityError`); infrastructure
failures appear per-case as `failed` results with `transport_error`/`timeout`
differences, not as exceptions. See `help(apiparity)` and
[`docs/architecture.md`](docs/architecture.md).

## Limitations

This is a behavioral *parity checker*, not a converter or a test framework. Deliberately
out of scope for 0.1.0:

- **No code conversion**: it never generates Flask→FastAPI patches (LLM or otherwise).
- **No HTML report**: JSON, JUnit XML, and terminal only.
- **No pytest plugin**: use the exit codes (or the Python API) from pytest.
- **No snapshot/blessing flow**: the baseline is never silently updated to make failures
  disappear.
- **No distributed replay or concurrency**: cases run sequentially in corpus order —
  ordering semantics (create-then-read) depend on it.
- **No database snapshot/restore, traffic capture, or automatic rollback**: state
  management is your responsibility.
- **No non-HTTP protocols**; **no body persistence to disk**; multipart file uploads are
  restricted to files inside the corpus directory (no trusted mode).

## Development

```powershell
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"
```

Quality gates (all must pass — see [`CONTRIBUTING.md`](CONTRIBUTING.md)):

```powershell
ruff check .
ruff format --check .
mypy src
pytest -q --cov=apiparity --cov-report=term-missing
python -m build
```

Coverage gate: ≥ 90% on `comparison.py` and `safety.py`.

## Documentation

| Document | Contents |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Runner/adapter model, data flow, design decisions |
| [`docs/configuration.md`](docs/configuration.md) | Config + corpus schema, precedence, env vars |
| [`docs/normalization.md`](docs/normalization.md) | Every normalization rule with examples |
| [`docs/safety.md`](docs/safety.md) | Opt-ins, loopback rules, side-effect policy |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Transport vs behavioral diffs, common errors |
| [`REQUIREMENTS.md`](REQUIREMENTS.md) | Full behavioral specification |

## License

MIT — see [LICENSE](LICENSE). © 2026 Shashi Kundan.
