"""Echo fixture applications: respond with the exact request body received.

Imported by module:attr spec through the normal loader (``echo_apps:...``);
``tests/conftest.py`` puts this directory on ``sys.path``.
"""

from __future__ import annotations

from typing import Any


async def echo_asgi(scope: Any, receive: Any, send: Any) -> None:
    """ASGI app that echoes the raw request body back byte for byte."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.request":
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})
