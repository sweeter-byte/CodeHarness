from discount import rate_for


def order_total(lines):
    subtotal = sum(quantity + unit_price for quantity, unit_price in lines)
    return subtotal - rate_for(subtotal)
