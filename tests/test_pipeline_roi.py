"""`_estimate_manual_review_roi` must not invent a savings figure when this
run's cost is unknown (unpriced model).
"""

import pytest

from intellisource_ai.pipeline import _estimate_manual_review_roi


def test_savings_subtracts_known_run_cost() -> None:
    hours, manual_cost, savings = _estimate_manual_review_roi(2000, 1.0, 200, 75.0)

    assert hours == 10.0
    assert manual_cost == 750.0
    assert savings == pytest.approx(749.0)


def test_savings_is_none_when_run_cost_is_unknown() -> None:
    hours, manual_cost, savings = _estimate_manual_review_roi(2000, None, 200, 75.0)

    assert hours == 10.0
    assert manual_cost == 750.0
    assert savings is None


def test_zero_loc_per_hour_with_unknown_cost_still_has_no_savings_figure() -> None:
    assert _estimate_manual_review_roi(2000, None, 0, 75.0) == (0.0, 0.0, None)
