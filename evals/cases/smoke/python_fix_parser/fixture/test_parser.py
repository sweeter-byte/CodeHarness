import pytest

from parser import parse_fields


def test_parse_fields_trims_keys_and_values():
    assert parse_fields("name = Ada, role= engineer") == {
        "name": "Ada",
        "role": "engineer",
    }


def test_parse_fields_allows_equals_inside_value():
    assert parse_fields("query=a=b") == {"query": "a=b"}


def test_parse_fields_rejects_missing_separator():
    with pytest.raises(ValueError):
        parse_fields("broken")
