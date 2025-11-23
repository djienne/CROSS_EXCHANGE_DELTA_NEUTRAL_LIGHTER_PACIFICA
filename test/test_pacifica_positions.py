import sys
import os
# Add parent directory to path to allow imports from parent folder
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


"""
Test script to get the current positions from Pacifica.
"""
from dotenv import load_dotenv
from pacifica_client import PacificaClient
from typing import Dict, Any

def get_pacifica_position(client: PacificaClient, symbol: str) -> Dict[str, Any]:
    """
    Fetches and returns the current position for a given symbol.
    
    Args:
        client: An initialized PacificaClient instance.
        symbol: The symbol to fetch the position for (e.g., "BTC").
        
    Returns:
        A dictionary containing the position details.
        Example structure:
        {
            "qty": 0.001,
            "entry_price": 65000.0,
            "unrealized_pnl": 15.0,
            "notional": 65.0,
            "opened_at": 1672531200.0
        }
    """
    print(f"Fetching {symbol} position...")
    try:
        position = client.get_position(symbol)
        return position
    except Exception as e:
        print(f"Error fetching {symbol} position: {e}")
        raise

def main():
    """
    Main function to get and display the BTC position.
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

    # Get BTC position
    try:
        symbol_to_test = "BTC"
        position_data = get_pacifica_position(client, symbol_to_test)
        position_qty = position_data.get("qty", 0)

        print(f"\n{symbol_to_test} Position: {position_data}")
        print(f"Explicit Quantity: {position_qty}")

        # Assert that there is a long position on BTC
        assert position_qty > 0, f"There is no long position on {symbol_to_test}."
        print(f"\nAssertion passed: {symbol_to_test} position is long.")

    except Exception:
        # Error is already printed in the get_pacifica_position function
        return

if __name__ == "__main__":
    main()

