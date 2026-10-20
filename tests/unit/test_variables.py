from __future__ import annotations

import pytest

from apiparity.exceptions import VariableResolutionError
from apiparity.variables import contains_placeholder, resolve_variables


class TestResolveVariables:
    def test_nested_structures(self) -> None:
        payload = {
            "a": {"b": ["x-${N}", 42, None, True]},
            "plain": "no vars",
            "list": [{"k": "${N}"}],
        }
        out = resolve_variables(payload, {"N": "7"})
        assert out["a"]["b"][0] == "x-7"
        assert out["a"]["b"][1] == 42
        assert out["a"]["b"][2] is None
        assert out["a"]["b"][3] is True
        assert out["list"][0]["k"] == "7"
        assert out["plain"] == "no vars"

    def test_multiple_placeholders_in_one_string(self) -> None:
        out = resolve_variables("${A}-${B}-${A}", {"A": "1", "B": "2"})
        assert out == "1-2-1"

    def test_placeholder_adjacent_to_text(self) -> None:
        out = resolve_variables("Bearer ${TOKEN}", {"TOKEN": "abc"})
        assert out == "Bearer abc"

    def test_dict_keys_resolved(self) -> None:
        out = resolve_variables({"${KEY}": "v"}, {"KEY": "X-Test"})
        assert out == {"X-Test": "v"}

    def test_missing_variable_names_variable_only(self) -> None:
        with pytest.raises(VariableResolutionError) as excinfo:
            resolve_variables("${API_SECRET}", {}, location="case 'x'.headers.Authorization")
        message = str(excinfo.value)
        assert "API_SECRET" in message
        assert "case 'x'.headers.Authorization" in message

    def test_default_env_used_when_env_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APITEST_VAR", "from-os")
        assert resolve_variables("${APITEST_VAR}") == "from-os"

    def test_non_string_scalars_untouched(self) -> None:
        assert resolve_variables(3.5, {}) == 3.5

    def test_missing_variable_in_nested_location(self) -> None:
        with pytest.raises(VariableResolutionError) as excinfo:
            resolve_variables({"headers": {"X-Key": "${NOPE}"}}, {}, location="case 'a'")
        assert "case 'a'.headers.X-Key" in str(excinfo.value)


class TestContainsPlaceholder:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("plain", False),
            ("${X}", True),
            ({"k": "${X}"}, True),
            (["${X}"], True),
            ({"deep": [{"deeper": "q${X}"}]}, True),
            ({"k": "v"}, False),
            (5, False),
        ],
    )
    def test_detection(self, value: object, expected: bool) -> None:
        assert contains_placeholder(value) is expected
