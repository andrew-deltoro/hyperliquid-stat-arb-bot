"""The counterfactual weight must be frozen, bounded, reproducible, and safe."""
import math

from src.hyperliquid_bot.phase3.conviction import (
    CF_LAM, CF_RHO, CF_Z_REF, CF_Z_SCALE, counterfactual_weight,
)


def test_neutral_at_entry_threshold():
    assert counterfactual_weight(CF_Z_REF) == 1.0
    assert counterfactual_weight(-CF_Z_REF) == 1.0  # sign-agnostic (|z|)


def test_bounded():
    for z in (0.0, 1.0, 2.0, 5.0, 50.0, 1e9):
        w = counterfactual_weight(z)
        assert 1.0 - CF_RHO <= w <= 1.0 + CF_RHO + 1e-12


def test_monotone_in_abs_z():
    ws = [counterfactual_weight(z) for z in (2.0, 3.0, 3.5, 4.0, 5.0)]
    assert ws == sorted(ws)


def test_reproducible_and_stateless():
    a = counterfactual_weight(3.7)
    b = counterfactual_weight(3.7)
    assert a == b  # no hidden in-memory history


def test_bad_input_is_neutral():
    assert counterfactual_weight(float("nan")) == 1.0
    assert counterfactual_weight(None) == 1.0
    assert counterfactual_weight("nonsense") == 1.0


def test_cap_reached_by_formula():
    z_cap = CF_Z_REF + CF_RHO * CF_Z_SCALE / CF_LAM
    assert math.isclose(counterfactual_weight(z_cap), 1.0 + CF_RHO, abs_tol=1e-9)
    assert math.isclose(counterfactual_weight(z_cap + 10), 1.0 + CF_RHO, abs_tol=1e-9)
