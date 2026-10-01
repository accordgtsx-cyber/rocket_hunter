import sys
import time
from core.orderbook import MarketDataEngine

print(">>> ШАГ 1: Движок успешно запущен в консоли!", flush=True)

engine = MarketDataEngine("SUIUSDT")

dummy_bids = [["1.5000", "10000"], ["1.4900", "25000"]]
dummy_asks = [["1.5100", "15000"], ["1.5200", "30000"]]

print(">>> ШАГ 2: Пробуем скормить данные в локальный стакан...", flush=True)
engine.update_orderbook("perp", dummy_bids, dummy_asks)
engine.update_orderbook("spot", dummy_bids, dummy_asks)

print(">>> ШАГ 3: Пробуем добавить тестовые сделки в ленту...", flush=True)
current_ts = int(time.time() * 1000)
engine.add_trade("Buy", 1.5050, 5000, current_ts)
engine.add_trade("Buy", 1.5060, 8000, current_ts)

print(">>> ШАГ 4: Считаем послойный имбаланс и базис...", flush=True)
metrics = engine.calculate_metrics()

print("\n📊 РЕЗУЛЬТАТ ТЕСТА МЕТРИК:")
print(f"Базис (Spot/Perp): {metrics['basis_pct']:.4f}%")
print(f"Имбаланс (Зона удара): {metrics['perp_imbalance']['zone_strike']:.2f}")
print(f"Скорость ленты: {metrics['trade_speed_ps']:.1f} принт/сек")
print(f"Чистая дельта: {metrics['net_delta_usdt']:.2f} USDT")

print("\n>>> ШАГ 5: Тест математики ядра завершен успешно!", flush=True)
input("Нажми Enter для закрытия...")
