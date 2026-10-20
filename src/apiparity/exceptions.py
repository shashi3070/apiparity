"""Exception hierarchy for apiparity.

All user-facing messages raised from these exceptions must be safe to print:
callers pass them through :mod:`apiparity.redaction` before display.
"""

from __future__ import annotations


class ApiParityError(Exception):
    """Base class for all apiparity errors."""


class ConfigError(ApiParityError):
    """Invalid or unknown configuration file content or CLI combination."""


class CorpusError(ApiParityError):
    """Invalid request corpus (schema, types, duplicate ids, conflicting bodies...)."""

    def __init__(self, message: str, *, location: str | None = None) -> None:
        self.location = location
        super().__init__(f"{location}: {message}" if location else message)


class VariableResolutionError(ApiParityError):
    """A `${VAR}` placeholder could not be resolved before execution."""

    def __init__(self, variable: str, *, location: str | None = None) -> None:
        self.variable = variable
        prefix = f"{location}: " if location else ""
        super().__init__(
            f"{prefix}environment variable '{variable}' is not set "
            f"(set it or remove the '${{{variable}}}' placeholder)"
        )


class SafetyError(ApiParityError):
    """A safety guard refused the run (mutating requests or remote targets)."""


class TargetInitError(ApiParityError):
    """A baseline or candidate target failed to initialize."""

    def __init__(self, side: str, message: str, *, action: str | None = None) -> None:
        self.side = side
        self.action = action
        text = f"{side} target failed to initialize: {message}"
        if action:
            text = f"{text}\nCorrective action: {action}"
        super().__init__(text)


class ExecutionError(ApiParityError):
    """An unexpected error prevented a valid comparison run."""
