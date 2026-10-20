# Configuration

Two files configure a run: the **corpus** (what to send) and the optional **config**
(how to compare, where targets live, safety grants). Both are YAML or JSON with
`version: 1`, both are validated strictly (unknown keys are errors), both may use
`${VAR}` placeholders.

Precedence, highest wins:

```
CLI flags  >  config file  >  corpus `defaults`  >  built-in defaults
```

Safety flags are **grant-only**: a lower-precedence source can grant `true`, but
nothing can revoke a grant made on the command line or in the config file.

## Project config file (`--config`)

### Full schema

```yaml
version: 1

defaults:                        # optional — corpus defaults override these
  timeout_seconds: 10.0          # > 0
  follow_redirects: false        # false by default everywhere

safety:                          # optional
  allow_mutating_requests: false # CLI flag overrides upward only
  allow_remote_targets: false

comparison:                      # optional
  compare_status: true
  compare_headers: true
  compare_body: true
  max_differences_per_case: 100    # > 0
  response_size_limit_bytes: 5242880  # > 0
  timing:                          # note: nested, NOT flat keys
    mode: "off"                    # "off" | "warn" | "fail"  (quote "off"!)
    max_ms: null                   # number required when mode: fail

normalization:                   # optional
  ignored_headers: []            # ONLY overrides defaults when this key is present
  ignored_json_pointers: []      # RFC 6901 pointers, e.g. /metadata/request_id
  whitespace:
    text: false                  # normalize whitespace in text bodies
  array_order:                   # per-pointer array handling
    /results: sort               # "preserve" (default) | "sort"

redaction:                       # optional — REPLACES defaults when set
  sensitive_headers: [authorization, proxy-authorization, cookie, set-cookie, x-api-key]
  sensitive_json_pointers: ["/credentials/token"]
  header_name_substrings: [token, secret, password, apikey, api-key, credential]

reporting:                       # optional
  json: true                     # write report.json
  junit_xml: true                # write junit.xml
  terminal: true
  include_body_previews: false   # body previews in JSON report; still redacted

targets:                         # optional — CLI --old-app/--old-url etc. override
  old:
    kind: http                   # "http" | "wsgi" | "asgi"
    url: "http://127.0.0.1:5000" # kind: http
    app: "legacy_flask.app:app"  # kind: wsgi|asgi — module:attribute
    factory: false               # true → module:factory(), call the attribute
    verify_tls: true
    timeout_seconds: null        # null → effective config/corpus default
    label: "baseline"
  new:
    kind: asgi
    app: "migrated_fastapi.app:app"
```

### Rules that surprise people

- **`timing: {mode: "off"}`** — bare `off` is YAML `false`, not the string. Always
  quote it: `mode: "off"`. (The failure mode: `mode: off` is rejected as an unknown
  type, loudly.)
- **`normalization.ignored_headers`** — if the key is *absent*, the built-in defaults
  (`date`, `content-length`, `server`, `connection`) stay in effect. If the key is
  *present — even empty* — it **replaces** the defaults entirely (set
  `["*"]` + `compare_headers: false` semantics differently: to ignore one extra
  header while keeping defaults, list all of them explicitly).
- **`redaction.*` lists** — same replace-not-extend semantics; list defaults plus your
  extras if you add any.
- **`targets` app specs** must match `module.path:attribute_name`. Factories are
  invoked; the resulting app is used (works for Flask `create_app()` and FastAPI
  `create_app()`).
- **CLI target flags always win** over `targets:` in the file.

### CLI ↔ config flag map

| Config path | CLI flag(s) |
|---|---|
| `targets.old.{kind,url,app,factory}` | `--old-app` / `--old-url` / `--old-kind` / `--old-factory` |
| `targets.new.*` | `--new-app` / `--new-url` / `--new-kind` / `--new-factory` |
| `safety.allow_mutating_requests` | `--allow-mutating-requests` |
| `safety.allow_remote_targets` | `--allow-remote-targets` |
| `comparison.*` | `--compare-status/--no-compare-status` etc., `--max-differences`, `--response-size-limit`, `--timing-mode`, `--timing-max-ms` |
| `normalization.*` | `--ignore-header` (repeatable), `--ignore-pointer` (repeatable) |
| `reporting.*` | `--json/--no-json`, `--junit/--no-junit`, `--terminal/--no-terminal`, `--include-bodies` |
| (output dir) | `--output DIR` |

## Corpus file (`--corpus`)

### Full schema

```yaml
version: 1

defaults:                        # optional — applies to every case
  timeout_seconds: 10.0
  follow_redirects: false
  compare_headers: true
  compare_body: true
  compare_status: true
  max_differences_per_case: 100
  response_size_limit_bytes: 5242880

normalization:                   # optional — corpus-level rules + timing
  ignored_headers: [x-internal-debug]
  ignored_json_pointers: ["/metadata/request_id", "/metadata/generated_at"]
  whitespace: {text: false}
  array_order: {/results: sort}
  timing: {mode: "warn", max_ms: 250}   # folded into defaults

cases:
  - id: unique-id                # REQUIRED, unique across the corpus
    description: optional free text
    method: GET                  # one of KNOWN_METHODS (default GET)
    path: /api/things            # REQUIRED, must start with /
    query:                       # optional; scalars or lists (repeated params)
      limit: 3
      tags: [a, b]
    headers:                     # optional; values may contain ${VAR}
      Authorization: "Bearer ${API_TOKEN}"
    json: {item: widget}         # exactly ONE of json | data | files
    data: "raw body text"        # string body (content-type via headers)
    files:                       # multipart — paths relative to the corpus file
      - field: document
        path: ./uploads/doc.pdf  # MUST stay inside the corpus directory
        content_type: application/pdf
    timeout_seconds: 5.0         # per-case override
    enabled: true                # false → case skipped with reason
    compare_headers: true        # per-case override
    compare_body: true           # per-case override
```

### Validation (all before any request)

| Error | Example trigger |
|---|---|
| Unknown top-level or case key | `method: GET, verb: POST` |
| Duplicate `id` | two cases with `id: health` |
| Conflicting bodies | both `json:` and `data:` set |
| Bad method / path | `method: FETCH`; `path: api/x` (no leading `/`) |
| Bad query value type | `limit: [1, "x"]` |
| File escaping corpus root | `path: ../../etc/passwd` |
| Missing `${VAR}` | `Authorization: Bearer ${TOKEN}` with `TOKEN` unset — names the variable, never a value |
| Malformed YAML/JSON | tab indentation, trailing comma |

### `${VAR}` interpolation

- Syntax: `${NAME}` where `NAME` matches `[A-Za-z_][A-Za-z0-9_]*`.
- Applied recursively to **string values and string keys** in the whole corpus and
  config, after structural validation, before anything runs.
- Source: the process environment. A missing variable aborts with exit 2 and an error
  naming the variable and its location — never the resolved value of anything else.
- To pass a literal `${...}`, use a different spelling; there is no escape sequence.

### Per-case override semantics

`compare_headers` / `compare_body` / `timeout_seconds` resolve as:
**case value → corpus `defaults` → config `defaults` → built-in**.
`compare_status` has no per-case key; use config/CLI.

## Environment variables

apiparity reads **only** the variables you reference via `${VAR}`. It does not use
environment variables for its own configuration (no `APIPARITY_*` namespace), and HTTP
targets ignore proxy environment variables entirely (`trust_env=False`).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All enabled cases matched (skipped allowed) |
| 1 | ≥ 1 mismatch, failed request, or configured timing failure |
| 2 | Config/corpus invalid, safety refusal, target init failure, internal error — **no valid comparison was produced** |
