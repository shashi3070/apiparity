"""Unit tests for secret redaction (apiparity.redaction)."""

from __future__ import annotations

from apiparity.models import RedactionConfig
from apiparity.redaction import (
    REDACTED,
    escape_xml,
    is_sensitive_header,
    is_sensitive_pointer,
    is_sensitive_query_key,
    redact_headers,
    redact_json,
    redact_url,
    sanitize_exception_message,
)


class TestSensitivityPredicates:
    def test_exact_configured_header(self) -> None:
        config = RedactionConfig()
        assert is_sensitive_header("authorization", config)
        assert is_sensitive_header("Authorization", config)
        assert is_sensitive_header("X-Api-Key", config)

    def test_substring_match(self) -> None:
        config = RedactionConfig()
        assert is_sensitive_header("x-auth-token", config)
        assert is_sensitive_header("db-password", config)
        assert is_sensitive_header("my-secret-header", config)

    def test_non_sensitive(self) -> None:
        config = RedactionConfig()
        assert not is_sensitive_header("content-type", config)
        assert not is_sensitive_header("x-request-id", config)

    def test_empty_substring_tokens_never_match(self) -> None:
        config = RedactionConfig(header_name_substrings=("",))
        assert not is_sensitive_header("anything", config)

    def test_pointer_match_is_exact(self) -> None:
        config = RedactionConfig(sensitive_json_pointers=("/credentials/token",))
        assert is_sensitive_pointer("/credentials/token", config)
        assert not is_sensitive_pointer("/credentials", config)
        assert not is_sensitive_pointer("/credentials/token/deep", config)

    def test_query_key_sensitivity(self) -> None:
        assert is_sensitive_query_key("api_key")
        assert is_sensitive_query_key("AccessToken")
        assert is_sensitive_query_key("password")
        assert not is_sensitive_query_key("q")
        assert not is_sensitive_query_key("limit")


class TestRedactHeaders:
    def test_mapping_input(self) -> None:
        config = RedactionConfig()
        result = redact_headers(
            {"Authorization": "Bearer hunter2", "content-type": "application/json"}, config
        )
        assert result["Authorization"] == REDACTED
        assert result["content-type"] == "application/json"

    def test_iterable_input(self) -> None:
        config = RedactionConfig()
        result = redact_headers([("x-api-key", "k")], config)
        assert result == {"x-api-key": REDACTED}

    def test_multi_value_list(self) -> None:
        config = RedactionConfig()
        result = redact_headers([("set-cookie", ["a=1", "b=2"])], config)
        assert result == {"set-cookie": [REDACTED, REDACTED]}

    def test_tuple_multi_value(self) -> None:
        config = RedactionConfig()
        result = redact_headers([("cookie", ("a=1", "b=2"))], config)
        assert result == {"cookie": [REDACTED, REDACTED]}


class TestRedactJson:
    def test_no_sensitive_nodes_passthrough(self) -> None:
        config = RedactionConfig()
        value = {"a": 1, "b": [1, 2]}
        assert redact_json(value, config) == value

    def test_exact_pointer_subtree(self) -> None:
        config = RedactionConfig(sensitive_json_pointers=("/credentials/token",))
        value = {"credentials": {"token": "hunter2", "kind": "bearer"}}
        result = redact_json(value, config)
        assert result == {"credentials": {"token": REDACTED, "kind": "bearer"}}

    def test_list_indexing(self) -> None:
        config = RedactionConfig(sensitive_json_pointers=("/items/1/token",))
        value = {"items": [{"token": "a"}, {"token": "b"}]}
        result = redact_json(value, config)
        assert result["items"][0] == {"token": "a"}
        assert result["items"][1]["token"] == REDACTED

    def test_escaped_key_tokens(self) -> None:
        config = RedactionConfig(sensitive_json_pointers=("/a~1b",))
        assert redact_json({"a/b": "secret"}, config) == {"a/b": REDACTED}

    def test_input_not_mutated(self) -> None:
        config = RedactionConfig(sensitive_json_pointers=("/x",))
        value = {"x": "secret", "y": 1}
        redact_json(value, config)
        assert value == {"x": "secret", "y": 1}


class TestRedactUrl:
    def test_userinfo_dropped(self) -> None:
        assert redact_url("https://user:pass@example.com/api") == "https://example.com/api"

    def test_sensitive_query_masked(self) -> None:
        result = redact_url("https://example.com/search?q=hello&api_key=abc123")
        assert "api_key=" + REDACTED in result
        assert "q=hello" in result

    def test_password_query_masked(self) -> None:
        result = redact_url("https://example.com/?password=hunter2")
        assert "hunter2" not in result

    def test_plain_url_unchanged_structure(self) -> None:
        assert redact_url("https://example.com/path?x=1") == "https://example.com/path?x=1"

    def test_fragment_preserved(self) -> None:
        assert redact_url("https://example.com/a#frag").endswith("#frag")


class TestSanitizeExceptionMessage:
    def test_userinfo_stripped(self) -> None:
        message = sanitize_exception_message("connect failed for https://user:hunter2@example.com")
        assert "hunter2" not in message
        assert REDACTED in message

    def test_query_credentials_masked(self) -> None:
        message = sanitize_exception_message("GET https://x.test/?token=abc&q=1 failed")
        assert "abc" not in message
        assert "q=1" in message

    def test_newlines_flattened(self) -> None:
        assert "\n" not in sanitize_exception_message("line1\nline2")

    def test_long_messages_truncated(self) -> None:
        result = sanitize_exception_message("x" * 2000, limit=100)
        assert len(result) <= 100


class TestEscapeXml:
    def test_entities(self) -> None:
        assert escape_xml('<a href="x">&\'</a>') == (
            "&lt;a href=&quot;x&quot;&gt;&amp;&apos;&lt;/a&gt;"
        )

    def test_control_chars_stripped(self) -> None:
        assert escape_xml("a\x00b\x07c") == "abc"
