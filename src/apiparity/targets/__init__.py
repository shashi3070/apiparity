"""Target adapters for HTTP, WSGI, and ASGI implementations."""

from __future__ import annotations

from apiparity.targets.asgi_target import AsgiTarget
from apiparity.targets.base import Target
from apiparity.targets.http_target import HttpTarget
from apiparity.targets.loader import import_app, resolve_target
from apiparity.targets.wsgi_target import WsgiTarget

__all__ = ["AsgiTarget", "HttpTarget", "Target", "WsgiTarget", "import_app", "resolve_target"]
