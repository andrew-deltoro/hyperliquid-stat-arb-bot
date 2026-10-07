"""
Correlation guard: prevent simultaneous bets on correlated clusters.

Each pair trade is nominally market-neutral, but pairs in the same cluster
(L1s, DeFi, ETH ecosystem) tend to widen their spreads together during
market stress. When BTC drops 15%, all crypto correlations spike to ~1,
the hedges stop working, and every open pair trade loses simultaneously.

Caps apply to gross leg notional: every leg counts toward the cluster of
its own coin (a pair can touch two clusters) and toward a per-coin cap, so
coins shared across pairs (e.g. xyz:TSM in two pairs) are bounded too.
Caps live in config under risk.cluster_caps and risk.max_coin_gross_usd.
"""
from __future__ import annotations

from dataclasses import dataclass


DEFAULT_CLUSTERS: dict[str, list[str]] = {
    "btc_eth": ["BTC", "ETH"],
    "alt_l1":  ["SOL", "AVAX", "SUI", "APT", "SEI", "TIA", "INJ", "NEO", "ETC", "LTC"],
    "defi":    ["AAVE", "COMP", "UNI", "SUSHI", "MKR", "CRV", "BAL", "SNX", "DYDX"],
    "oracle":  ["LINK", "PYTH"],
    "meme":    ["DOGE", "SHIB", "PEPE", "WIF", "MEME", "AIXBT", "GMT", "ORDI"],
    "xyz_semis": ["XYZ:NVDA", "XYZ:PLTR", "XYZ:MU", "XYZ:TSM", "XYZ:AMD"],
    "xyz_metals": ["XYZ:GOLD", "XYZ:SILVER"],
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
        max_coin_gross: float | None = None,
    ):
        self.clusters = DEFAULT_CLUSTERS
        self.cluster_caps = cluster_caps or {}
        self.default_cap = default_cap
        self.max_coin_gross = max_coin_gross

    def cluster_of(self, coin: str) -> str | None:
        c = coin.upper()
        for name, members in self.clusters.items():
            if c in members:
                return name
        return None

    @staticmethod
    def _open_legs(open_positions: list):
        for pos in open_positions:
            if pos.get("status") not in ("open", "needs_review"):
                continue
            for leg in ("a", "b"):
                coin = pos.get(f"coin_{leg}")
                usd = pos.get(f"leg_{leg}_usd")
                if coin and usd is not None:
                    yield coin.upper(), abs(float(usd))

    def check(
        self,
        coin_a: str,
        coin_b: str,
        new_notional: float,
        open_positions: list,
        beta: float = 1.0,
    ) -> GuardResult:
        """Would opening this position breach any cluster or coin cap?"""
        new_legs = {coin_a.upper(): new_notional / 2.0,
                    coin_b.upper(): abs(beta) * new_notional / 2.0}
        coin_gross: dict[str, float] = {}
        cluster_gross: dict[str, float] = {}
        for coin, usd in self._open_legs(open_positions):
            coin_gross[coin] = coin_gross.get(coin, 0.0) + usd
            cl = self.cluster_of(coin)
            if cl:
                cluster_gross[cl] = cluster_gross.get(cl, 0.0) + usd

        if self.max_coin_gross is not None:
            for coin, usd in new_legs.items():
                projected = coin_gross.get(coin, 0.0) + usd
                if projected > self.max_coin_gross:
                    return GuardResult(
                        False,
                        reason=(f"coin {coin} gross ${projected:.2f} > "
                                f"${self.max_coin_gross:.2f}"),
                    )

        touched: dict[str, float] = {}
        for coin, usd in new_legs.items():
            cl = self.cluster_of(coin)
            if cl:
                touched[cl] = touched.get(cl, 0.0) + usd
        if not touched:
            return GuardResult(True, reason="unclustered pair")

        notes = []
        for cl, add in touched.items():
            cap = float(self.cluster_caps.get(cl, self.default_cap))
            current = cluster_gross.get(cl, 0.0)
            if current + add > cap:
                return GuardResult(
                    False,
                    reason=(f"cluster '{cl}' cap ${cap:.2f} breached "
                            f"(current ${current:.2f} + new ${add:.2f} "
                            f"= ${current + add:.2f})"),
                    cluster=cl,
                )
            notes.append(f"{cl} ${current:.2f}+${add:.2f}≤${cap:.2f}")
        return GuardResult(True, reason="clusters ok: " + ", ".join(notes),
                           cluster=",".join(touched))
