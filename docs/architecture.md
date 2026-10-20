# Architecture

How apiparity is put together, and why. The behavioral contract lives in
[`REQUIREMENTS.md`](../REQUIREMENTS.md); this document explains the implementation.

## Big picture

```
                    ┌──────────────────────────────────────────────┐
  CLI (argparse) ──►│  runner.py — sequential orchestrator         │
  Python API   ──►  │  validate → safety → startup → execute →     │
                    │  normalize → compare → summarize → cleanup   │
                    └──────┬───────────────────────────┬───────────┘
                           │                           │
              ┌────────────▼──────────┐   ┌────────────▼───────────┐
              │  targets/loader.py    │   │  corpus.py / config.py │
              │  module:attr → Target │   │  strict YAML/JSON      │
              └────────────┬──────────┘   │  ${VAR} resolution     │
                           │              └────────────────────────┘
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
  targets/http_target  wsgi_target      asgi_target
   (httpx.AsyncClient,  (WSGITransport   (ASGITransport +
    trust_env=False)     + daemon thread) manual lifespan driver)
        │                  │                  │
        └──────────────────┴──────────────────┘
                           │  ResponseSnapshot (per side)
                           ▼
              normalization.py → comparison.py → redaction.py
                           │  CaseResult → RunResult
                           ▼
              reports/{json,junit,terminal} + exit code
```

One rule governs the whole flow: **everything that can fail validation fails before the
first byte is sent.** Corpus schema, config schema, variable resolution, safety guards,
and target initialization all complete (or raise) before case 1 executes.

## Data flow (one run)

1. **Load** — `load_corpus(path)` parses YAML/JSON, validates every field strictly
   (unknown keys rejected), resolves `${VAR}` from the environment, and rejects file
   upload paths escaping the corpus directory. `load_config_file(path)` does the same
   for the project config.
2. **Merge** — precedence is CLI flags > config file > corpus `defaults` > built-ins.
   Safety flags are *grant-only*: they can turn a permission on, never off.
3. **Safety** — `evaluate_safety()` inspects case methods and target URLs. Mutating
   methods without the opt-in, or non-loopback HTTP targets without the opt-in, raise
   `SafetyError` (exit 2). Otherwise a safety summary is printed describing exactly
   what the run will do.
4. **Initialize targets** — `resolve_target()` imports the app module (calling it when
   `factory=True`) or builds an HTTP client. Any failure raises `TargetInitError`
   (exit 2); already-started targets are torn down. ASGI targets drive the lifespan
   protocol explicitly (see below).
5. **Execute sequentially** — for each enabled case: send to baseline, capture, send
   the identical request to the candidate, capture. A 4xx/5xx is a valid observation,
   never a reason to skip the other side. Disabled cases become `skipped`.
6. **Normalize** — drop ignored headers, drop ignored JSON pointers, apply
   whitespace/array rules. Produces *derived* snapshots; inputs are never mutated.
7. **Compare** — `compare_snapshots()` walks status, headers, and body (JSON
   structural diff with RFC 6901 pointers, or text/binary), producing typed
   `Difference` objects. Sensitive values are compared as `"[REDACTED]"` — a
   difference in a secret is detected but never displayed.
8. **Summarize** — counts, exit code (`0` match / `1` mismatch-or-failed / `2` no
   valid comparison), `RunResult` for reports.
9. **Cleanup** — `shutdown()` always runs (the `finally` in `run_comparison`),
   closing HTTP clients and sending ASGI lifespan shutdown, even on failure.

## Target adapters

All adapters implement the same `Target` ABC (`startup` / `execute` / `shutdown` /
`describe`) and share request-building and snapshot-capture code in `targets/base.py`.

### HTTP (`http_target.py`)

One `httpx.AsyncClient` per run. TLS verification on by default, explicit timeouts,
redirects off, **no retries**, `trust_env=False` — environment/system proxies must
never intercept comparison traffic. `describe()` strips URL userinfo so credentials
never reach reports.

### WSGI (`wsgi_target.py`)

