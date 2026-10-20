# Troubleshooting

Quick diagnosis guide. Start with the exit code, then the report, then this page.

## Exit codes

| Code | Meaning | First thing to check |
|---|---|---|
| `0` | Every enabled case matched | — |
| `1` | Behavioral mismatch, failed request, or `timing.mode: fail` breach | Terminal diff / `report.json` |
| `2` | **No valid comparison was produced**: bad config/corpus, safety refusal, target init failure, internal error | The error message on stderr |

A run that could not execute never returns `0`. If you got `0` and expected failures,
the requests may not be exercising the code you think (see [below](#cases-match-but-i-expected-them-to-differ)).

## Transport failures vs behavioral differences

These are different categories on purpose — a connection problem is not evidence
that business logic differs.

| What you see | Category | Case status | Typical cause |
|---|---|---|---|
| `status_code`, `json_*`, `header_*`, `text_body`, `binary_body` differences | Behavioral | `mismatch` | Real regression or intentional change |
| `transport_error` with message about connect/refused/DNS | Infrastructure | `failed` | Server not running; wrong port; app didn't start |
| `timeout` (or `httpx.ReadTimeout`-derived message) | Infrastructure | `failed` | Server hung; endpoint slower than `timeout_seconds` |
| `response_too_large` | Guard | never `match` | Body exceeded `response_size_limit_bytes` (default 5 MiB) |

**Fix order for `failed` cases:** (1) curl/wget the baseline URL yourself, (2) check
the app logs, (3) raise `timeout_seconds` only after confirming the endpoint is just
slow, (4) re-run. Do not "fix" a transport failure by adding normalization rules.

## Common errors, by message

### `SafetyError` / "mutating" / "remote"

```
case 'create-order' uses mutating method POST; pass --allow-mutating-requests to allow
```

By design. Replaying writes is opt-in and there is no rollback. Verify both targets
are isolated test environments, then add the flag (see [safety.md](safety.md)).

```
http target host '10.1.2.3' is not loopback; pass --allow-remote-targets to allow
```

Same story for network targets. Prefer pointing at `localhost`.

### `TargetInitError` / import failures

```
cannot import app 'legacy_flask.app:app': No module named 'legacy_flask'
```

The module isn't importable from the current working directory / `PYTHONPATH`.
apiparity never modifies `sys.path` for you — run from the app's directory or set
`PYTHONPATH`. (This is also why integration tests inject the fixtures directory in
`conftest.py`.)

```
cannot import app 'myapp.main:create_app': module 'myapp.main' has no attribute 'create_app'
```

The attribute name after `:` must exist on the module. Factories: the attribute is
*called* (`create_app()`); plain objects are used as-is. Remember the
`module:attribute` form — `myapp.main:create_app()` with parens is invalid syntax
and rejected at parse time.

```
ASGI lifespan startup failed: ...
```

Your app's `lifespan`/startup handler raised. apiparity fails closed — it will not
run requests against an app that failed its own startup. Read the chained message
(missing env var, unreachable DB, ...). A lifespan task that dies with an exception
is reported the same way — it is never mistaken for an app that simply doesn't
implement lifespan. Apps that genuinely don't implement lifespan keep working under
the default `lifespan: "auto"`; apps that crash or hang on lifespan scope need
`lifespan: "off"` on that target, and `lifespan: "required"` turns a missing
lifespan protocol into a hard error.

### `CorpusError` / `ConfigError` / schema violations

```
unknown top-level key(s): verison   (did you mean 'version'?)
```

Strict validation rejects unknown keys everywhere — top level, `defaults`,
`comparison`, each case. Fix the typo; this catches silent no-ops like
`ignored_pointer:` (singular) that would otherwise leave diffs un-ignored.

```
duplicate case id: 'health'
case 'create-order': body must use exactly one of json, data, files
```

Self-explanatory; ids must be unique (they key the reports).

```
config file not found: apiparity.yaml
```

`--config` paths are resolved from the working directory, not the corpus directory.

### `VariableResolutionError`

```
environment variable 'API_TOKEN' referenced at cases[3].headers.Authorization is not set
```

The name of the missing variable and where it was referenced — never any resolved
value. Export the variable before running:

```powershell
$env:API_TOKEN = "..."   # PowerShell
apiparity compare ...
```

There is no escape syntax for a literal `${...}` inside a corpus string; use a
different spelling.

### YAML surprises

- **`mode: off`** — bare `off` parses as boolean `false`. Quote it:
  `mode: "off"`. Same for `"yes"`, `"no"`, `"on"`, `"null"`.
- **Tabs** — YAML forbids tabs for indentation. Use spaces.
- **Paths** — `/api/x` in unquoted YAML is fine (a leading `/` is a plain scalar),
  but quote paths containing special characters or leading `?`/`#`.

### File upload path escape

```
file path for field 'document' escapes the corpus directory: .../secrets/x.pdf
```

Multipart paths are resolved relative to the corpus file and confined to its
directory tree. Move the file next to the corpus; there is intentionally no
trusted-mode override.

## Cases match but I expected them to differ

1. **The difference is in an ignored header.** Defaults drop `date`,
   `content-length`, `server`, `connection`. List headers explicitly in
   `normalization.ignored_headers` if you replaced the defaults and want them back.
2. **The field is covered by an ignored JSON pointer.** Check
   `normalization.ignored_json_pointers` in both the corpus and the config file —
   they combine. `report.json` → `normalization` records the effective set.
3. **Body comparison is off.** `compare_body: false` in config, corpus defaults, or
   per case (`compare_body: false`) makes bodies skip comparison — status/headers
   still compare.
4. **The case is disabled.** `enabled: false` → `skipped`, exit-code neutral.
5. **Array-order sorting masked it.** `/results: sort` makes reordered arrays match.
6. **The request never reached the code path.** Add `--verbose` and inspect the
   request as actually sent (`report.json` → per-case request echo shows resolved
   headers/query/body — secrets redacted). Confirm the path/method/query match what
   the app routes on. Check `Host`-based routing: in-process apps see the internal
   `apiparity.local` host.
7. **`--include-bodies` shows nothing helpful in terminal.** Per-case JSON previews
   are in `report.json` only when `reporting.include_body_previews: true` (or
   `--include-bodies`); terminal previews are always redacted.

## Timing failures with `mode: fail`

```
[timing] 312.4ms exceeds max 250.0ms
```

The run exits `1`. For CI, prefer `warn` or `off` unless you have stable hardware —
in-process targets share the machine with the runner; local servers share the OS.
`max_ms` applies per case, per side, using wall-clock duration.

## In-process vs HTTP weirdness

- **Host header / URL generation differs** between targets. In-process requests use
  `http://apiparity.local` as a fixed host (not configurable) precisely so both sides
  see identical absolute URLs. Apps that branch on `Host` or `SERVER_NAME` may behave
  differently than under real hosting — a finding worth knowing about.
- **Non-ASCII path parameters**: the WSGI adapter re-encodes PATH_INFO per PEP 3333
  (ADR in [architecture.md](architecture.md)). If you see mojibake in paths, that's a
  bug — please report it.
- **Timeouts now fire for in-process targets, with a caveat.** Both WSGI and ASGI
  handlers are bounded by `timeout_seconds`: a hung endpoint produces a
  `timeout`-classified failure instead of stalling the run. For WSGI the handler
  *thread* cannot be interrupted — it is abandoned at the deadline and keeps
  running until your code returns (its response, if any, is discarded).

## Report files missing

- Check `--output DIR` (default `apiparity-output`); files are `report.json`,
  `junit.xml`.
- `--no-json` / `--no-junit` / `--no-terminal` (or the `reporting:` config keys)
  disable writers. A disabled writer is not an error.
- On exit `2` (no valid comparison), no reports are written — by design. The error
  is on stderr.

## Still stuck

1. `apiparity validate --corpus ... --config ...` — cheap, sends nothing.
2. Re-run with `--verbose --include-bodies` and read `report.json`.
3. Check the quality of the comparison itself: corpus coverage, isolated state,
   identical seeding (see [safety.md](safety.md)).
4. Search [GitHub Issues](https://github.com/shashi3070/apiparity/issues) — or
   open one with a minimal corpus reproducer (never real credentials).
