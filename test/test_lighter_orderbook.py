import sys
import os
# Add parent directory to path to allow imports from parent folder
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

"""
Test script to fetch orderbook data from Lighter.
"""
import asyncio
from dotenv import load_dotenv
import lighter
from lighter_client import get_lighter_best_bid_ask, get_lighter_market_details

async def main():
    """
    Main function to fetch and display orderbook data.
    """
    print("=" * 60)
    print("Lighter Orderbook Test")
    print("=" * 60)

    # Load environment variables from .env file
    load_dotenv()

    api_key = os.getenv("API_KEY_PRIVATE_KEY")
    base_url = (
        os.getenv("LIGHTER_BASE_URL")
        or os.getenv("BASE_URL")
        or "https://mainnet.zklighter.elliot.ai"
    )

    if not api_key:
        print("Error: Missing required environment variable: API_KEY_PRIVATE_KEY")
        return

    # Initialize Lighter API client
    try:
        lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=base_url))
        order_api = lighter.OrderApi(lighter_api_client)
    except Exception as e:
        print(f"Error initializing Lighter API client: {e}")
        return

    # Test symbols
    symbols_to_test = ["BTC", "ETH", "SOL", "ASTER"]

    print("\n=== Fetching Orderbook Data ===\n")

    for symbol in symbols_to_test:
        try:
            # Get market ID
            market_id, price_tick, amount_tick = await get_lighter_market_details(order_api, symbol)

            # Get best bid/ask
            bid, ask = await get_lighter_best_bid_ask(order_api, symbol, market_id)

            if bid and ask:
                spread = ask - bid
                spread_pct = (spread / bid) * 100 if bid > 0 else 0
                mid_price = (bid + ask) / 2

                print(f"{symbol}:")
                print(f"  Market ID: {market_id}")
                print(f"  Best Bid: ${bid:.4f}")
                print(f"  Best Ask: ${ask:.4f}")
                print(f"  Mid Price: ${mid_price:.4f}")
                print(f"  Spread: ${spread:.4f} ({spread_pct:.3f}%)")
                print(f"  Price Tick: {price_tick}")
                print(f"  Amount Tick: {amount_tick}")
            else:
                print(f"{symbol}: Could not fetch orderbook data")

            print()

        except Exception as e:
            print(f"{symbol}: Error - {e}\n")

    # Cleanup
    await lighter_api_client.close()

    print("=" * 60)
    print("Orderbook test finished.")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
