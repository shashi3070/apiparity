"""Target resolution: turn a :class:`TargetConfig` into a ready-to-start target.

Import rules:

* spec format ``package.module:attribute``;
* ``factory=True`` calls the imported object to obtain the application;
* import failures raise :class:`TargetInitError` naming the failing side, the
  sanitized error, and a corrective action — **never** a silent fallback to a
  different target type.

Note: importing an application module executes its import-time code (documented
in docs/safety.md); in-process targets may touch real databases or external
services if the application is configured that way.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from apiparity.exceptions import TargetInitError
from apiparity.models import TargetConfig, TargetKind
from apiparity.redaction import sanitize_exception_message
from apiparity.targets.asgi_target import AsgiTarget
from apiparity.targets.base import Target
from apiparity.targets.http_target import HttpTarget
from apiparity.targets.wsgi_target import WsgiTarget

__all__ = ["import_app", "resolve_target"]


def resolve_target(
    config: TargetConfig,
    *,
    side: str,
    size_limit: int,
    follow_redirects: bool,
    corpus_dir: Path,
    default_timeout: float = 10.0,
    allow_remote: bool = False,
) -> Target:
    """Construct (but do not start) the target described by ``config``.

    ``allow_remote`` mirrors ``safety.allow_remote_targets``: it is the policy
    the HTTP target applies to every request URL, including redirect hops.
    """
    kind: TargetKind = config.kind
    if kind == "http":
        if not config.url:
            raise TargetInitError(
                side,
                "http target requires a URL",
                action="Pass --{old|new}-url or set targets.<side>.url in the config file.",
            )
        return HttpTarget(
            side,
            config,
            size_limit=size_limit,
            follow_redirects=follow_redirects,
            corpus_dir=corpus_dir,
            default_timeout=default_timeout,
            allow_remote=allow_remote,
        )

    if kind in ("wsgi", "asgi"):
        app = import_app(config.app, side=side, factory=config.factory)
        target_class: type[WsgiTarget] | type[AsgiTarget] = (
            WsgiTarget if kind == "wsgi" else AsgiTarget
        )
        return target_class(
            side,
            config,
            app=app,
            size_limit=size_limit,
            follow_redirects=follow_redirects,
            corpus_dir=corpus_dir,
            default_timeout=default_timeout,
        )

    raise TargetInitError(  # pragma: no cover — config validation forbids this
        side,
        f"unknown target kind {kind!r}",
        action="Use one of: http, wsgi, asgi.",
    )


def import_app(spec: str | None, *, side: str, factory: bool) -> Any:
    """Import ``module:attribute`` and optionally call it as a factory."""
    if not spec or ":" not in spec:
        raise TargetInitError(
            side,
            f"application reference must look like 'package.module:attribute', got {spec!r}",
            action="Example: --old-app legacy_flask.app:app",
        )
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute or "." in attribute:
        raise TargetInitError(
            side,
            f"invalid application reference {spec!r}",
            action="Example: --old-app legacy_flask.app:create_app --old-factory",
        )

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        detail = sanitize_exception_message(f"{type(exc).__name__}: {exc}")
        raise TargetInitError(
            side,
            f"cannot import module '{module_name}': {detail}",
            action=(
                "Check the module name, install the application, and ensure its directory "
                "is on PYTHONPATH."
            ),
        ) from exc
    except Exception as exc:
        detail = sanitize_exception_message(f"{type(exc).__name__}: {exc}")
        raise TargetInitError(
            side,
            f"importing module '{module_name}' raised: {detail}",
            action="Fix the application's import-time code before comparing.",
        ) from exc

    if not hasattr(module, attribute):
        raise TargetInitError(
            side,
            f"module '{module_name}' has no attribute '{attribute}'",
            action=f"Check the attribute name in {spec!r}.",
        )
    target_object = getattr(module, attribute)

    if factory:
        if not callable(target_object):
            raise TargetInitError(
                side,
                f"'{spec}' is not callable, so it cannot be an application factory",
                action="Remove the --*-factory flag or point at a factory function.",
            )
        try:
            target_object = target_object()
        except Exception as exc:
            detail = sanitize_exception_message(f"{type(exc).__name__}: {exc}")
            raise TargetInitError(
                side,
                f"application factory '{spec}' raised: {detail}",
                action="Fix the factory, then rerun.",
            ) from exc

    if not callable(target_object):
        raise TargetInitError(
            side,
            f"'{spec}' resolved to {type(target_object).__name__}, "
            "which is not a callable application",
            action="Point at the WSGI/ASGI application object or its factory.",
        )
    return target_object
