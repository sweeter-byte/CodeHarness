def clamp(value, lower, upper):
    if lower > upper:
        raise ValueError("lower must not exceed upper")
    return min(lower, max(value, upper))