`httpx.WSGITransport` runs the WSGI app synchronously, so each request is dispatched
to a dedicated daemon thread; the corpus is sequential, so only one thread is active
per target. Local transports cannot enforce network timeouts themselves, so the
per-request timeout (case → target → run default) is enforced on the event loop: at
the deadline the run stops waiting, returns a `timeout` snapshot, and the thread is
abandoned. A handler that never returns therefore cannot hang the run or process
exit; whatever it eventually produces is discarded. The trade-off (a timed-out
handler keeps consuming CPU until it returns) is documented in `docs/safety.md`.

**PEP 3333 fix.** httpx's `WSGITransport` passes the URL-*decoded* Unicode string as
`PATH_INFO`, but PEP 3333 requires the raw request bytes represented as a latin-1
"bytes-as-str". Frameworks like Werkzeug encode PATH_INFO back to latin-1 and decode
UTF-8 for routing, so an already-decoded non-ASCII path parameter arrives mangled.
The adapter wraps the app with a middleware that re-encodes
(`path.encode("utf-8").decode("latin-1")`), restoring real-server semantics. The
round-trip is the identity for ASCII paths.

### ASGI (`asgi_target.py`)

`httpx.ASGITransport` never runs the lifespan protocol, so this target drives it
manually on the same event loop, governed by `targets.<side>.lifespan`:

- **`auto`** (default): `lifespan.startup` is sent during `startup()`. A
  `lifespan.startup.failed` message **or an uncaught exception in the lifespan
  task** raises `TargetInitError` — fail closed, and startup errors are never
  concealed as "app doesn't support lifespan". An app whose lifespan task exits
  cleanly without ever sending a message *is* treated as lifespan-less and runs
  without startup hooks.
- **`required`**: like `auto`, but an app that never sends a startup message
  (clean exit included) raises `TargetInitError`. Use it to guarantee startup
  hooks actually ran.
- **`off`**: the lifespan protocol is not run at all — the explicit escape hatch
  for applications that crash or hang on lifespan scope.

`lifespan.shutdown` is always attempted during `shutdown()` (unless `off` or the
app was lifespan-less); every wait around the lifespan task is bounded, and a hung
startup fails closed after a 10 s timeout. The per-request timeout is enforced
with a cancellable deadline around the stream.

## Comparison semantics

- **Status** compared by default (`compare_status`).
- **Headers** case-insensitive (lowercased); repeated values compared as ordered
  lists. Default ignored set: `date`, `content-length`, `server`, `connection` —
  transport-generated, not application behavior. Everything else (including
  `content-type`, `location`, cache headers) is compared.
- **JSON** (when the body parses as JSON): structural diff, object key order ignored,
  array order preserved unless configured per-pointer (`preserve`/`sort`), null ≠
  missing, type changes detected, RFC 6901 JSON Pointer paths on every difference.
  Ignored pointers are applied *before* differences are produced. Capped at
  `max_differences_per_case` (default 100) with an info-level `normalization_note`.
- **Text** decoded per charset/`content-type`; no whitespace normalization unless
  enabled.
- **Binary** compared bytewise.
- **Size limit** (default 5 MiB): a truncated body can never report `match` —
  `response_too_large` is emitted so an incomplete comparison is always visible.
- **Timing** is captured as diagnostic metadata; it only fails a case when
  `timing.mode: fail` with `max_ms` configured (default `off`).

Difference categories are exact strings (`status_code`, `header_added`,
`header_removed`, `header_value`, `body_type`, `json_key_added`, `json_key_removed`,
`json_value`, `json_type`, `text_body`, `binary_body`, `transport_error`, `timeout`,
`response_too_large`, `normalization_note`, `timing`) with severities
`error` / `warning` / `info`. Transport and configuration failures stay distinct from
behavioral differences: a connection failure is never evidence that business logic
differs, and a case with a transport/timeout error is `failed`, not `mismatch`.

## Error classification

