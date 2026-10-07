"""In-memory stand-in for the Hyperliquid SDK Exchange + Info objects."""
from __future__ import annotations


class FakeHL:
    def __init__(self, prices: dict[str, float], sz_decimals: dict[str, int] | None = None):
        self.prices = dict(prices)
        self.positions: dict[str, float] = {}
        coins = list(prices)
        self.coin_to_asset = {c: i for i, c in enumerate(coins)}
        decs = sz_decimals or {}
        self.asset_to_sz_decimals = {i: decs.get(c, 3) for i, c in enumerate(coins)}

        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self._next_oid = 1

        # Failure knobs
        self.ioc_fill_frac: dict[str, float] = {}
        # Fill fraction for reduce-only IOC orders (defaults to ioc_fill_frac).
        self.reduce_fill_frac: dict[str, float] = {}
        self.maker_fill_frac: float = 0.0
        self.user_state_error = False
        self.user_fills_error = False

    # ---------------- helpers ----------------
    def _oid(self) -> int:
        oid = self._next_oid
        self._next_oid += 1
        return oid

    def _apply(self, coin: str, signed_sz: float, px: float, oid: int) -> None:
        self.positions[coin] = round(self.positions.get(coin, 0.0) + signed_sz, 10)
        if abs(self.positions[coin]) < 1e-12:
            self.positions.pop(coin)
        self.fills.append({"coin": coin, "oid": oid, "sz": str(abs(signed_sz)), "px": str(px)})

    def _reduce_cap(self, coin: str, is_buy: bool) -> float:
        cur = self.positions.get(coin, 0.0)
        if (is_buy and cur < 0) or (not is_buy and cur > 0):
            return abs(cur)
        return 0.0

    # ---------------- Exchange ----------------
    def _slippage_price(self, coin, is_buy, slippage, px):
        return px * (1 + slippage) if is_buy else px * (1 - slippage)

    def order(self, coin, is_buy, sz, limit_px, order_type, reduce_only=False):
        tif = order_type["limit"]["tif"]
        oid = self._oid()
        self.orders.append({"coin": coin, "is_buy": is_buy, "sz": sz, "tif": tif,
                            "reduce_only": reduce_only, "oid": oid})
        sign = 1.0 if is_buy else -1.0
        cap = self._reduce_cap(coin, is_buy) if reduce_only else float("inf")
        if reduce_only and cap == 0:
            return {"status": "ok", "response": {"data": {"statuses": [
                {"error": "Reduce only order would increase position."}]}}}

        if tif == "Alo":
            filled = min(sz * self.maker_fill_frac, cap)
            if filled > 0:
                self._apply(coin, sign * filled, limit_px, oid)
            return {"status": "ok", "response": {"data": {"statuses": [
                {"resting": {"oid": oid}}]}}}

        frac = self.ioc_fill_frac.get(coin, 1.0)
        if reduce_only:
            frac = self.reduce_fill_frac.get(coin, frac)
        filled = min(sz * frac, cap)
        if filled <= 0:
            return {"status": "ok", "response": {"data": {"statuses": [
                {"error": "Order could not immediately match."}]}}}
        px = self.prices[coin]
        self._apply(coin, sign * filled, px, oid)
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": str(filled), "avgPx": str(px), "oid": oid}}]}}}

    def cancel(self, coin, oid):
        return {"status": "ok"}

    # ---------------- Info ----------------
    def user_state(self, address, dex=""):
        if self.user_state_error:
            raise ConnectionError("user_state unavailable")
        out = []
        for coin, szi in self.positions.items():
            coin_dex = coin.split(":", 1)[0] if ":" in coin else ""
            if coin_dex != dex:
                continue
            out.append({"position": {
                "coin": coin, "szi": str(szi),
                "positionValue": str(abs(szi) * self.prices[coin]),
            }})
        return {"assetPositions": out, "marginSummary": {"accountValue": "1000"}}

    def user_fills(self, address):
        if self.user_fills_error:
            raise ConnectionError("user_fills unavailable")
        return list(self.fills)

    def query_order_by_oid(self, address, oid):
        return {"order": {"status": "open"}}

    def l2_snapshot(self, coin):
        px = self.prices[coin]
        return {"levels": [[{"px": str(px * 0.999)}], [{"px": str(px * 1.001)}]]}

    def ioc_orders(self, coin=None):
        return [o for o in self.orders if o["tif"] == "Ioc" and (coin is None or o["coin"] == coin)]
