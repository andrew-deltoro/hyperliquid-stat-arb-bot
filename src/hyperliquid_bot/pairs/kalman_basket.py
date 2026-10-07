"""
k-leg generalization of KalmanBeta.

State: [beta_2, beta_3, ..., beta_k, alpha]   (dimension k)
Observation: y_1 = beta_2*y_2 + ... + beta_k*y_k + alpha + noise

y_1 is the "target" leg (the one we price against the rest).
Which leg is y_1 is an arbitrary normalization; the Johansen vector
tells us which coefficient to isolate. In practice we pick the leg with
weight closest to 1.0 after normalization.

Same predict/update equations as KalmanBeta, just higher dimension.
"""
from __future__ import annotations
from dataclasses import dataclass

import numpy as np


@dataclass
class KalmanBasketState:
    betas: np.ndarray            # length k-1
    alpha: float
    spread: float                # y_1 - H @ state
    beta_var: np.ndarray         # diagonal of P for the slope terms
    alpha_var: float


class KalmanBasket:
    def __init__(
        self,
        k: int,
        beta_init: np.ndarray,
        alpha_init: float,
        q: float = 1e-5,
        r: float = 1e-4,
        p_init: float = 1.0,
    ):
        assert len(beta_init) == k - 1, \
            f"beta_init must have length k-1={k-1}, got {len(beta_init)}"
        self.k = k
        self.x = np.concatenate([np.asarray(beta_init, dtype=float),
                                 [float(alpha_init)]])
        self.P = np.eye(k) * p_init
        self.Q = np.eye(k) * q
        self.R = float(r)

    def update(self, prices: np.ndarray) -> KalmanBasketState:
        """
        prices: array of length k, ordered [y_1, y_2, ..., y_k].
        y_1 is the target; the rest are predictors.
        """
        assert len(prices) == self.k
        y_target = float(prices[0])
        y_preds = np.asarray(prices[1:], dtype=float)

        # Predict: state random walk
        x_pred = self.x
        P_pred = self.P + self.Q

        # Observation model
        H = np.concatenate([y_preds, [1.0]])   # length k
        y_hat = float(H @ x_pred)
        S = float(H @ P_pred @ H) + self.R
        innovation = y_target - y_hat

        # Kalman gain (k-vector)
        K = (P_pred @ H) / S

        # Update
        self.x = x_pred + K * innovation
        self.P = P_pred - np.outer(K, H) @ P_pred

        return KalmanBasketState(
            betas=self.x[:-1].copy(),
            alpha=float(self.x[-1]),
            spread=float(innovation),
            beta_var=np.diag(self.P)[:-1].copy(),
            alpha_var=float(self.P[-1, -1]),
        )

    def smooth(self, price_matrix: np.ndarray):
        """
        Run the filter through a (T, k) matrix.
        Returns: betas_history (T, k-1), alphas_history (T,)
        """
        T = price_matrix.shape[0]
        betas_hist = np.empty((T, self.k - 1))
        alphas_hist = np.empty(T)
        for i in range(T):
            s = self.update(price_matrix[i])
            betas_hist[i] = s.betas
            alphas_hist[i] = s.alpha
        return betas_hist, alphas_hist

    def spread_series(self, price_matrix: np.ndarray) -> np.ndarray:
        """Compute the time-varying spread series (recursive, one pass)."""
        T = price_matrix.shape[0]
        spreads = np.empty(T)
        for i in range(T):
            p = price_matrix[i]
            H = np.concatenate([p[1:], [1.0]])
            spreads[i] = float(p[0] - H @ self.x)
            self.update(p)
        return spreads


if __name__ == "__main__":
    # Sanity check: y1 = 0.7*y2 + 0.5*y3 + small noise
    rng = np.random.default_rng(7)
    T = 500
    y2 = np.cumsum(rng.normal(0, 0.01, T))
    y3 = np.cumsum(rng.normal(0, 0.01, T))
    y1 = 0.7 * y2 + 0.5 * y3 + rng.normal(0, 0.002, T)
    M = np.column_stack([y1, y2, y3])

    kf = KalmanBasket(k=3, beta_init=np.array([0.5, 0.5]),
                       alpha_init=0.0, q=1e-6, r=1e-5)
    betas, alphas = kf.smooth(M)
    print(f"final betas: {betas[-1].round(4).tolist()} (target [0.7, 0.5])")
    print(f"final alpha: {alphas[-1]:+.4f}")
