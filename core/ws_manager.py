import asyncio
import json
import logging
import os
import pandas as pd
import websockets
from datetime import datetime
import config

logger = logging.getLogger("WS_Manager")


def _bybit_ws_url() -> str:
    """
    Публичные рыночные данные (стакан/сделки) у Bybit одни и те же для mainnet И для Demo Trading —
    отдельного публичного WS для демо-режима не существует, он просто зеркалит настоящий рынок.
    Отдельный публичный поток есть только у Testnet.
    """
    if config.ACCOUNT_MODE == "testnet":
        return "wss://stream-testnet.bybit.com/v5/public/linear"
    return "wss://stream.bybit.com/v5/public/linear"


class BybitWSManager:
    def __init__(self, hdd_path="D:/rocket_hunter_data"):
        self.hdd_path = hdd_path
        os.makedirs(self.hdd_path, exist_ok=True)
        self.db_buffer = []
        self.flush_limit = 2000
        self.running = True
        self._flushing = False
        self._bg_tasks = set()  # держим ссылки на фоновые задачи, иначе Python может их "потерять"

    async def start_streaming(self, engines: dict):
        tasks = [
            self._listen_tier_1(engines),
            self._listen_tier_2(engines),
            self._listen_tier_3(engines),
        ]
        await asyncio.gather(*tasks)

    async def _listen_tier_1(self, engines: dict):
        url = _bybit_ws_url()
        backoff = 3
        while self.running:
            try:
                async with websockets.connect(url, max_size=2**24) as ws:
                    backoff = 3  # сброс после удачного коннекта
                    symbols = config.TIER_1_LEADERS + config.TIER_1_ALTS
                    # Глубина 500 официально доступна не для всех символов линейных контрактов —
                    # для надёжности используем 200 для всех Tier-1, кроме BTC/ETH.
                    args = []
                    for sym in symbols:
                        depth = 200  # для линейных контрактов Bybit допустимы глубины 1, 50, 200, 1000; 500 биржа отклоняет
                        args.append(f"orderbook.{depth}.{sym}")
                    args += [f"publicTrade.{sym}" for sym in symbols]
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    self._spawn(self._send_ping(ws))
                    async for message in ws:
                        self._process_msg(json.loads(message), engines, tier=1)
            except Exception:
                logger.exception("Tier-1 WS разорван, переподключение через %sс", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _listen_tier_2(self, engines: dict):
        url = _bybit_ws_url()
        backoff = 3
        while self.running:
            try:
                async with websockets.connect(url, max_size=2**24) as ws:
                    backoff = 3
                    args = [f"orderbook.50.{sym}" for sym in config.TIER_2_ALTS]
                    args += [f"publicTrade.{sym}" for sym in config.TIER_2_ALTS]
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    self._spawn(self._send_ping(ws))
                    async for message in ws:
                        self._process_msg(json.loads(message), engines, tier=2)
            except Exception:
                logger.exception("Tier-2 WS разорван, переподключение через %sс", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _listen_tier_3(self, engines: dict):
        url = _bybit_ws_url()
        backoff = 3
        while self.running:
            try:
                async with websockets.connect(url, max_size=2**24) as ws:
                    backoff = 3
                    args = [f"publicTrade.{sym}" for sym in config.TIER_3_ALTS]
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    self._spawn(self._send_ping(ws))
                    async for message in ws:
                        self._process_msg(json.loads(message), engines, tier=3)
            except Exception:
                logger.exception("Tier-3 WS разорван, переподключение через %sс", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _process_msg(self, data: dict, engines: dict, tier: int):
        if data.get("op") == "subscribe" and data.get("success") is False:
            logger.error("Tier-%s: биржа отклонила подписку: %s", tier, data.get("ret_msg"))
            return
        if "topic" not in data or "data" not in data:
            return
        topic = data["topic"]
        symbol = topic.split(".")[-1]
        engine = engines.get(symbol)
        if not engine:
            return

        if "orderbook" in topic:
            engine.update_orderbook(
                "perp", data["data"].get("b", []), data["data"].get("a", []),
                is_snapshot=(data.get("type") == "snapshot"),
            )
        elif "publicTrade" in topic:
            for t in data["data"]:
                ts = t.get("T") or t.get("t") or int(datetime.now().timestamp() * 1000)
                price = float(t["p"])
                volume = float(t["v"])
                side = t.get("S") or t.get("s") or "Buy"

                engine.add_trade(side, price, volume, ts)
                self.db_buffer.append({
                    "timestamp": datetime.fromtimestamp(ts / 1000.0),
                    "symbol": symbol,
                    "market_type": f"tier_{tier}",
                    "price": price,
                    "volume": volume,
                    "side": side,
                })
            if len(self.db_buffer) >= self.flush_limit and not self._flushing:
                self._flushing = True
                self._spawn(self._flush_to_parquet())

    async def _flush_to_parquet(self):
        try:
            if not self.db_buffer:
                return
            to_write = self.db_buffer.copy()
            self.db_buffer.clear()

            def _write():
                df = pd.DataFrame(to_write)
                date_str = datetime.now().strftime("%Y-%m-%d")
                dir_path = os.path.join(self.hdd_path, date_str)
                os.makedirs(dir_path, exist_ok=True)
                # Каждая порция — отдельный файл: не нужно перечитывать и переписывать весь дневной
                # файл (он растёт до миллионов строк), и два потока не могут писать в один файл.
                file_path = os.path.join(dir_path, f"trades_{datetime.now():%H%M%S_%f}.parquet")
                df.to_parquet(file_path, compression="snappy", index=False)

            try:
                await asyncio.to_thread(_write)
            except Exception:
                logger.exception("Не удалось записать parquet — возвращаю данные в буфер")
                self.db_buffer[:0] = to_write
        finally:
            self._flushing = False

    async def _send_ping(self, ws):
        try:
            while True:
                await asyncio.sleep(20)
                await ws.send(json.dumps({"op": "ping"}))
        except Exception:
            pass
