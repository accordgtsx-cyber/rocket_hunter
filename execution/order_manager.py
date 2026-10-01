# execution/order_manager.py
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from pybit.unified_trading import HTTP

logger = logging.getLogger("OrderManager")


class BybitOrderManager:
    def __init__(self, api_key: str, api_secret: str, account_mode: str = "mainnet"):
        testnet = account_mode == "testnet"
        self._client = HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret,
        )
        self._demo = account_mode == "demo"
        logger.info("OrderManager инициализирован | режим=%s testnet=%s", account_mode, testnet)

    # ─────────────────────────────────────────────
    #  LIMIT ордер (maker)
    # ─────────────────────────────────────────────

    async def place_limit(
        self,
        symbol: str,
        side: str,          # "Buy" | "Sell"
        price: float,
        qty: float,
        reduce_only: bool = False,
    ) -> Optional[dict]:
        """
        Размещает LIMIT ордер.
        timeInForce=PostOnly — гарантирует maker, биржа отклонит если цена
        пересечёт стакан (защита от случайного taker-исполнения).
        """
        try:
            params = dict(
                category="linear",
                symbol=symbol,
                side=side,
                orderType="Limit",
                qty=str(qty),
                price=str(price),
                timeInForce="PostOnly",
                reduceOnly=reduce_only,
            )
            if self._demo:
                params["isLeverage"] = 1  # demo требует явного флага

            resp = self._client.place_order(**params)
            if resp["retCode"] != 0:
                logger.error("place_limit ошибка [%s]: %s", resp["retCode"], resp["retMsg"])
                return None

            order = resp["result"]
            logger.info("✅ LIMIT %s %s | price=%.5f qty=%s | orderId=%s",
                        side, symbol, price, qty, order["orderId"])
            return order

        except Exception:
            logger.exception("place_limit exception [%s]", symbol)
            return None

    # ─────────────────────────────────────────────
    #  Отмена ордера
    # ─────────────────────────────────────────────

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            resp = self._client.cancel_order(
                category="linear",
                symbol=symbol,
                orderId=order_id,
            )
            if resp["retCode"] != 0:
                # 110001 = ордер уже не существует — не страшно
                if resp["retCode"] == 110001:
                    logger.debug("cancel_order: ордер %s уже не активен", order_id)
                    return True
                logger.error("cancel_order ошибка [%s]: %s", resp["retCode"], resp["retMsg"])
                return False

            logger.info("🗑 Ордер отменён: %s %s", symbol, order_id)
            return True

        except Exception:
            logger.exception("cancel_order exception [%s]", symbol)
            return False

    # ─────────────────────────────────────────────
    #  Статус ордера
    # ─────────────────────────────────────────────

    async def get_order(self, symbol: str, order_id: str) -> Optional[dict]:
        try:
            resp = self._client.get_open_orders(
                category="linear",
                symbol=symbol,
                orderId=order_id,
            )
            if resp["retCode"] != 0:
                logger.error("get_order ошибка [%s]: %s", resp["retCode"], resp["retMsg"])
                return None

            orders = resp["result"]["list"]
            if not orders:
                # Ордер исполнен или отменён — проверяем историю
                return await self._get_order_history(symbol, order_id)

            return orders[0]

        except Exception:
            logger.exception("get_order exception [%s]", symbol)
            return None

    async def _get_order_history(self, symbol: str, order_id: str) -> Optional[dict]:
        try:
            resp = self._client.get_order_history(
                category="linear",
                symbol=symbol,
                orderId=order_id,
                limit=1,
            )
            if resp["retCode"] != 0:
                return None
            orders = resp["result"]["list"]
            return orders[0] if orders else None
        except Exception:
            logger.exception("_get_order_history exception [%s]", symbol)
            return None

    # ─────────────────────────────────────────────
    #  Текущая позиция на бирже
    # ─────────────────────────────────────────────

    async def get_position(self, symbol: str) -> Optional[dict]:
        """
        Возвращает dict позиции или None.
        Если size == "0" → позиция закрыта.
        """
        try:
            resp = self._client.get_positions(
                category="linear",
                symbol=symbol,
            )
            if resp["retCode"] != 0:
                logger.error("get_position ошибка [%s]: %s", resp["retCode"], resp["retMsg"])
                return None

            positions = resp["result"]["list"]
            if not positions:
                return None

            # Берём первую ненулевую или просто первую
            for pos in positions:
                if float(pos.get("size", 0)) > 0:
                    return pos
            return positions[0]

        except Exception:
            logger.exception("get_position exception [%s]", symbol)
            return None

    # ─────────────────────────────────────────────
    #  Реализованный PnL за сегодня
    # ─────────────────────────────────────────────

    async def get_today_pnl(self) -> float:
        """
        Суммирует closedPnl по всем сделкам с начала текущего дня UTC.
        Bybit отдаёт максимум 7 дней, limit=200 на вызов.
        """
        try:
            # Начало сегодняшнего дня UTC в миллисекундах
            now = datetime.now(timezone.utc)
            start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            start_ms = int(start_of_day.timestamp() * 1000)

            total_pnl = 0.0
            cursor = ""

            while True:
                params = dict(
                    category="linear",
                    limit=200,
                    startTime=start_ms,
                )
                if cursor:
                    params["cursor"] = cursor

                resp = self._client.get_closed_pnl(**params)
                if resp["retCode"] != 0:
                    logger.error("get_today_pnl ошибка [%s]: %s",
                                 resp["retCode"], resp["retMsg"])
                    break

                result = resp["result"]
                for item in result.get("list", []):
                    total_pnl += float(item.get("closedPnl", 0))

                cursor = result.get("nextPageCursor", "")
                if not cursor:
                    break

            logger.info("PnL за сегодня: %.4f USDT", total_pnl)
            return total_pnl

        except Exception:
            logger.exception("get_today_pnl exception")
            return 0.0

    # ─────────────────────────────────────────────
    #  Установка плеча (вызвать один раз при старте)
    # ─────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            resp = self._client.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
            # retCode 110043 = плечо уже установлено — не ошибка
            if resp["retCode"] not in (0, 110043):
                logger.error("set_leverage ошибка [%s]: %s", resp["retCode"], resp["retMsg"])
                return False
            logger.info("Плечо %dx установлено для %s", leverage, symbol)
            return True
        except Exception:
            logger.exception("set_leverage exception [%s]", symbol)
            return False