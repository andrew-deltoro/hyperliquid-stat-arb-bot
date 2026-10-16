"""Classic technical indicators, computed from price/volume arrays.

Computation only. This module has no network access, no state, and makes no
decision. It exists so a study, backtest, or row in
``docs/DAY_TRADE_EDGE_MATRIX.md`` can compute an indicator the same way every
time.

Reading a number off a chart image is not what this does, and is not needed:
an indicator is a formula over OHLCV, so it is computed from the exact bars
(``data.ohlcv.fetch_candles``), not recovered from pixels.

Important
---------
Computing an indicator is not an edge. A threshold such as "RSI < 30" is a
hypothesis, not a signal, until it clears its locked scatter, walk-forward,
and the 4.5 bp-per-side taker cost. Do not wire these into a live path
without a pre-registered row.

Conventions
-----------
- Inputs are 1-D and finite; length must match across price/high/low/volume.
- Outputs are ``float64`` arrays the same length as the input, with ``nan``
  in the warm-up region where the indicator is not yet defined.
- RSI and ATR use Wilder smoothing. Bollinger uses the population standard
  deviation (``ddof=0``), as is conventional.
"""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np

__all__ = [
    "sma",
    "ema",
    "rolling_sum",
    "rolling_std",
    "rsi",
    "vwap",
    "macd",
    "bollinger",
    "stochastic",
    "true_range",
    "atr",
    "indicator_snapshot",
]


def _arr(values: Sequence[float], name: str = "values") -> np.ndarray:
    a = np.asarray(values, dtype=float)
    if a.ndim != 1:
        raise ValueError(f"{name} must be 1-D, got shape {a.shape}")
    if not np.all(np.isfinite(a)):
        raise ValueError(f"{name} contains non-finite values")
    return a


def _same_length(**arrays: np.ndarray) -> None:
    lengths = {k: len(v) for k, v in arrays.items()}
    if len(set(lengths.values())) > 1:
        raise ValueError(f"length mismatch: {lengths}")


def rolling_sum(values: Sequence[float], window: int) -> np.ndarray:
    """Trailing sum over ``window`` bars; ``nan`` until the window is full."""
    x = _arr(values)
    if window < 1:
        raise ValueError("window must be >= 1")
    out = np.full(x.shape, np.nan, dtype=float)
    if len(x) < window:
        return out
    csum = np.cumsum(np.insert(x, 0, 0.0))
    out[window - 1:] = csum[window:] - csum[:-window]
    return out


def rolling_std(values: Sequence[float], window: int, ddof: int = 0) -> np.ndarray:
    """Trailing standard deviation over ``window`` bars.

    ``ddof=0`` (population) is the Bollinger convention; use ``ddof=1`` for
    the sample standard deviation.
    """
    x = _arr(values)
    if window < 1:
        raise ValueError("window must be >= 1")
    out = np.full(x.shape, np.nan, dtype=float)
    if len(x) < window:
        return out
    s = rolling_sum(x, window)
    s2 = rolling_sum(x * x, window)
    mean = s / window
    var = (s2 - s * mean) / (window - ddof)
    var = np.clip(var, 0.0, None)
    out[window - 1:] = np.sqrt(var[window - 1:])
    return out


def sma(values: Sequence[float], window: int) -> np.ndarray:
    """Simple moving average."""
    s = rolling_sum(values, window)
    return s / window


