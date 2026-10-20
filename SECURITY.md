# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | ✔ (current release line) |

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report privately via one of:

- **GitHub Security Advisory** (preferred): Repository → Security → Report a
  vulnerability → "Start draft advisory".
- **Email:** shashikundan0001@gmail.com — subject prefix `[apiparity SECURITY]`.

Include, where possible:

- apiparity version, Python version, OS.
- A minimal reproduction: config + corpus (with secrets replaced by obvious fakes)
  and the command line.
- Impact assessment: what an attacker could gain (e.g., secret leakage in reports,
  unintended request replay, path escape).

### What to expect

| Stage | Target time |
|---|---|
| Acknowledgement of report | ≤ 3 business days |
| Initial triage / severity assessment | ≤ 7 business days |
| Fix or mitigation plan for confirmed issues | ≤ 30 days for high/critical; next release for others |
| Coordinated disclosure | After a fix is released, or by mutual agreement |

These are honest targets for a small project, not SLAs. Critical issues in the
safety guards (mutating/remote opt-ins, redaction, path confinement) are prioritized
above everything else.

## Scope

**In scope**

- Secret redaction bypasses: any path by which a credential value reaches
  `report.json`, `junit.xml`, terminal output, logs, or exception messages.
- Safety-guard bypasses: replaying mutating requests or contacting non-loopback
  hosts without the explicit opt-ins.
- Path traversal via multipart `files[].path` escaping the corpus directory.
- Code execution via corpus/config content (there must be none — no `eval`, no
  shell, no dynamic imports of corpus-specified code paths beyond the explicitly
  configured `module:app` targets).
- Denial of service via crafted corpus/config (unbounded memory, hangs) — e.g.
  response-size-limit enforcement gaps.
- Dependency vulnerabilities with a demonstrated impact on the above.

**Out of scope**

- Vulnerabilities in the user's own applications under test (apiparity *runs* your
  app; bugs in the app are the app's to fix).
- Issues requiring non-default unsafe opt-ins without further escalation (e.g.,
  pointing `--allow-remote-targets` + `--allow-mutating-requests` at production is
  misuse, not a vulnerability — though UX improvements are welcome).
- Availability of third-party services/hosts referenced in user corpora.
- Reports against unreleased code on `main` without a reproduction.
- Social engineering, physical attacks.

## Security model (summary)

apiparity's threat model is that **corpus/config files and target responses are
untrusted input**, and that **the machine running a comparison may be shared**:

- Corpus/config content never executes (data-only parsing; no `eval`/shell).
- App imports happen only from user-supplied, explicitly configured `module:app`
  specs — same trust as running the app under pytest.
- All validation (schema, variables, safety, target init) completes before any
  outbound request.
- Secrets are redacted at difference-construction time and again at every output
  boundary ([docs/safety.md](docs/safety.md#redaction)).
- HTTP targets ignore environment proxies (`trust_env=False`).
- TLS verification defaults on; no redirects; no retries; timeouts on every request.

Full behavior: [docs/safety.md](docs/safety.md).
