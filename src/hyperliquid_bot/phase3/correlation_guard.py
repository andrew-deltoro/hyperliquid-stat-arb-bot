"""
Correlation guard: prevent simultaneous bets on correlated clusters.

Each pair trade is nominally market-neutral, but pairs in the same cluster
(L1s, DeFi, ETH ecosystem) tend to widen their spreads together during
market stress. When BTC drops 15%, all crypto correlations spike to ~1,
the hedges stop working, and every open pair trade loses simultaneously.

This guard caps the total notional deployed in each cluster, so we never
end up with all our capital in one correlated bet.

Clusters are defined in code (DEFAULT_CLUSTERS) with caps in config
under risk.cluster_caps. A pair belongs to a cluster if BOTH legs are
in that cluster's member list.
"""
from __future__ import annotations

from dataclasses import dataclass


DEFAULT_CLUSTERS: dict[str, list[str]] = {
    "btc_eth": ["BTC", "ETH"],
    "alt_l1":  ["SOL", "AVAX", "SUI", "APT", "SEI", "TIA", "INJ"],
    "defi":    ["AAVE", "COMP", "UNI", "SUSHI", "MKR", "CRV", "BAL"],
    "oracle":  ["LINK", "PYTH"],
    "meme":    ["DOGE", "SHIB", "PEPE", "WIF"],
}


@dataclass
class GuardResult:
    allowed: bool
    reason: str = ""
    cluster: str = ""


class CorrelationGuard:
    def __init__(
        self,
        cluster_caps: dict | None = None,
        default_cap: float = 200.0,
    ):
        self.clusters = DEFAULT_CLUSTERS
        self.cluster_caps = cluster_caps or {}
        self.default_cap = default_cap

    def _find_cluster(self, coin_a: str, coin_b: str) -> str | None:
        """Which cluster does this pair belong to? None if unclustered."""
        a, b = coin_a.upper(), coin_b.upper()
        for name, members in self.clusters.items():
            if a in members and b in members:
                return name
        return None

    def _current_cluster_exposure(
        self, open_positions: list, cluster_name: str
    ) -> float:
        """Sum notional of open positions whose BOTH legs are in this cluster."""
        members = self.clusters.get(cluster_name, [])
        total = 0.0
        for pos in open_positions:
            if pos.get("status") != "open":
                continue
            ca = pos.get("coin_a", "").upper()
            cb = pos.get("coin_b", "").upper()
            if ca in members and cb in members:
                total += float(pos.get("notional_usd", 0.0))
        return total

    def check(
        self,
        coin_a: str,
        coin_b: str,
        new_notional: float,
        open_positions: list,
    ) -> GuardResult:
        """Would opening this position breach any cluster cap?"""
        cluster = self._find_cluster(coin_a, coin_b)
        if cluster is None:
            return GuardResult(True, reason="unclustered pair")

        cap = self.cluster_caps.get(cluster, self.default_cap)
        current = self._current_cluster_exposure(open_positions, cluster)
        projected = current + new_notional

        if projected > cap:
            return GuardResult(
                False,
                reason=(
                    f"cluster '{cluster}' cap ${cap:.2f} breached "
                    f"(current ${current:.2f} + new ${new_notional:.2f} "
                    f"= ${projected:.2f})"
                ),
                cluster=cluster,
            )

        return GuardResult(
            True,
            reason=(
                f"cluster '{cluster}' ok "
                f"(${current:.2f} + ${new_notional:.2f} ≤ ${cap:.2f})"
            ),
            cluster=cluster,
        )
