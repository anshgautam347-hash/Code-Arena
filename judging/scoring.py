"""Server-side scoring. Scores are always computed here from stored test results,
never accepted from the browser."""
from decimal import ROUND_DOWN, Decimal

CENT = Decimal("0.01")


def score_for(marks, passed_weight, total_weight):
    """Marks awarded in proportion to the weight of passed test cases (rounded down to 2 places)."""
    if not total_weight or passed_weight <= 0:
        return Decimal("0.00")
    passed_weight = min(passed_weight, total_weight)
    return (Decimal(marks) * Decimal(passed_weight) / Decimal(total_weight)).quantize(CENT, rounding=ROUND_DOWN)
