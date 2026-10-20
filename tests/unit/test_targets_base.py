"""Request preparation: explicit JSON-null vs absent body, content-type rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from apiparity.models import RequestCase, TargetConfig
from apiparity.targets.wsgi_target import WsgiTarget


def _target() -> WsgiTarget:
    return WsgiTarget(
        "baseline",
        TargetConfig(kind="wsgi", app="test:app"),
        app=lambda environ, start_response: [],  # type: ignore[no-untyped-call]
        size_limit=1024,
        follow_redirects=False,
        corpus_dir=Path(__file__).parent,
    )


class TestJsonBodyPreparation:
    def test_absent_body_sends_nothing(self) -> None:
        case = RequestCase(id="c", method="POST", path="/x")
        assert case.has_json_body is False
        _method, _url, kwargs = _target()._prepare_request(case)
        assert "json" not in kwargs
        assert "content" not in kwargs

    def test_explicit_null_sent_as_json_null_bytes(self) -> None:
        case = RequestCase(id="c", method="POST", path="/x", json_body=None, has_json_body=True)
        _method, _url, kwargs = _target()._prepare_request(case)
        assert kwargs["content"] == b"null"
        assert kwargs["headers"].get("content-type") == "application/json"
        assert "json" not in kwargs

    def test_explicit_null_respects_case_content_type(self) -> None:
        case = RequestCase(
            id="c",
            method="POST",
            path="/x",
            headers={"Content-Type": "application/vnd.api+json"},
            json_body=None,
            has_json_body=True,
        )
        _method, _url, kwargs = _target()._prepare_request(case)
        assert kwargs["headers"]["Content-Type"] == "application/vnd.api+json"
        assert "content-type" not in kwargs["headers"]

    @pytest.mark.parametrize(
        ("json_body", "expected"),
        [
            (False, False),
            ("", ""),
            ({}, {}),
            (0, 0),
            ([], []),
        ],
    )
    def test_falsy_json_bodies_still_sent(self, json_body: object, expected: object) -> None:
        case = RequestCase(
            id="c", method="POST", path="/x", json_body=json_body, has_json_body=True
        )
        _method, _url, kwargs = _target()._prepare_request(case)
        assert kwargs["json"] == expected
        assert "content" not in kwargs

    def test_data_body_still_used_when_no_json(self) -> None:
        case = RequestCase(id="c", method="POST", path="/x", data="raw")
        _method, _url, kwargs = _target()._prepare_request(case)
        assert kwargs["content"] == b"raw"
