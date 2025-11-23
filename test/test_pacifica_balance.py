import sys
import os
# Add parent directory to path to allow imports from parent folder
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

"""
Test script to get the available balance from Pacifica.
"""
from dotenv import load_dotenv
from pacifica_client import PacificaClient
from typing import Tuple

def get_pacifica_balances(client: PacificaClient) -> Tuple[float, float]:
    """
    Fetches and returns the equity and available balance from Pacifica.

    Args:
        client: An initialized PacificaClient instance.

    Returns:
        A tuple containing the equity and available balance.
        Example structure: (10000.0, 8500.50)
    """
    print("Fetching Pacifica balances...")
    try:
        equity = client.get_equity()
        available_balance = client.get_available_balance()
        return equity, available_balance
    except Exception as e:
        print(f"Error fetching balances: {e}")
        raise

def main():
    """
    Main function to get and display the available balance.
    """
    # Load environment variables from .env file
    load_dotenv()

    sol_wallet = os.getenv("SOL_WALLET")
    api_public = os.getenv("API_PUBLIC")
    api_private = os.getenv("API_PRIVATE")

    if not all([sol_wallet, api_public, api_private]):
        print("Error: Missing required environment variables: SOL_WALLET, API_PUBLIC, API_PRIVATE")
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

    # Get balances
    try:
        equity_data, available_balance_data = get_pacifica_balances(client)
        print(f"\nEquity: ${equity_data:.2f}")
        print(f"Available Balance: ${available_balance_data:.2f}")
        print(f"Explicit Equity: {equity_data}")
        print(f"Explicit Available Balance: {available_balance_data}")
    except Exception:
        # Error is already printed in the get_pacifica_balances function
        return

if __name__ == "__main__":
    main()

