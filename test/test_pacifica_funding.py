import sys
import os
# Add parent directory to path to allow imports from parent folder
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

"""
Test script to get funding fees for a list of symbols and rank them.
"""
import json
from dotenv import load_dotenv
from pacifica_client import PacificaClient

def main():
    """
    Main function to get and rank funding fees.
    """
    # Load environment variables from .env file
    load_dotenv()

    sol_wallet = os.getenv("SOL_WALLET")
    api_public = os.getenv("API_PUBLIC")
    api_private = os.getenv("API_PRIVATE")

    if not all([sol_wallet, api_public, api_private]):
        print("Error: Missing required environment variables: SOL_WALLET, API_PUBLIC, API_PRIVATE")
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

    # Initialize PacificaClient
    try:
        client = PacificaClient(
            sol_wallet=sol_wallet,
            api_public=api_public,
            api_private=api_private
        )
    except Exception as e:
        print(f"Error initializing PacificaClient: {e}")
        return

    # Get funding fees
    print("Fetching funding fees...")
    try:
        funding_fees = client.get_funding_fees(symbols)
    except Exception as e:
        print(f"Error fetching funding fees: {e}")
        return

    # Sort symbols by funding rate in descending order
    ranked_symbols = sorted(funding_fees.items(), key=lambda item: item[1], reverse=True)

    # Print ranked list
    print("\n--- APR Ranking (based on hourly funding) ---")
    for symbol, rate in ranked_symbols:
        # APR = hourly rate × number of hours in a year
        hourly_rate = rate
        periods_per_year = 365 * 24  # hourly periods in a year
        apr = hourly_rate * periods_per_year * 100
        print(f"{symbol}: {apr:.2f}% APR (hourly rate: {rate*100:.4f}%)")

if __name__ == "__main__":
    main()
