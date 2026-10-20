# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-10-20

### Fixed

- Redirect precedence: an explicit `follow_redirects: false` in the config file is no
  longer overridden by a truthy corpus default (identity-based resolution instead of
  truthiness).
- In-process request timeouts are now enforced: an ASGI handler that never completes
  is cancelled at the deadline, and a WSGI handler that never returns no longer hangs
  the run — the run stops waiting at the deadline, the case fails with
  `error_kind: timeout`, and the (uninterruptible) handler thread is abandoned.
- ASGI startup errors are no longer concealed: a lifespan task that dies with an
  exception during startup raises `TargetInitError` instead of being treated as an
  app that "does not support lifespan".

### Added

- ASGI lifespan policy `targets.<side>.lifespan` (`auto` | `required` | `off`,
  default `auto`) with matching `--old-lifespan` / `--new-lifespan` CLI flags:
  `required` aborts when the app never sends a lifespan startup message, `off`
  skips the lifespan protocol entirely.
- Bounded waits around the ASGI lifespan task — a lingering lifespan task can no
  longer block startup failure reporting or shutdown cleanup.

### Changed

- Distribution renamed to `apiparity-check` on PyPI (the bare name `apiparity` is
  taken by an unrelated project); the import package and CLI command are unchanged.
- Dev tooling pinned (`ruff>=0.15,<0.18`, `mypy>=1.10,<2.2`) so CI formatting stays
  reproducible across ruff releases.

## [0.1.0] - 2026-10-20

Distribution name on PyPI is **`apiparity-check`** (the normalized name `apiparity`
collides with an unrelated existing project); the import package and CLI command are
both `apiparity`.

### Added

- Initial release of `apiparity`, a behavioral parity checker for API implementations.
- Request corpora in YAML and JSON with strict validation and `${VAR}` interpolation.
- Targets: HTTP endpoints, in-process WSGI applications, in-process ASGI applications
  (with explicit lifespan handling), including application factories.
- Comparison engine: status codes, headers (case-insensitive, multi-value aware),
  structural JSON diff with RFC 6901 JSON Pointer paths, text and binary bodies,
  configurable normalization, timing diagnostics, response-size limits.
- Secret redaction for terminal, JSON, and JUnit outputs.
- Safety controls: mutating-request opt-in, remote-target opt-in, pre-run safety summary.
- Reports: canonical JSON (schema v1), JUnit XML, terminal summary.
- CLI: `apiparity validate`, `apiparity compare`, documented exit codes 0/1/2.
- Python API: async `compare()` plus synchronous `compare_sync()`.
