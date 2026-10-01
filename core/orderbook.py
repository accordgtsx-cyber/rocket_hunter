import numpy as np

class MarketDataEngine:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.spot_bids = {}
        self.spot_asks = {}
        self.perp_bids = {}
        self.perp_asks = {}
        self.recent_trades = []

    def update_orderbook(self, market_type: str, bids_delta: list, asks_delta: list, is_snapshot: bool = False):
        bids = self.spot_bids if market_type == "spot" else self.perp_bids
        asks = self.spot_asks if market_type == "spot" else self.perp_asks
        if is_snapshot:
            # Снапшот — это полная картина стакана. Старые уровни надо выбросить, иначе после
            # переподключения в книге остаются "призрачные" уровни, которых на бирже уже нет.
            bids.clear()
            asks.clear()
        for price, vol in bids_delta:
            p, v = float(price), float(vol)
            if v == 0: bids.pop(p, None)
            else: bids[p] = v
        for price, vol in asks_delta:
            p, v = float(price), float(vol)
            if v == 0: asks.pop(p, None)
            else: asks[p] = v

    def add_trade(self, side: str, price: float, volume: float, ts_ms: int):
        self.recent_trades.append({"side": side, "price": price, "volume": volume, "ts": ts_ms})

    def clear_old_trades(self, current_ts_ms: int, window_sec: int = 10):
        cutoff = current_ts_ms - (window_sec * 1000)
        self.recent_trades = [t for t in self.recent_trades if t["ts"] >= cutoff]

    def calculate_metrics(self) -> dict:
        metrics = {
            "basis_pct": 0.0,
            "perp_imbalance": {"zone_strike": 0.0, "zone_mm": 0.0, "zone_horizon": 0.0},
            "trade_speed_ps": 0.0,
            "net_delta_usdt": 0.0,
            "is_wash_trading": False,
            "price_change_10s_pct": 0.0
        }
        if self.spot_bids and self.spot_asks and self.perp_bids and self.perp_asks:
            mid_spot = (max(self.spot_bids.keys()) + min(self.spot_asks.keys())) / 2
            mid_perp = (max(self.perp_bids.keys()) + min(self.perp_asks.keys())) / 2
            metrics["basis_pct"] = ((mid_spot - mid_perp) / mid_perp) * 100
        if self.perp_bids and self.perp_asks:
            best_bid, best_ask = max(self.perp_bids.keys()), min(self.perp_asks.keys())
            mid_perp = (best_bid + best_ask) / 2
            zones = {"zone_strike": (0.0, 0.005), "zone_mm": (0.005, 0.02), "zone_horizon": (0.02, 0.05)}
            for zone_name, (low, high) in zones.items():
                b_vols = sum(v for p, v in self.perp_bids.items() if mid_perp * (1 - high) <= p <= mid_perp * (1 - low))
                a_vols = sum(v for p, v in self.perp_asks.items() if mid_perp * (1 + low) <= p <= mid_perp * (1 + high))
                total = b_vols + a_vols
                metrics["perp_imbalance"][zone_name] = (b_vols - a_vols) / total if total > 0 else 0.0
        if self.recent_trades:
            metrics["trade_speed_ps"] = len(self.recent_trades) / 10.0
            buy_vol = sum(t["price"] * t["volume"] for t in self.recent_trades if t["side"].lower() == "buy")
            sell_vol = sum(t["price"] * t["volume"] for t in self.recent_trades if t["side"].lower() != "buy")
            metrics["net_delta_usdt"] = buy_vol - sell_vol
            first_price = self.recent_trades[0]["price"]
            last_price = self.recent_trades[-1]["price"]
            metrics["price_change_10s_pct"] = ((last_price - first_price) / first_price) * 100
            if (buy_vol + sell_vol) > 50000 and abs(metrics["perp_imbalance"]["zone_strike"]) < 0.05:
                metrics["is_wash_trading"] = True
        return metrics
