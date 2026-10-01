# core/trigger.py
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("Trigger")

# ─────────────────────────────────────────────
#  Конфиг (можно перенести в config.py)
# ─────────────────────────────────────────────
WALL_MIN_USDT       = 50_000   # минимальный объём стены
WALL_MIN_AGE_SEC    = 30       # стена должна прожить ≥ 30с
WALL_VANISH_PCT     = 0.30     # стена -30% → фальстарт
MOVE_MIN_PCT        = 0.0015   # 0.15% — минимальное движение за 60с
STALL_TIMEOUT_SEC   = 60       # нет движения → фальстарт
COOLDOWN_SEC        = 120      # пауза после фальстарта
DEPTH_LEVELS        = 3        # сколько уровней стакана проверяем на стену


# ─────────────────────────────────────────────
#  Структура «живой стены»
# ─────────────────────────────────────────────
@dataclass
class WallRecord:
    price: float
    side: str          # "bid" | "ask"
    usdt_vol: float
    first_seen: float  # time.time()
    last_seen: float
    peak_vol: float    # максимальный объём за жизнь стены

    @property
    def age_sec(self) -> float:
        return self.last_seen - self.first_seen

    @property
    def is_mature(self) -> bool:
        return self.age_sec >= WALL_MIN_AGE_SEC

    def vanished(self, current_vol: float) -> bool:
        """Стена исчезла если объём упал на WALL_VANISH_PCT от пика."""
        if self.peak_vol == 0:
            return True
        return current_vol < self.peak_vol * (1 - WALL_VANISH_PCT)


# ─────────────────────────────────────────────
#  Состояние после входа в позицию
# ─────────────────────────────────────────────
@dataclass
class EntryState:
    direction: str       # "long" | "short"
    entry_price: float
    entry_time: float
    entry_wall_price: float
    entry_wall_usdt: float
    last_checked_price: float = 0.0
    # Для cooldown
    false_start_time: Optional[float] = None


