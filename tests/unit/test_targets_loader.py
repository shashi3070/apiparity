"""Unit tests for application loading and target resolution (targets.loader)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apiparity.exceptions import TargetInitError
from apiparity.models import TargetConfig
from apiparity.targets.asgi_target import AsgiTarget
from apiparity.targets.loader import import_app, resolve_target
from apiparity.targets.wsgi_target import WsgiTarget

CORPUS_DIR = Path()


class TestImportApp:
    def test_missing_module(self) -> None:
        with pytest.raises(TargetInitError, match=r"definitely\.missing\.module"):
            import_app("definitely.missing.module:app", side="baseline", factory=False)

    def test_invalid_spec_no_colon(self) -> None:
        with pytest.raises(TargetInitError, match=r"package\.module"):
            import_app("no_colon_here", side="candidate", factory=False)

    def test_missing_attribute(self) -> None:
        with pytest.raises(TargetInitError, match="does_not_exist"):
            import_app("legacy_flask.app:does_not_exist", side="baseline", factory=False)

    def test_plain_app_object(self) -> None:
        app = import_app("legacy_flask.app:app", side="baseline", factory=False)
        assert hasattr(app, "wsgi_app")

    def test_factory_is_called(self) -> None:
        app = import_app("legacy_flask.app:create_app", side="baseline", factory=True)
        assert hasattr(app, "wsgi_app")

    def test_asgi_app_object(self) -> None:
        app = import_app("migrated_fastapi.app:app", side="candidate", factory=False)
        assert hasattr(app, "router")


class TestResolveTarget:
    def _resolve(self, config: TargetConfig) -> object:
        return resolve_target(
            config,
            side="baseline",
            size_limit=1024,
            follow_redirects=False,
            corpus_dir=CORPUS_DIR,
        )

    def test_http_requires_url(self) -> None:
        with pytest.raises(TargetInitError, match="requires a URL"):
            self._resolve(TargetConfig(kind="http"))

    def test_wsgi_resolves(self) -> None:
        target = self._resolve(TargetConfig(kind="wsgi", app="legacy_flask.app:app"))
        assert isinstance(target, WsgiTarget)

    def test_asgi_resolves(self) -> None:
        target = self._resolve(TargetConfig(kind="asgi", app="migrated_fastapi.app:app"))
        assert isinstance(target, AsgiTarget)

    def test_wsgi_factory_resolves(self) -> None:
        target = self._resolve(
            TargetConfig(kind="wsgi", app="legacy_flask.app:create_app", factory=True)
        )
        assert isinstance(target, WsgiTarget)

    def test_default_timeout_plumbed(self) -> None:
        target = resolve_target(
            TargetConfig(kind="wsgi", app="legacy_flask.app:app"),
            side="baseline",
            size_limit=1024,
            follow_redirects=False,
            corpus_dir=CORPUS_DIR,
            default_timeout=3.5,
        )
        assert target.default_timeout == 3.5  # type: ignore[attr-defined]

    def test_effective_timeout_precedence(self) -> None:
        target = resolve_target(
            TargetConfig(kind="wsgi", app="legacy_flask.app:app", timeout_seconds=2.0),
            side="baseline",
            size_limit=1024,
            follow_redirects=False,
            corpus_dir=CORPUS_DIR,
            default_timeout=3.5,
        )
        assert target.effective_timeout == 2.0  # type: ignore[attr-defined]
