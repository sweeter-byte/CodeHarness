from solution import unique_preserving_order


def test_unique_preserves_first_occurrence_order():
    assert unique_preserving_order([3, 1, 3, 2, 1]) == [3, 1, 2]


def test_unique_handles_empty_input():
    assert unique_preserving_order([]) == []
