"""
Multivariate cointegration via the Johansen procedure.

For k log-price series, Johansen estimates the number of cointegrating
relationships (the rank) and the cointegrating vectors themselves. A pair
is the special case k=2, rank=1.

The result is a set of linear combinations of the input series that are
jointly stationary. Each combination is a "basket spread" that can be
traded with all k legs simultaneously.
"""
from __future__ import annotations
from dataclasses import dataclass

import numpy as np
from statsmodels.tsa.vector_ar.vecm import coint_johansen
from statsmodels.tsa.stattools import adfuller


@dataclass
class BasketVector:
    weights: np.ndarray          # length k, normalized so max |w|=1
    spread: np.ndarray           # length T, the cointegrating residual
    adf_pval: float              # stationarity test on the spread
    half_life_hours: float       # OU half-life, inf if not reverting
    z_current: float             # current z-score of the spread
    spread_sd: float
    eigenvalue: float


@dataclass
class BasketFit:
    symbols: list[str]           # length k, matching the input matrix columns
    rank: int                    # number of cointegrating relationships
    vectors: list[BasketVector]  # the significant vectors, length == rank
    trace_stats: np.ndarray
    trace_crit_95: np.ndarray
    eigenvalues: np.ndarray
    n_obs: int


def _normalize_vector(v: np.ndarray) -> np.ndarray:
    """Scale so the largest-magnitude coefficient is 1.0.
    Also flip sign so the first nonzero coefficient is positive."""
    idx = int(np.argmax(np.abs(v)))
    scale = v[idx]
    if abs(scale) < 1e-12:
        return v
    w = v / scale
    for x in w:
        if abs(x) > 1e-9:
            if x < 0:
                w = -w
            break
    return w


def _ou_half_life(spread: np.ndarray) -> float:
    """Half-life in bars from AR(1) on the spread."""
    y = spread - spread.mean()
    lag = y[:-1]
    diff = np.diff(y)
    if lag.std() < 1e-12:
        return float("inf")
    theta = float(np.polyfit(lag, diff, 1)[0])
    if theta >= 0:
        return float("inf")
    return float(-np.log(2) / theta)


def fit_basket_johansen(
    log_prices: np.ndarray,
    symbols: list[str],
    det_order: int = 0,
    k_ar_diff: int = 1,
    min_rank_confidence: float = 0.95,
) -> BasketFit | None:
    """
    Run Johansen on a (T, k) matrix of log prices.

    det_order: -1 = no deterministic, 0 = constant (standard), 1 = linear trend
    k_ar_diff: number of lagged difference terms. 1 is standard.
    min_rank_confidence: 0.90, 0.95, or 0.99 (column in critical values table)
    """
    T, k = log_prices.shape
    if T < 100 or k < 2:
        return None

    try:
        result = coint_johansen(log_prices, det_order, k_ar_diff)
    except Exception:
        return None

    # Column 1 = 95%, column 0 = 90%, column 2 = 99%
    crit_col = {0.90: 0, 0.95: 1, 0.99: 2}[min_rank_confidence]

    # Sequential rank estimation: stop at first non-rejection
    rank = 0
    for i in range(k):
        if result.lr1[i] > result.cvt[i, crit_col]:
            rank += 1
        else:
            break

    vectors: list[BasketVector] = []
    for i in range(min(rank, k)):
        raw_v = result.evec[:, i]
        w = _normalize_vector(raw_v)
        spread = log_prices @ w

        try:
            adf_p = float(adfuller(spread, maxlag=24, autolag=None,
                                   result_object=False)[1])
        except Exception:
            adf_p = 1.0

        hl = _ou_half_life(spread)
        sd = float(spread.std())
        z_now = float((spread[-1] - spread.mean()) / (sd + 1e-9))

        vectors.append(BasketVector(
            weights=w,
            spread=spread,
            adf_pval=adf_p,
            half_life_hours=hl,
            z_current=z_now,
            spread_sd=sd,
            eigenvalue=float(result.eig[i]),
        ))

    return BasketFit(
        symbols=list(symbols),
        rank=rank,
        vectors=vectors,
        trace_stats=np.asarray(result.lr1),
        trace_crit_95=np.asarray(result.cvt[:, 1]),
        eigenvalues=np.asarray(result.eig),
        n_obs=T,
    )


if __name__ == "__main__":
    # Sanity check on a synthetic 3-leg basket.
    # y1 = 0.7*y2 + 0.5*y3 + noise  -> rank should be 2 (or at least 1)
    rng = np.random.default_rng(42)
    T = 2000
    y2 = np.cumsum(rng.normal(0, 0.01, T))
    y3 = np.cumsum(rng.normal(0, 0.01, T))
    noise = rng.normal(0, 0.005, T)
    y1 = 0.7 * y2 + 0.5 * y3 + noise
    M = np.column_stack([y1, y2, y3])

    fit = fit_basket_johansen(M, ["A", "B", "C"])
    print(f"rank: {fit.rank}")
    for i, v in enumerate(fit.vectors):
        print(f"  vec {i}: weights={v.weights.round(3).tolist()} "
              f"ADF p={v.adf_pval:.4f} HL={v.half_life_hours:.1f} z={v.z_current:+.2f}")