def ema(values: Sequence[float], span: int) -> np.ndarray:
    """Exponential moving average, seeded at the first observation.

    Equivalent to ``pandas`` ``ewm(span=span, adjust=False)``.
    """
    x = _arr(values)
    if span < 1:
        raise ValueError("span must be >= 1")
    out = np.empty_like(x)
    if len(x) == 0:
        return out
    alpha = 2.0 / (span + 1.0)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def rsi(close: Sequence[float], window: int = 14) -> np.ndarray:
    """Relative Strength Index with Wilder smoothing.

    ``nan`` for the first ``window`` bars. A flat series reports 50; a
    series with no losses reports 100 and one with no gains reports 0.
    """
    x = _arr(close, "close")
    if window < 2:
        raise ValueError("window must be >= 2")
    n = len(x)
    out = np.full(n, np.nan, dtype=float)
    if n <= window:
        return out

    delta = np.diff(x)
    gain = np.clip(delta, 0.0, None)
    loss = np.clip(-delta, 0.0, None)

    def _val(g: float, l: float) -> float:
        if l == 0.0:
            return 100.0 if g > 0.0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    avg_gain = float(gain[:window].mean())
    avg_loss = float(loss[:window].mean())
    out[window] = _val(avg_gain, avg_loss)

    for i in range(window + 1, n):
        avg_gain = (avg_gain * (window - 1) + gain[i - 1]) / window
        avg_loss = (avg_loss * (window - 1) + loss[i - 1]) / window
        out[i] = _val(avg_gain, avg_loss)
    return out


def vwap(close: Sequence[float], volume: Sequence[float],
         window: int) -> np.ndarray:
    """Rolling volume-weighted average price over ``window`` bars.

    This is a rolling VWAP, which suits 24/7 markets such as crypto. A
    session VWAP reset at a chosen clock is a different series; compute it
    by feeding one session's bars at a time.
    """
    c = _arr(close, "close")
    v = _arr(volume, "volume")
    _same_length(close=c, volume=v)
    if window < 1:
        raise ValueError("window must be >= 1")
    num = rolling_sum(c * v, window)
    den = rolling_sum(v, window)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    out[den == 0] = np.nan
    return out


