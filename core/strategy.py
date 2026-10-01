# core/strategy.py
import logging
import time
from typing import Optional

from core.trigger import WallTrigger

logger = logging.getLogger("Strategy")


class RocketStrategy:
    def __init__(self, order_manager):
        self.order_manager = order_manager          # BybitOrderManager
        self.triggers: dict[str, WallTrigger] = {}  # symbol → WallTrigger
        self.positions: dict[str, dict] = {}        # symbol → {side, qty, entry_price, order_id}

    # ─────────────────────────────────────────────
    #  Главный метод — вызывается из main.py каждую секунду
    # ─────────────────────────────────────────────

    async def evaluate(self, sym: str, engine, daily_pnl: float):
        import config

        # Лимит убытков за день → не входим
        if daily_pnl <= -config.DAILY_LOSS_LIMIT:
            return

        # Лимит одновременных позиций
        if sym not in self.positions and len(self.positions) >= config.MAX_POSITIONS:
            return

        # Создаём триггер при первом обращении
        if sym not in self.triggers:
            self.triggers[sym] = WallTrigger(sym)

        trigger = self.triggers[sym]
        signal = trigger.update(engine)

        # ── ВХОД ────────────────────────────────────────────────────
        if signal["action"] == "enter" and sym not in self.positions:
            bids = engine.perp_bids
            asks = engine.perp_asks
            if not bids or not asks:
                return

            direction = signal["direction"]
            best_bid = max(bids)
            best_ask = min(asks)

            # LIMIT: покупаем на best_bid, продаём на best_ask (maker)
            if direction == "long":
                price = best_bid
                side = "Buy"
            else:
                price = best_ask
                side = "Sell"

            qty = self._calc_qty(price)
            if qty is None:
                return

            logger.info("[%s] → размещаю LIMIT %s %.5f x %.2f", sym, side, price, qty)

            order = await self.order_manager.place_limit(
                symbol=sym,
                side=side,
                price=price,
                qty=qty,
            )
            if not order:
                return

            self.positions[sym] = {
                "direction": direction,
                "side": side,
                "qty": qty,
                "entry_price": price,
                "order_id": order["orderId"],
                "filled": False,
                "open_time": time.time(),
            }

            trigger.notify_entry(
                direction=direction,
                price=price,
                wall_price=signal["wall_price"],
                wall_usdt=signal["wall_usdt"],
            )

            logger.info("[%s] ✅ Ордер размещён | %s %.5f qty=%.2f | %s",
                        sym, side, price, qty, signal["reason"])

        # ── ВЫХОД (фальстарт или TP/SL) ─────────────────────────────
        elif signal["action"] == "exit" and sym in self.positions:
            pos = self.positions[sym]
            logger.warning("[%s] ⚠️ Выход: %s", sym, signal["reason"])

            # Отменяем ордер если не исполнен
            if not pos["filled"]:
                await self.order_manager.cancel_order(sym, pos["order_id"])
            else:
                # Закрываем позицию обратным LIMIT
                close_side = "Sell" if pos["direction"] == "long" else "Buy"
                bids = engine.perp_bids
                asks = engine.perp_asks
                close_price = min(asks) if close_side == "Sell" else max(bids)
                await self.order_manager.place_limit(
                    symbol=sym,
                    side=close_side,
                    price=close_price,
                    qty=pos["qty"],
                    reduce_only=True,
                )

            del self.positions[sym]
            trigger.notify_exit()

        # ── Проверяем исполнение висящих ордеров ─────────────────────
        elif sym in self.positions and not self.positions[sym]["filled"]:
            await self._check_fill(sym)

    # ─────────────────────────────────────────────
    #  Вызывается из main.py периодически (POSITION_RECONCILE_SEC)
    # ─────────────────────────────────────────────

    async def reconcile(self) -> float:
        """
        Сверяется с биржей: какие позиции реально закрылись.
        Возвращает реализованный PnL за период.
        """
        realized = 0.0
        closed = []

        for sym, pos in self.positions.items():
            position_info = await self.order_manager.get_position(sym)
            if position_info is None:
                continue

            # Если биржа говорит size=0 — позиция закрылась
            if float(position_info.get("size", 1)) == 0:
                pnl = float(position_info.get("unrealisedPnl", 0))
                realized += pnl
                closed.append(sym)
                logger.info("[%s] Позиция закрыта на бирже, PnL: %.4f", sym, pnl)

        for sym in closed:
            del self.positions[sym]
            if sym in self.triggers:
                self.triggers[sym].notify_exit()

        return realized

    async def load_today_pnl(self) -> float:
        """Загружает реализованный PnL за сегодня с биржи."""
        return await self.order_manager.get_today_pnl()

    # ─────────────────────────────────────────────
    #  Лог-снапшот (вызывается из main.py каждые 10с)
    # ─────────────────────────────────────────────

    def log_snapshot(self, engines: dict, top_n: int = 5):
        lines = []
        for sym, engine in list(engines.items())[:top_n]:
            bids = engine.perp_bids
            asks = engine.perp_asks
            if not bids or not asks:
                continue
            mid = (max(bids) + min(asks)) / 2
            m = engine.calculate_metrics()
            in_pos = "📍" if sym in self.positions else "  "
            lines.append(
                f"{in_pos} {sym:12s} mid={mid:.5f} "
                f"delta={m['net_delta_usdt']:+.0f} "
                f"imb={m['perp_imbalance']['zone_strike']:+.2f}"
            )
        if lines:
            logger.info("── Снапшот ──\n" + "\n".join(lines))

    # ─────────────────────────────────────────────
    #  Вспомогательные
    # ─────────────────────────────────────────────

    def _calc_qty(self, price: float) -> Optional[float]:
        """
        Размер позиции: config.TRADE_USDT / цена.
        Возвращает None если цена = 0.
        """
        import config
        if price <= 0:
            return None
        qty = round(config.TRADE_USDT / price, 2)
        return qty if qty > 0 else None

    async def _check_fill(self, sym: str):
        """Проверяет, исполнился ли ордер на входе."""
        pos = self.positions[sym]
        order = await self.order_manager.get_order(sym, pos["order_id"])
        if order and order.get("orderStatus") == "Filled":
            pos["filled"] = True
            logger.info("[%s] ✅ Ордер исполнен @ %.5f", sym, pos["entry_price"])