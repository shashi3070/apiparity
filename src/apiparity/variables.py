"""`${VAR}` environment-variable interpolation for corpus and config strings.

Resolution is recursive over dicts/lists/strings and happens **before** any
request is executed. Missing variables fail fast with the variable *name* only —
never a resolved secret value.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from apiparity.exceptions import VariableResolutionError

_VARIABLE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

__all__ = ["VARIABLE_RE", "contains_placeholder", "resolve_variables"]

VARIABLE_RE = _VARIABLE_RE


def contains_placeholder(value: Any) -> bool:
    """True if any string nested inside ``value`` contains a `${VAR}` placeholder."""
    if isinstance(value, str):
        return _VARIABLE_RE.search(value) is not None
    if isinstance(value, dict):
        return any(contains_placeholder(k) or contains_placeholder(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_placeholder(item) for item in value)
    return False


def resolve_variables(
    value: Any,
    env: Mapping[str, str] | None = None,
    *,
    location: str = "$",
) -> Any:
    """Recursively resolve ``${NAME}`` placeholders in strings.

    Args:
        value: Any nested structure of dicts, lists, and scalars.
        env: Environment mapping (defaults to :data:`os.environ`); injectable for tests.
        location: Dotted/pointer location used in error messages (never a value).

    Raises:
        VariableResolutionError: if a referenced variable is absent from ``env``.
    """
    environ = os.environ if env is None else env
    if isinstance(value, str):
        return _resolve_string(value, environ, location)
    if isinstance(value, dict):
        resolved_dict: dict[Any, Any] = {}
        for key, item in value.items():
            resolved_key = (
                _resolve_string(key, environ, f"{location}.<key>") if isinstance(key, str) else key
            )
            resolved_dict[resolved_key] = resolve_variables(
                item, environ, location=f"{location}.{resolved_key}"
            )
        return resolved_dict
    if isinstance(value, (list, tuple)):
        return [
            resolve_variables(item, environ, location=f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def _resolve_string(text: str, env: Mapping[str, str], location: str) -> str:
    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise VariableResolutionError(name, location=location)
        return env[name]

    return _VARIABLE_RE.sub(substitute, text)
