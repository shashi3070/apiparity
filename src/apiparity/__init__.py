"""apiparity — behavioral parity checker for API implementations.

Public API (async primary, synchronous convenience wrapper provided)::

    from apiparity import AppConfig, compare, compare_sync

``compare`` is the documented async entry point; ``compare_sync`` runs the same
flow on a private event loop for callers that are not async.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "ApiParityError",
    "AppConfig",
    "CaseResult",
    "ComparisonConfig",
    "ConfigError",
    "CorpusError",
    "Difference",
    "RunResult",
    "RunSummary",
    "SafetyError",
    "TargetInitError",
    "VariableResolutionError",
    "__version__",
    "compare",
    "compare_sync",
    "load_corpus",
]


def __getattr__(name: str) -> object:
    """Lazy re-exports so ``import apiparity`` stays light for CLI startup."""
    if name in {
        "AppConfig",
        "ComparisonConfig",
        "RunResult",
        "RunSummary",
        "CaseResult",
        "Difference",
    }:
        from apiparity import models

        return getattr(models, name)
    if name in {
        "ApiParityError",
        "ConfigError",
        "CorpusError",
        "SafetyError",
        "TargetInitError",
        "VariableResolutionError",
    }:
        from apiparity import exceptions

        return getattr(exceptions, name)
    if name in {"compare", "compare_sync"}:
        from apiparity import runner

        return getattr(runner, name)
    if name == "load_corpus":
        from apiparity.corpus import load_corpus

        return load_corpus
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
