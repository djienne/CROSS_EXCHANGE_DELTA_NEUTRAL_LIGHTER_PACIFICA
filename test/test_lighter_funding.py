import sys
import os
# Add parent directory to path to allow imports from parent folder
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

"""
Test script to get funding rates for a list of symbols from Lighter and rank them.
"""
import json
import asyncio
from dotenv import load_dotenv
import lighter
from lighter_client import get_lighter_funding_rate, get_lighter_market_details

async def main():
    """
    Main function to get and rank funding rates.
    """
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

    # Load symbols from bot_config.json
    try:
        with open("bot_config.json", "r") as f:
            config = json.load(f)
        symbols = config.get("symbols_to_monitor", [])
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading bot_config.json: {e}")
        return

    if not symbols:
        print("No symbols found in bot_config.json")
        return

    # Initialize Lighter API client
    try:
        lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=base_url))
        order_api = lighter.OrderApi(lighter_api_client)
        funding_api = lighter.FundingApi(lighter_api_client)
    except Exception as e:
        print(f"Error initializing Lighter API client: {e}")
        return

    # Get funding rates
    print("Fetching funding rates from Lighter...")
    funding_rates = {}

    for symbol in symbols:
        try:
            # Get market ID first
            market_id, _, _ = await get_lighter_market_details(order_api, symbol)

            # Get funding rate
            funding_rate = await get_lighter_funding_rate(funding_api, market_id)
            if funding_rate is not None:
                funding_rates[symbol] = funding_rate
        except Exception as e:
            print(f"Error fetching funding rate for {symbol}: {e}")
            continue

    # Sort symbols by funding rate in descending order
    ranked_symbols = sorted(funding_rates.items(), key=lambda item: item[1], reverse=True)

    # Print ranked list
    print("\n--- APR Ranking (based on 8-hour funding) ---")
    for symbol, rate in ranked_symbols:
        # Convert 8-hour rate to hourly equivalent (÷8) then annualise (8760 hours/year)
        hourly_rate = rate / 8
        periods_per_year = 365 * 24
        apr = hourly_rate * periods_per_year * 100
        print(f"{symbol}: {apr:.2f}% APR (8h rate: {rate*100:.4f}%)")

    # Cleanup
    await lighter_api_client.close()

if __name__ == "__main__":
    asyncio.run(main())
