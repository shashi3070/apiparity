# Contributing to apiparity

Thanks for helping improve `apiparity`.

## Development setup

```powershell
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"
```

## Quality gates (all must pass before a PR)

```powershell
ruff check .
ruff format --check .
mypy src
pytest -q --cov=apiparity --cov-report=term-missing
python -m build
```

Coverage target: at least 90% statements for `apiparity/comparison.py` and
`apiparity/safety.py`.

## Guidelines

- Behavioral comparison results must stay **deterministic**: never introduce LLM calls or
  other non-reproducible logic into the comparison engine.
- Keep the runtime dependency budget: only `httpx` and `PyYAML` may be runtime dependencies.
  Everything else belongs in optional extras or the `dev` extra.
- Every new feature needs tests that assert behavior (not just imports), plus updates to
  `README.md` and the relevant `docs/` page.
- Security-sensitive changes (redaction, safety guards, path handling) require tests in
  `tests/security/`.
- Never commit credentials, generated report files, or `dist/`.
- Report vulnerabilities per `SECURITY.md`, never in a public issue.

## Pull requests

1. Open an issue describing the behavioral change or bug.
2. Keep PRs focused; include test evidence (command output) in the description.
3. Update `CHANGELOG.md` under "Unreleased".