def macd(close: Sequence[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """MACD line, signal line, and histogram (line minus signal).

    EMAs are seeded at the first observation, so all three arrays are
    defined from index 0.
    """
    if not 0 < fast < slow:
        raise ValueError("require 0 < fast < slow")
    x = _arr(close, "close")
    line = ema(x, fast) - ema(x, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def bollinger(close: Sequence[float], window: int = 20,
              k: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bollinger middle (SMA), upper, and lower bands."""
    if k <= 0:
        raise ValueError("k must be > 0")
    x = _arr(close, "close")
    mid = sma(x, window)
    sd = rolling_std(x, window, ddof=0)
    return mid, mid + k * sd, mid - k * sd


def stochastic(high: Sequence[float], low: Sequence[float],
               close: Sequence[float], k_period: int = 14,
               k_smooth: int = 3, d_smooth: int = 3,
               ) -> tuple[np.ndarray, np.ndarray]:
    """Stochastic oscillator: the ``%K`` and ``%D`` lines.

    ``%K`` is the close's position within the ``k_period``-bar high/low
    range, expressed in percent, then smoothed by a ``k_smooth``-bar SMA.
    ``%D`` is a ``d_smooth``-bar SMA of the smoothed ``%K``. The classic
    slow-stochastic settings are 14, 3, 3.

    A flat range (``highest == lowest``) reports 50 rather than dividing by
    zero, matching the RSI convention for a flat series. ``nan`` fills the
    warm-up region: raw ``%K`` starts at ``k_period - 1``, smoothed ``%K``
    at ``k_period + k_smooth - 2``, and ``%D`` after that.
    """
    h = _arr(high, "high")
    l = _arr(low, "low")
    c = _arr(close, "close")
    _same_length(high=h, low=l, close=c)
    if k_period < 1 or k_smooth < 1 or d_smooth < 1:
        raise ValueError("k_period, k_smooth and d_smooth must be >= 1")
    n = len(c)
    raw = np.full(n, np.nan, dtype=float)
    if n >= k_period:
        hh = np.array([h[i - k_period + 1:i + 1].max()
                       for i in range(k_period - 1, n)])
        ll = np.array([l[i - k_period + 1:i + 1].min()
                       for i in range(k_period - 1, n)])
        rng = hh - ll
        with np.errstate(invalid="ignore", divide="ignore"):
            k = 100.0 * (c[k_period - 1:] - ll) / rng
        k = np.where(rng == 0.0, 50.0, k)
        raw[k_period - 1:] = k

    def _trailing_mean(x: np.ndarray, window: int) -> np.ndarray:
        """SMA that tolerates the ``nan`` warm-up region (unlike ``sma``)."""
        out = np.full(x.shape, np.nan, dtype=float)
        start = next((i for i, v in enumerate(x) if np.isfinite(v)), None)
        if start is None or len(x) - start < window:
            return out
        cs = np.cumsum(np.insert(x[start:], 0, 0.0))
        out[start + window - 1:] = (cs[window:] - cs[:-window]) / window
        return out

    k_line = _trailing_mean(raw, k_smooth)
    d_line = _trailing_mean(k_line, d_smooth)
    return k_line, d_line


def true_range(high: Sequence[float], low: Sequence[float],
               close: Sequence[float]) -> np.ndarray:
    """True range; the first bar uses ``high - low`` (no prior close)."""
    h = _arr(high, "high")
    l = _arr(low, "low")
    c = _arr(close, "close")
    _same_length(high=h, low=l, close=c)
    n = len(c)
    if n == 0:
        return np.empty(0, dtype=float)
    tr = np.empty(n, dtype=float)
    tr[0] = h[0] - l[0]
    if n > 1:
        tr[1:] = np.maximum.reduce([
            h[1:] - l[1:],
            np.abs(h[1:] - c[:-1]),
            np.abs(l[1:] - c[:-1]),
        ])
    return tr


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float],
        window: int = 14) -> np.ndarray:
    """Average True Range with Wilder smoothing."""
    if window < 1:
        raise ValueError("window must be >= 1")
    tr = true_range(high, low, close)
    n = len(tr)
    out = np.full(n, np.nan, dtype=float)
    if n < window:
        return out
    out[window - 1] = tr[:window].mean()
    for i in range(window, n):
        out[i] = (out[i - 1] * (window - 1) + tr[i]) / window
    return out


def indicator_snapshot(high: Sequence[float], low: Sequence[float],
                       close: Sequence[float], volume: Sequence[float],
                       *, rsi_window: int = 14, vwap_window: int = 24,
                       bb_window: int = 20, bb_k: float = 2.0,
                       atr_window: int = 14, ma_window: int = 200,
                       macd_params: tuple[int, int, int] = (12, 26, 9),
                       ) -> dict[str, float]:
    """Latest value of each indicator, for logging or a study's feature row.

    Returns a flat ``dict`` of the most recent finite value. Keys whose
    indicator is not yet defined (too few bars) are ``nan``. This is a
    convenience for research output only; it makes no trading decision.
    """
    h = _arr(high, "high")
    l = _arr(low, "low")
    c = _arr(close, "close")
    v = _arr(volume, "volume")
    _same_length(high=h, low=l, close=c, volume=v)

    fast, slow, sig = macd_params
    line, sig_line, hist = macd(c, fast, slow, sig)
    bb_mid, bb_up, bb_lo = bollinger(c, bb_window, bb_k)
    ma = sma(c, ma_window)
    r = rsi(c, rsi_window)
    a = atr(h, l, c, atr_window)
    vw = vwap(c, v, vwap_window)

    def last(arr: np.ndarray) -> float:
        return float(arr[-1]) if len(arr) else float("nan")

    close_last = last(c)
    upper = last(bb_up)
    lower = last(bb_lo)
    return {
        "close": close_last,
        "rsi": last(r),
        "vwap": last(vw),
        "macd": last(line),
        "macd_signal": last(sig_line),
        "macd_hist": last(hist),
        "bb_mid": last(bb_mid),
        "bb_upper": upper,
        "bb_lower": lower,
        "bb_pct_b": (
            (close_last - lower) / (upper - lower)
            if np.isfinite(upper) and np.isfinite(lower) and upper != lower
            else float("nan")
        ),
        "ma": last(ma),
        "atr": last(a),
        "atr_pct": last(a) / close_last * 100.0 if close_last else float("nan"),
    }
