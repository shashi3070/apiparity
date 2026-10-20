# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
