import pytest

from solution import clamp


def test_clamp_preserves_values_inside_range():
    assert clamp(5, 0, 10) == 5


def test_clamp_limits_both_sides():
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10


def test_clamp_rejects_inverted_bounds():
    with pytest.raises(ValueError):
        clamp(1, 2, 0)
