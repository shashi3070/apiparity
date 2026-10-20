"""Fixture applications whose request handlers never complete (timeout tests).

Imported by module:attr spec through the normal loader (``hang_apps:...``);
``tests/conftest.py`` puts this directory on ``sys.path``.
"""

from __future__ import annotations

import threading
from typing import Any

#: Released by the test after it has asserted the timeout, so the abandoned
#: handler thread can finish before the suite ends.
RELEASE = threading.Event()

#: Paths the recording app has actually served (append-only, GIL-atomic).
SERVED: list[str] = []


def hang_wsgi_app(environ: Any, start_response: Any) -> Any:
    """WSGI app that blocks until RELEASE is set (safety valve: 15s)."""
    RELEASE.wait(timeout=15.0)
    start_response("200 OK", [("content-type", "application/json")])
    return [b'{"ok": true}']


def recording_wsgi_app(environ: Any, start_response: Any) -> Any:
    """Record every served path; ``/hang`` blocks until RELEASE (valve: 15s).

    Lets tests prove that a quarantined target is never touched again: paths
    served after quarantine must not appear in ``SERVED``.
    """
    path = environ.get("PATH_INFO", "")
    SERVED.append(path)
    if path == "/hang":
        RELEASE.wait(timeout=15.0)
    start_response("200 OK", [("content-type", "application/json")])
    return [b'{"ok": true}']
