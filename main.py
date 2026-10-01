import asyncio
import logging
import sys
import time
from datetime import datetime
import requests
import config
from core.orderbook import MarketDataEngine
from core.ws_manager import BybitWSManager
from core.strategy import RocketStrategy
from execution.order_manager import BybitOrderManager

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("RocketHunter")

print("=== STARTING ROCKET LOWER TIER AUTO V3 ===", flush=True)

order_manager = BybitOrderManager(
    api_key=config.API_KEY,
    api_secret=config.API_SECRET,
    account_mode=config.ACCOUNT_MODE,
)

daily_pnl = 0.0

# Создаем глобальные менеджеры, чтобы они были видны везде
ws_manager = BybitWSManager(hdd_path=config.HDD_PATH)
strategy = RocketStrategy(order_manager)


def send_tg_msg(text):
    """Отправка сообщения в Telegram через официальный Bot API."""
    if not config.TELEGRAM_TOKEN or not config.CHAT_ID:
        return 0, "Telegram не настроен (нет переменных ROCKET_TG_TOKEN / ROCKET_TG_CHAT_ID)"
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        return res.status_code, res.text
    except Exception as e:
        return -1, str(e)


def filter_valid_symbols() -> None:
    """
    Оставляет в списках монет только те, что реально торгуются на Bybit как USDT-перпетуалы.
    Публичный запрос, ключ не нужен. Зачем: если в списке есть несуществующее название
    (например, PEPEUSDT вместо 1000PEPEUSDT), биржа может отклонить подписку, а бот по этой монете
    будет молча "слепым".
    """
    base = "https://api-testnet.bybit.com" if config.ACCOUNT_MODE == "testnet" else "https://api.bybit.com"
    valid = set()
    try:
        cursor = ""
        while True:
            params = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            r = requests.get(base + "/v5/market/instruments-info", params=params, timeout=10).json()
            for item in r["result"]["list"]:
                if item.get("status") == "Trading":
                    valid.add(item["symbol"])
            cursor = r["result"].get("nextPageCursor") or ""
            if not cursor:
                break
    except Exception as e:
        logger.warning("Не удалось проверить список монет на бирже (%s) — использую список из config как есть", e)
        return

    missing = [s for s in config.FULL_WATCH_LIST if s not in valid]
    if missing:
        logger.warning("Этих монет нет среди торгуемых USDT-перпетуалов Bybit, исключаю: %s", ", ".join(missing))
    config.TIER_1_LEADERS = [s for s in config.TIER_1_LEADERS if s in valid]
    config.TIER_1_ALTS = [s for s in config.TIER_1_ALTS if s in valid]
    config.TIER_2_ALTS = [s for s in config.TIER_2_ALTS if s in valid]
    config.TIER_3_ALTS = [s for s in config.TIER_3_ALTS if s in valid]
    config.FULL_WATCH_LIST = (
        config.TIER_1_LEADERS + config.TIER_1_ALTS + config.TIER_2_ALTS + config.TIER_3_ALTS
    )


async def main_loop():
    global daily_pnl

    logger.info("Проверка моста связи с Telegram...")
    test_code, test_resp = send_tg_msg("🏁 **Проверка связи: Робот В3 успешно запущен!**")
    if test_code == 200:
        logger.info("📱 МОСТ TELEGRAM: УСПЕШНО ПОДКЛЮЧЕН!")
    elif test_code == 0:
        logger.warning("Telegram отключён: %s", test_resp)
    else:
        logger.error("❌ МОСТ СЛОМАН! Код: %s. Ответ: %s", test_code, test_resp)

    filter_valid_symbols()
    logger.info("Инициализация аналитических движков (%d монет)...", len(config.FULL_WATCH_LIST))
    engines = {sym: MarketDataEngine(sym) for sym in config.FULL_WATCH_LIST}

    # Запускаем фоновое сканирование рынка. Ссылку на задачу храним, иначе Python может её "потерять".
    ws_task = asyncio.create_task(ws_manager.start_streaming(engines))
    logger.info("Робот в режиме ТРЁХ ЭШЕЛОНОВ запущен.")

    # После перезапуска: подхватываем уже открытые на бирже позиции и сегодняшний PnL
    await strategy.reconcile()
    daily_pnl = await strategy.load_today_pnl()
    logger.info("Реализованный PnL за сегодня: %.2f USDT", daily_pnl)
    current_day = datetime.now().date()

    last_reconcile_ts = 0.0
    last_snapshot_ts = 0.0

    while True:
        try:
            await asyncio.sleep(1)
            current_ts_ms = int(time.time() * 1000)
            now = time.time()

            if ws_task.done():
                logger.error("Поток рыночных данных остановился: %r — перезапусти бота", ws_task.exception())

            # Новый день — обнуляем дневной счётчик убытков
            today = datetime.now().date()
            if today != current_day:
                current_day = today
                daily_pnl = 0.0
                logger.info("Новый день: дневной PnL обнулён.")

            # Чистим старые сделки и ищем сигнал входа по каждой монете.
            # Ошибка по одной монете не должна останавливать обработку остальных.
            for sym, engine in engines.items():
                engine.clear_old_trades(current_ts_ms)
                try:
                    await strategy.evaluate(sym, engine, daily_pnl)
                except Exception:
                    logger.exception("Ошибка при обработке %s", sym)

            # Периодически сверяемся с биржей: какие позиции реально закрылись и с каким PnL
            if now - last_reconcile_ts >= config.POSITION_RECONCILE_SEC:
                last_reconcile_ts = now
                realized = await strategy.reconcile()
                if realized:
                    daily_pnl += realized
                    logger.info("Дневной PnL обновлён: %.2f USDT", daily_pnl)
                    if daily_pnl <= -config.DAILY_LOSS_LIMIT:
                        logger.warning("⛔ Дневной лимит убытков достигнут (%.2f USDT). Новые входы остановлены.", daily_pnl)

            if now - last_snapshot_ts >= 10:
                last_snapshot_ts = now
                buffered_total = len(ws_manager.db_buffer)
                logger.info(
                    " Сбор данных активен по %s монетам. Открытых позиций: %s. Буфер HDD: %s/2000",
                    len(config.FULL_WATCH_LIST),
                    len(strategy.positions),
                    buffered_total,
                )
                strategy.log_snapshot(engines, top_n=5)

        except Exception:
            logger.exception("Ошибка в главном цикле")
            await asyncio.sleep(2)


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main_loop())
