import requests
import time
import config

print("=== ЗАПУСК ЭХО-ЛОКАТОРА ===")
print(f"Использую токен: {config.TELEGRAM_TOKEN[:15]}...")
print("ИНСТРУКЦИЯ: Открой Telegram на телефоне, зайди в своего бота и НАПИШИ ЕМУ ЛЮБОЕ СЛОВО (например: Привет).")
print("Ожидаю сообщение в Telegram...", flush=True)

url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/getUpdates"

while True:
    try:
        res = requests.get(url, timeout=5).json()
        if "result" in res and len(res["result"]) > 0:
            last_update = res["result"][-1]

            if "message" in last_update:
                chat_info = last_update["message"]["chat"]
                user_text = last_update["message"].get("text", "")

                print("\n🎉 УРА! КАНАЛ СВЯЗИ ОБНАРУЖЕН!")
                print(f"📩 Бот получил от тебя текст: '{user_text}'")
                print(f"🔥 ТВОЙ ИСТИННЫЙ CHAT_ID ДЛЯ CONFIG.PY: '{chat_info['id']}'")
                print(f"👤 Имя чата: {chat_info.get('first_name', '')} {chat_info.get('username', '')}")
                break
        time.sleep(1)
    except KeyboardInterrupt:
        print("\nЛокатор остановлен пользователем.")
        break
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        time.sleep(2)

input("\nНажми Enter для завершения...")