# ─────────────────────────────────────────────
#  Основной детектор
# ─────────────────────────────────────────────
class WallTrigger:
    """
    Детектор стен и условий входа/выхода для одного символа.
    Вызывается из RocketStrategy.evaluate() на каждом тике (1с).
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self._walls: dict[float, WallRecord] = {}  # price → WallRecord
        self._cooldown_until: float = 0.0
        self._entry: Optional[EntryState] = None

    # ── публичный API ────────────────────────────────────────────────

    def in_cooldown(self) -> bool:
        return time.time() < self._cooldown_until

    def has_position(self) -> bool:
        return self._entry is not None

    def notify_entry(self, direction: str, price: float,
                     wall_price: float, wall_usdt: float):
        """Стратегия сообщает, что вошла в позицию."""
        self._entry = EntryState(
            direction=direction,
            entry_price=price,
            entry_time=time.time(),
            entry_wall_price=wall_price,
            entry_wall_usdt=wall_usdt,
            last_checked_price=price,
        )
        logger.info("[%s] Позиция зафиксирована: %s @ %.5f, стена %.0f USDT",
                    self.symbol, direction, price, wall_usdt)

    def notify_exit(self):
        """Стратегия сообщает о закрытии позиции."""
        self._entry = None

    # ── основной метод ───────────────────────────────────────────────

    def update(self, engine) -> dict:
        """
        Вызывай каждую секунду, передавая MarketDataEngine.
        Возвращает signal-dict:

        Для входа:
          {"action": "enter", "direction": "long"|"short",
           "wall_price": float, "wall_usdt": float, "reason": str}

        Для выхода (фальстарт):
          {"action": "exit", "reason": str}

        Нет сигнала:
          {"action": "none"}
        """
        now = time.time()

        # Обновляем карту живых стен из стакана
        self._refresh_walls(engine, now)

        # Если в позиции — проверяем условия выхода (фальстарт)
        if self._entry:
            return self._check_exit(engine, now)

        # Если кулдаун — не ищем вход
        if self.in_cooldown():
            remaining = self._cooldown_until - now
            logger.debug("[%s] Cooldown %.0fс", self.symbol, remaining)
            return {"action": "none"}

        # Ищем сигнал входа
        return self._check_entry(engine, now)

    # ── внутренние методы ────────────────────────────────────────────

    def _refresh_walls(self, engine, now: float):
        """
        Актуализируем WallRecord-ы:
        — добавляем новые крупные уровни
        — удаляем исчезнувшие
        """
        bids = engine.perp_bids
        asks = engine.perp_asks
        if not bids or not asks:
            return

        best_bid = max(bids)
        best_ask = min(asks)

        # Кандидаты на стену: топ-DEPTH_LEVELS уровней с каждой стороны
        top_bids = sorted(bids.items(), key=lambda x: -x[0])[:DEPTH_LEVELS * 5]
        top_asks = sorted(asks.items(), key=lambda x: x[0])[:DEPTH_LEVELS * 5]

        active_prices = set()

        for price, vol in top_bids:
            usdt = price * vol
            if usdt >= WALL_MIN_USDT:
                active_prices.add(price)
                if price in self._walls:
                    w = self._walls[price]
                    w.last_seen = now
                    w.usdt_vol = usdt
                    w.peak_vol = max(w.peak_vol, usdt)
                else:
                    self._walls[price] = WallRecord(
                        price=price, side="bid", usdt_vol=usdt,
                        first_seen=now, last_seen=now, peak_vol=usdt
                    )

        for price, vol in top_asks:
            usdt = price * vol
            if usdt >= WALL_MIN_USDT:
                active_prices.add(price)
                if price in self._walls:
                    w = self._walls[price]
                    w.last_seen = now
                    w.usdt_vol = usdt
                    w.peak_vol = max(w.peak_vol, usdt)
                else:
                    self._walls[price] = WallRecord(
                        price=price, side="ask", usdt_vol=usdt,
                        first_seen=now, last_seen=now, peak_vol=usdt
                    )

        # Удаляем стены, которых больше нет в стакане
        gone = [p for p in self._walls if p not in active_prices]
        for p in gone:
            del self._walls[p]

    def _check_entry(self, engine, now: float) -> dict:
        """
        Условие входа:
          1. Есть зрелая стена (≥ WALL_MIN_AGE_SEC) ≥ WALL_MIN_USDT
          2. Net delta подтверждает направление
        """
        bids = engine.perp_bids
        asks = engine.perp_asks
        if not bids or not asks:
            return {"action": "none"}

        best_bid = max(bids)
        best_ask = min(asks)
        mid = (best_bid + best_ask) / 2

        metrics = engine.calculate_metrics()
        net_delta = metrics.get("net_delta_usdt", 0.0)

        # Ищем зрелую bid-стену (давление снизу → лонг)
        mature_bid_walls = [
            w for w in self._walls.values()
            if w.side == "bid" and w.is_mature and w.price < mid
        ]
        # Ищем зрелую ask-стену (давление сверху → шорт)
        mature_ask_walls = [
            w for w in self._walls.values()
            if w.side == "ask" and w.is_mature and w.price > mid
        ]

        # Выбираем самую крупную зрелую стену
        best_bid_wall = max(mature_bid_walls, key=lambda w: w.usdt_vol, default=None)
        best_ask_wall = max(mature_ask_walls, key=lambda w: w.usdt_vol, default=None)

        # LONG: bid-стена + положительная delta
        if best_bid_wall and net_delta > 0:
            logger.info(
                "[%s] 🟢 СИГНАЛ ЛОНГ | стена %.5f %.0f USDT возраст %.0fс | delta +%.0f",
                self.symbol, best_bid_wall.price, best_bid_wall.usdt_vol,
                best_bid_wall.age_sec, net_delta
            )
            return {
                "action": "enter",
                "direction": "long",
                "wall_price": best_bid_wall.price,
                "wall_usdt": best_bid_wall.usdt_vol,
                "reason": f"bid_wall {best_bid_wall.usdt_vol:.0f} USDT age {best_bid_wall.age_sec:.0f}s delta +{net_delta:.0f}",
            }

        # SHORT: ask-стена + отрицательная delta
        if best_ask_wall and net_delta < 0:
            logger.info(
                "[%s] 🔴 СИГНАЛ ШОРТ | стена %.5f %.0f USDT возраст %.0fс | delta %.0f",
                self.symbol, best_ask_wall.price, best_ask_wall.usdt_vol,
                best_ask_wall.age_sec, net_delta
            )
            return {
                "action": "enter",
                "direction": "short",
                "wall_price": best_ask_wall.price,
                "wall_usdt": best_ask_wall.usdt_vol,
                "reason": f"ask_wall {best_ask_wall.usdt_vol:.0f} USDT age {best_ask_wall.age_sec:.0f}s delta {net_delta:.0f}",
            }

        return {"action": "none"}

    def _check_exit(self, engine, now: float) -> dict:
        """
        Условия фальстарта (→ выход + cooldown 120с):
          A. 60с нет движения ≥ 0.15%
          B. Стена-опора упала на 30% от пика
          C. Net delta развернулась против направления
        """
        entry = self._entry
        bids = engine.perp_bids
        asks = engine.perp_asks
        if not bids or not asks:
            return {"action": "none"}

        best_bid = max(bids)
        best_ask = min(asks)
        current_price = (best_bid + best_ask) / 2
        metrics = engine.calculate_metrics()
        net_delta = metrics.get("net_delta_usdt", 0.0)

        elapsed = now - entry.entry_time

        # A. Нет движения за STALL_TIMEOUT_SEC
        if elapsed >= STALL_TIMEOUT_SEC:
            price_move = abs(current_price - entry.entry_price) / entry.entry_price
            if price_move < MOVE_MIN_PCT:
                return self._false_start(
                    f"stall: движение {price_move*100:.3f}% за {elapsed:.0f}с < {MOVE_MIN_PCT*100}%"
                )

        # B. Стена-опора исчезла / похудела
        wall_rec = self._walls.get(entry.entry_wall_price)
        if wall_rec is None:
            # Стена полностью ушла из стакана
            return self._false_start("wall_gone: стена-опора исчезла из стакана")
        if wall_rec.vanished(wall_rec.usdt_vol):
            return self._false_start(
                f"wall_shrink: объём стены {wall_rec.usdt_vol:.0f} < "
                f"{wall_rec.peak_vol * (1 - WALL_VANISH_PCT):.0f} USDT"
            )

        # C. Delta развернулась
        if entry.direction == "long" and net_delta < -WALL_MIN_USDT * 0.5:
            return self._false_start(f"delta_flip: delta {net_delta:.0f} при лонге")
        if entry.direction == "short" and net_delta > WALL_MIN_USDT * 0.5:
            return self._false_start(f"delta_flip: delta +{net_delta:.0f} при шорте")

        return {"action": "none"}

    def _false_start(self, reason: str) -> dict:
        self._entry = None
        self._cooldown_until = time.time() + COOLDOWN_SEC
        logger.warning("[%s] ⚠️ ФАЛЬСТАРТ → cooldown %dс | %s",
                       self.symbol, COOLDOWN_SEC, reason)
        return {"action": "exit", "reason": reason}