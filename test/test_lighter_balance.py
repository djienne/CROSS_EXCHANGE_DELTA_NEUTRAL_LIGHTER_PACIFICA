import sys
import os
# Add parent directory to path to allow imports from parent folder
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

"""
Test script to get the available balance from Lighter.
"""
import asyncio
from dotenv import load_dotenv
from lighter_client import get_lighter_balance, BalanceFetchError
from typing import Tuple

async def get_lighter_balances(ws_url: str, account_index: int) -> Tuple[float, float]:
    """
    Fetches and returns the available balance and portfolio value from Lighter.

    Args:
        ws_url: Lighter WebSocket URL
        account_index: Account index

    Returns:
        A tuple containing (available_balance, portfolio_value).
    """
    print("Fetching Lighter balances...")
    try:
        available_balance, portfolio_value = await get_lighter_balance(ws_url, account_index)
        return available_balance, portfolio_value
    except BalanceFetchError as e:
        print(f"Error fetching balances: {e}")
        raise

async def main():
    """
    Main function to get and display the available balance.
    """
    # Load environment variables from .env file
    load_dotenv()

    account_index = int(os.getenv("ACCOUNT_INDEX", "0"))
    ws_url = (
        os.getenv("LIGHTER_WS_URL")
        or os.getenv("WEBSOCKET_URL")
        or "wss://mainnet.zklighter.elliot.ai/stream"
    )

    # Get balances
    try:
        available_balance_data, portfolio_value_data = await get_lighter_balances(ws_url, account_index)
        print(f"\nPortfolio Value: ${portfolio_value_data:.2f}")
        print(f"Available Balance: ${available_balance_data:.2f}")
        print(f"Explicit Portfolio Value: {portfolio_value_data}")
        print(f"Explicit Available Balance: {available_balance_data}")
    except Exception:
        # Error is already printed in the get_lighter_balances function
        return

if __name__ == "__main__":
    asyncio.run(main())
