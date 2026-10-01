import asyncio
import config
from execution.order_manager import BybitOrderManager


async def main():
    print(f"Режим: {config.ACCOUNT_MODE}")
    print(f"Ключ (первые 8 символов): {config.API_KEY[:8]}...")

    om = BybitOrderManager(
        api_key=config.API_KEY,
        api_secret=config.API_SECRET,
        account_mode=config.ACCOUNT_MODE,
    )

    print("\n--- Проверка позиций (/v5/position/list) ---")
    positions = await om.get_open_positions()
    print(positions)

    print("\n--- Проверка баланса кошелька (/v5/account/wallet-balance) ---")
    balance = await om._send_get_request("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    print(balance)


if __name__ == "__main__":
    asyncio.run(main())
