"""
Kalman filter for time-varying cointegration beta.

State vector: [beta, alpha]
Observation: y_a = beta * y_b + alpha + noise(R)
Process:     state_t = state_{t-1} + noise(Q)

The filter updates beta and alpha continuously as new price bars arrive,
instead of refitting static values every cycle.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np


@dataclass
class KalmanState:
    beta: float
    alpha: float
    beta_var: float
    alpha_var: float
    beta_alpha_cov: float


class KalmanBeta:
    """
    A 2-state Kalman filter for (beta, alpha) in y_a = beta * y_b + alpha.

    Usage:
        kf = KalmanBeta(beta_init=1.0, alpha_init=0.0, q=1e-5, r=1e-4)
        for y_a, y_b in zip(log_a, log_b):
            state = kf.update(y_a, y_b)
    """

    def __init__(
        self,
        beta_init: float,
        alpha_init: float,
        q: float = 1e-5,
        r: float = 1e-4,
        p_init: float = 1.0,
    ):
        self.x = np.array([beta_init, alpha_init], dtype=float)
        self.P = np.eye(2) * p_init
        self.Q = np.eye(2) * q
        self.R = float(r)

    def update(self, y_a: float, y_b: float) -> KalmanState:
        # 1. Predict (state random walk, F = identity)
        x_pred = self.x
        P_pred = self.P + self.Q

        # 2. Innovation
        H = np.array([y_b, 1.0])
        y_pred = H @ x_pred
        S = float(H @ P_pred @ H) + self.R
        innovation = y_a - y_pred

        # 3. Kalman gain
        K = (P_pred @ H) / S  # 2-vector

        # 4. Update
        self.x = x_pred + K * innovation
        self.P = P_pred - np.outer(K, H) @ P_pred

        return KalmanState(
            beta=float(self.x[0]),
            alpha=float(self.x[1]),
            beta_var=float(self.P[0, 0]),
            alpha_var=float(self.P[1, 1]),
            beta_alpha_cov=float(self.P[0, 1]),
        )

    def smooth(self, log_a: np.ndarray, log_b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Run the filter over a full series; return arrays of beta_t, alpha_t."""
        n = len(log_a)
        betas = np.empty(n)
        alphas = np.empty(n)
        for i in range(n):
            s = self.update(float(log_a[i]), float(log_b[i]))
            betas[i] = s.beta
            alphas[i] = s.alpha
        return betas, alphas

    def spread(self, log_a: np.ndarray, log_b: np.ndarray) -> np.ndarray:
        """Compute the time-varying spread series."""
        betas, alphas = self.smooth(log_a, log_b)
        return log_a - betas * log_b - alphas
