from discount import rate_for
from pricing import order_total


def test_discount_starts_at_one_hundred():
    assert rate_for(100) == 0.10


def test_order_total_multiplies_lines_and_applies_rate():
    assert order_total([(2, 25), (1, 50)]) == 90
