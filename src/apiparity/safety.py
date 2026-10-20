"""Fail-closed safety guards.

Two independent opt-ins are required before a run may proceed:

* ``allow_mutating_requests`` — the corpus contains methods outside
  ``{GET, HEAD, OPTIONS}``;
* ``allow_remote_targets`` — an HTTP target points at a non-loopback host.

Never assume a private IP is a test environment; never default to replaying
traffic anywhere but loopback.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from apiparity.exceptions import SafetyError
from apiparity.models import (
    KNOWN_METHODS,
    SAFE_METHODS,
    RequestCase,
    SafetyConfig,
    TargetConfig,
)

__all__ = [
    "MUTATING_METHODS",
    "SafetyReport",
    "evaluate_safety",
    "is_loopback_url",
    "mutating_methods_in",
]

MUTATING_METHODS: frozenset[str] = frozenset(KNOWN_METHODS - SAFE_METHODS)


@dataclass(frozen=True)
class SafetyReport:
    """What the run is about to do, and which grants made it allowed."""

    mutating_case_ids: tuple[str, ...]
    mutating_methods: tuple[str, ...]
    remote_targets: tuple[str, ...]
    loopback_targets: tuple[str, ...]
    granted_mutating: bool
    granted_remote: bool
    in_process_targets: tuple[str, ...]

    def summary_lines(self) -> list[str]:
        lines = ["Safety summary:"]
        lines.append(
            f"  mutating cases: {len(self.mutating_case_ids)}"
            + (f" ({', '.join(sorted(self.mutating_methods))})" if self.mutating_case_ids else "")
            + (
                ""
                if not self.mutating_case_ids
                else (
                    " — ALLOWED by --allow-mutating-requests/config"
                    if self.granted_mutating
                    else " — allowed"
                )
            )
        )
        for target in self.remote_targets:
            lines.append(
                f"  remote HTTP target: {target} — ALLOWED by --allow-remote-targets/config"
            )
        for target in self.loopback_targets:
            lines.append(f"  loopback HTTP target: {target}")
        for target in self.in_process_targets:
            lines.append(f"  in-process target: {target} (no network)")
        lines.append(
            "  note: apiparity cannot roll back side effects; baseline and candidate must use "
            "isolated, identically seeded state."
        )
        return lines

    def render(self) -> str:
        return "\n".join(self.summary_lines())


def mutating_methods_in(cases: Sequence[RequestCase]) -> set[str]:
    return {case.method for case in cases if case.enabled and case.method not in SAFE_METHODS}


def is_loopback_url(url: str) -> bool:
    """True only for literal loopback hosts. Unknown/unparseable → False (remote)."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback


def evaluate_safety(
    cases: Sequence[RequestCase],
    target_configs: Sequence[tuple[str, TargetConfig]],
    safety: SafetyConfig,
) -> SafetyReport:
    """Evaluate both guards. Raises :class:`SafetyError` when a guard is not satisfied.

    ``target_configs`` pairs a human side label (``baseline``/``candidate``) with its config.
    """
    mutating = sorted(mutating_methods_in(cases))
    mutating_ids = tuple(
        case.id for case in cases if case.enabled and case.method in MUTATING_METHODS
    )

    remote: list[str] = []
    loopback: list[str] = []
    in_process: list[str] = []
    for side, config in target_configs:
        if config.kind == "http":
            if config.url and is_loopback_url(config.url):
                loopback.append(f"{side}: {config.url}")
            else:
                remote.append(f"{side}: {config.url or '<missing url>'}")
        else:
            in_process.append(f"{side}: {config.kind}:{config.app}")

    problems: list[str] = []
    if mutating and not safety.allow_mutating_requests:
        problems.append(
            f"corpus contains mutating methods ({', '.join(mutating)}) but "
            f"allow_mutating_requests is disabled; these requests cannot be rolled back. "
            f"Pass --allow-mutating-requests (or set safety.allow_mutating_requests) to proceed."
        )
    if remote and not safety.allow_remote_targets:
        host_list = ", ".join(remote)
        problems.append(
            f"HTTP target(s) point outside loopback ({host_list}) but allow_remote_targets is "
            f"disabled; never assume a host is a safe test environment. Pass "
            f"--allow-remote-targets (or set safety.allow_remote_targets) to proceed."
        )

    if problems:
        raise SafetyError("safety refusal:\n- " + "\n- ".join(problems))

    return SafetyReport(
        mutating_case_ids=mutating_ids,
        mutating_methods=tuple(mutating),
        remote_targets=tuple(remote),
        loopback_targets=tuple(loopback),
        granted_mutating=bool(mutating) and safety.allow_mutating_requests,
        granted_remote=bool(remote) and safety.allow_remote_targets,
        in_process_targets=tuple(in_process),
    )
