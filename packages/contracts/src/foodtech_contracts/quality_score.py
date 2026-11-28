"""Score calculation for the approved seven-item cooking quality scale."""
from decimal import Decimal, ROUND_HALF_UP


SCORE_ITEMS = (
    "ingredient_condition", "preparation", "taste", "texture", "appearance",
    "aroma", "temperature_suitability",
)


def summarize_quality_scores(scores):
    if type(scores) is not dict or set(scores) != set(SCORE_ITEMS):
        raise ValueError("the seven cooking quality scores are required")
    values = []
    for name in SCORE_ITEMS:
        value = scores[name]
        if type(value) not in (int, Decimal) or (type(value) is Decimal and not value.is_finite()) or (
            not 1 <= value <= 5 or value != int(value)
        ):
            raise ValueError("a score must be an integer from one to five")
        values.append((name, int(value)))
    total = sum(value for _, value in values)
    percent = (Decimal(total) / 35 * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return tuple(values), total, percent