| Situation | Mechanism | Outcome |
|---|---|---|
| Bad corpus/config/variables | `CorpusError` / `ConfigError` / `VariableResolutionError` | exit 2, nothing sent |
| Safety refusal | `SafetyError` | exit 2, nothing sent |
| Target import/lifespan failure | `TargetInitError` | exit 2, started targets torn down |
| Connection refused / timeout per case | `error_kind` on the snapshot | case `failed`, exit 1 |
| Behavioral difference | `Difference` with `severity: error` | case `mismatch`, exit 1 |
| Unexpected internal error | caught in `cli.main` | exit 2 (sanitized message) |

All exception messages pass through `sanitize_exception_message()` (strips URL
userinfo, credential-shaped substrings) before display.

## Design decisions (ADR-style)

| # | Decision | Rationale |
|---|---|---|
| 1 | stdlib `dataclasses` + hand validation; no pydantic at runtime | Runtime dependency budget: only `httpx` + `PyYAML` |
| 2 | Async core (`compare`) + `compare_sync` wrapper | One lifecycle; httpx is async; CLI uses `asyncio.run` |
| 3 | httpx `WSGITransport`/`ASGITransport` + **manual ASGI lifespan driver** | ASGITransport never fires lifespan; startup failures must fail closed |
| 4 | argparse CLI | No framework needed for two subcommands |
| 5 | stdlib `json` / `xml.etree` for reports | Small footprint; JUnit escaping under our control |
| 6 | Sequential execution only | Corpus ordering semantics (create-then-read) depend on it |
| 7 | Redaction applied when differences are constructed and again at report time | Secrets never enter `Difference` values; defense in depth at the output layer |
| 8 | Fixture apps imported via `conftest.py` path injection | Keeps `module:app` loader realistic without installing fixtures |
| 9 | HTTP targets use `trust_env=False` | A parity checker must send requests exactly where the user pointed them; system proxies must not intercept or leak traffic |
| 10 | WSGI adapter re-encodes PATH_INFO per PEP 3333 | httpx's WSGITransport violates the spec; without the fix, Werkzeug mangles non-ASCII path params |
| 11 | `connection` in the default ignored headers | Hop-by-hop transport header; not application behavior |
| 12 | Config normalization: absent `ignored_headers` keeps built-in defaults; explicit value replaces | Omitting a key must not silently re-enable transport-header diffs |
| 13 | In-process deadlines: WSGI waits on a detached daemon thread, ASGI cancels its stream; ASGI lifespan policy `auto`/`required`/`off` | A hung handler or lifespan task must never hang the run, and startup errors must never be concealed as "lifespan-less" |
| 14 | Quarantine: WSGI non-cancellable timeout or ASGI un-cancellable/loop-blocking timeout marks target quarantined; later requests refused locally (`request not sent`); redirect hops checked hop-by-hop (`RemoteGuardedTransport`) with redacted messages | Prevents re-entering a target with mutated in-process state and prevents redirect escape of the loopback policy |
| 15 | Secret non-disclosure: redact URL userinfo + sensitive query params, headers/JSON by policy; httpx `json=None` cannot send explicit JSON null (send `b"null"` + `application/json`); corpus `json: null` tracked by presence (`has_json_body`) | Zero secrets in artifacts; explicit JSON null bodies reach the wire intact |

## Resource lifecycle

`compare()` owns everything. Callers never open or close clients. HTTP targets hold
one `AsyncClient` per run, closed in `shutdown()`. WSGI targets close their
`Client` on a worker thread. ASGI targets always attempt `lifespan.shutdown`, then
cancel a hung lifespan task. `_shutdown_targets` runs in a `finally`, so cleanup
happens on success, on case failures, and on startup failures (partially started
targets are shut down in reverse order).

## Testing architecture

- `tests/unit/` — pure logic: corpus/config validation, comparison diffing,
  redaction, normalization, safety rules, report serialization, CLI argument errors.
- `tests/integration/` — full runs against fixture apps in `tests/fixtures`
  (Flask baseline + FastAPI candidate + deliberate mismatch variant), real
  Werkzeug/uvicorn servers on loopback ephemeral ports, stalled/closed sockets for
  timeout and refusal classification.
- `tests/integration/test_security.py` — secret non-disclosure across all report
  channels, safety refusals before any request, userinfo leakage, failed-run
  never-passes.
- `tests/test_cli.py` — exit-code contract end to end through `cli.main`.
