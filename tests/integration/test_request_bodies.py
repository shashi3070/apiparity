"""Explicit JSON body edge cases reach the wire intact.

httpx cannot distinguish ``json=None`` from "no body" (it silently omits the
payload), so apiparity sends an explicit JSON ``null`` as the literal bytes
``b"null"`` with an ``application/json`` content type. These tests prove each
body shape survives corpus parsing → request preparation → the transport.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apiparity.corpus import load_corpus
from apiparity.models import AppConfig, SafetyConfig, TargetConfig
from apiparity.runner import run_comparison

CORPUS_TEXT = """\
version: 1
cases:
  - {id: no-body, method: POST, path: /echo}
  - {id: json-null, method: POST, path: /echo, json: null}
  - {id: json-false, method: POST, path: /echo, json: false}
  - {id: json-empty-string, method: POST, path: /echo, json: ""}
  - {id: json-empty-object, method: POST, path: /echo, json: {}}
"""

EXPECTED_BODIES = {
    "no-body": b"",
    "json-null": b"null",
    "json-false": b"false",
    "json-empty-string": b'""',
    "json-empty-object": b"{}",
}


@pytest.mark.asyncio
async def test_bodies_arrive_byte_for_byte(tmp_path: Path) -> None:
    corpus_path = tmp_path / "bodies.yaml"
    corpus_path.write_text(CORPUS_TEXT, encoding="utf-8")
    corpus = load_corpus(corpus_path)

    by_id = {case.id: case for case in corpus.cases}
    assert by_id["json-null"].has_json_body is True
    assert by_id["json-null"].json_body is None
    assert by_id["no-body"].has_json_body is False

    config = AppConfig(
        old_target=TargetConfig(kind="asgi", app="echo_apps:echo_asgi"),
        new_target=TargetConfig(kind="asgi", app="echo_apps:echo_asgi"),
        safety=SafetyConfig(allow_mutating_requests=True),
    )
    result = await run_comparison(corpus=corpus, config=config)

    assert result.summary.exit_code == 0, [c.status for c in result.cases]
    for case in result.cases:
        assert case.baseline is not None
        assert case.baseline.body_bytes == EXPECTED_BODIES[case.case_id], case.case_id
        assert case.status == "match", case.case_id
