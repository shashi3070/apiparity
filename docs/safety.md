# Safety

apiparity replays recorded requests against real applications. Replaying has
consequences. Every guard below is implemented in `src/apiparity/safety.py`,
enforced by the runner **before the first request**, and tested in
`tests/unit/test_safety.py` and `tests/integration/test_security.py`. When in doubt,
the tool refuses.

## The two explicit opt-ins

Both fail **closed**. A refusal raises `SafetyError` → exit `2`, and nothing is sent.

### 1. Mutating requests

Any case whose method is outside `{GET, HEAD, OPTIONS}` requires:

```bash
apiparity compare ... --allow-mutating-requests
```

```yaml
# config file — same grant, via file
safety:
  allow_mutating_requests: true
```

CLI and config are grant-only and merge upward: the run proceeds if *either* grants.
There is no way to turn a grant off from a lower-precedence source.

**There is no rollback.** A `POST /api/orders` really creates an order. apiparity
does not snapshot databases, does not wrap transactions, and will not claim to undo
anything. Before sending, it prints a safety summary naming the mutating methods it
is about to run:

```
! SAFETY: 3 mutating request(s) will be replayed (POST, PUT, DELETE).
  apiparity does not roll back side effects. Ensure both targets use isolated,
  identically seeded test state.
```

### 2. Remote (non-loopback) targets

An HTTP target whose host is not loopback requires:

```bash
apiparity compare ... --allow-remote-targets
```

Loopback means: `localhost`, `127.0.0.0/8`, `::1`. Everything else — including
private ranges like `10.x`, `192.168.x`, RFC 6177-reserved `169.254.x`, DNS names
resolving off-box, and any public host — is "remote". A private IP is **never**
assumed to be a test environment: internal staging systems receive real traffic
too.

In-process (WSGI/ASGI) targets are exempt: they never cross a network.

## Other built-in guarantees (no opt-out)

| Guarantee | Detail |
|---|---|
| TLS verification on | `verify_tls: true` default; opt-out only via explicit config |
| Timeouts on every HTTP request | Case → target → corpus → 30 s built-in fallback |
| No redirects | `follow_redirects: false` everywhere by default; a 3xx is compared as a response |
| No retries | Failed requests fail once; write traffic is never re-sent |
| Response size limit | Default 5 MiB; oversize bodies truncated + flagged, never silently matched |
| No environment proxies | HTTP targets use `trust_env=False`; system/HTTP_PROXY cannot redirect or observe traffic |
| No path escape | Multipart file paths must resolve inside the corpus directory |
| No code execution from corpus content | Corpus/config are data; no `eval`, no shell, no template execution |
| Malformed input fails first | All validation (corpus, config, variables, safety, target init) completes before case 1 |
| Secrets never printed | Redaction of headers, JSON pointers, URL userinfo in reports, terminal, and exception messages |
| Failed run never exits 0 | Exit code derives only from a successfully produced `RunResult` |

## In-process targets: the honest fine print

`--old-kind wsgi` / `--new-kind asgi` **import your application module inside the
apiparity process**:

- Import-time code runs (module-level DB connections, `create_all()`, env reads,
  cache warmup, ...).
- The app may open real database connections, publish to real queues, or call real
  third-party APIs — exactly as it would under any test client.
- WSGI apps run on worker threads (no network boundary); ASGI lifespan startup hooks
  run. Startup failure fails the run (`TargetInitError`, exit 2) — there is no
  "continue without startup" fallback.

Point fixture targets at test configuration (test databases, fake credentials,
`ENVIRONMENT=test`). apiparity does not sandbox your code.

## State isolation is your job

For any run that writes (including endpoints that mutate server state on GET):

- Both targets must be **isolated** from production data.
- For meaningful comparison of write-then-read flows, both must be **identically
  seeded** before the run (same database snapshot, same fixtures).
- Corpus order is sequential and significant: `POST /orders` then `GET /orders`
  depends on the write succeeding, in order, on each side independently.

apiparity never connects to a database itself and never restores snapshots.

## Loopback and network rules, summarized

```
wsgi/asgi targets   → always allowed (in-process)
http + localhost    → allowed (GET/HEAD/OPTIONS)
http + localhost    → needs --allow-mutating-requests (POST/PUT/PATCH/DELETE)
http + any other IP → also needs --allow-remote-targets
http + userinfo URL → allowed, but credentials are stripped from all output
```

## Redaction

Redaction is a **release-blocking** guarantee: no secret value may appear in
`report.json`, `junit.xml`, terminal output, logs, or exception messages.

### What is redacted

**Headers** (name match, case-insensitive) — always:

`authorization`, `proxy-authorization`, `cookie`, `set-cookie`, `x-api-key`

plus any header whose name **contains** one of: `token`, `secret`, `password`,
`apikey`, `api-key`, `credential` (e.g. `X-Auth-Token`, `Db-Password`).

Configure additional exact names via `redaction.sensitive_headers` /
`redaction.header_name_substrings` (setting either **replaces** that default list).

**JSON values** at configured pointers — default:

`/credentials/token`, plus `redaction.sensitive_json_pointers` (replace semantics;
add your own paths such as `/data/password`, `/user/ssn`).

**URLs** — userinfo (`http://user:pass@host/...`) is stripped from target
descriptions and from transport exception messages before display.

**Query strings** — credential-shaped parameters are masked in any rendered URL:
`?password=hunter2` → `?password=[REDACTED]` (keys containing password/secret/
token/apikey/api-key/credential are masked).

### How it behaves

- Redaction is applied **when a `Difference` is constructed** — a sensitive value
  never enters a `Difference.baseline_value` / `candidate_value`. Report writers and
  the terminal renderer then apply the same rules again at the output boundary
  (defense in depth).
- Two sensitive values that **differ** are still reported — with both sides shown as
  `"[REDACTED]"` and the message `"Sensitive field differs"`. You learn *that* the
  credential changed, never what it is.
- Variable-resolution errors name the missing **variable** and its location; they
  never echo any resolved value.
- Sanitized transport exceptions: `httpx` errors embed URLs and can embed headers;
  apiparity strips userinfo and credential-shaped query params before the message
  reaches a report.

### Verified by test

`tests/integration/test_security.py` runs a full comparison with known secrets
(`hunter2`, `secret-key-123`, `Bearer ` values) in headers, bodies, and URLs, then
greps the **entire** `report.json`, `junit.xml`, and captured terminal output —
asserting the secret substrings are absent even with `include_body_previews: true`.
Body previews are **off by default**; when enabled they are still redacted.

## Reporting vulnerabilities

See [SECURITY.md](../SECURITY.md). Never open a public issue for a vulnerability.
