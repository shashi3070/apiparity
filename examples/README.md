# apiparity examples

Runnable examples that use the fixture applications shipped in this repository
(`tests/fixtures/legacy_flask` and `tests/fixtures/migrated_fastapi`), so they
work from a fresh checkout with no services to start.

## Quick start

From the repository root (the fixture apps live under `tests/fixtures`, so put
that on `PYTHONPATH` when running the examples from a checkout):

```console
# PowerShell
$env:PYTHONPATH = "tests/fixtures"

# bash
export PYTHONPATH="tests/fixtures"

# Validate the corpus and safety posture without sending any request.
apiparity validate --corpus examples/requests.yaml --config examples/config.yaml --allow-mutating-requests

# The corpus contains POST cases, which require the mutating opt-in.
apiparity compare --corpus examples/requests.yaml --config examples/config.yaml \
    --allow-mutating-requests --output build/apiparity
```

Exit codes: `0` all matched, `1` behavioral differences found, `2` invalid
configuration, safety refusal, or target initialization failure.

## Files

| File | Purpose |
| --- | --- |
| `requests.yaml` | Request corpus: GET/POST cases, query defaults, repeated params, header-driven behavior, JSON body validation. |
| `config.yaml` | Project configuration: targets, comparison limits, normalization, redaction, safety, reporting. |

## Comparing two real services

Replace the in-process targets with URLs (loopback is enforced unless you pass
`--allow-remote-targets`):

```yaml
targets:
  old:
    kind: http
    url: http://127.0.0.1:8000
  new:
    kind: http
    url: http://127.0.0.1:8001
```

```console
apiparity compare --corpus examples/requests.yaml --config examples/config.yaml
```

## What the reports contain

`report.json` is the canonical machine-readable report (schema-versioned,
fully redacted). `junit.xml` feeds CI test-report viewers. The terminal output
is a human summary; use `--verbose` for per-case detail or `--quiet` for just
the final status line. Secrets in headers and configured JSON pointers are
masked as `[REDACTED]` in every report.
