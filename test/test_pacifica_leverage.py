import sys
import os
# Add parent directory to path to allow imports from parent folder
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from dotenv import load_dotenv
from pacifica_client import PacificaClient

async def main():
    print("=" * 60)
    print("Pacifica Leverage Test")
    print("=" * 60)

    try:
        # --- Load Environment and Initialize Connector ---
        load_dotenv()
        sol_wallet = os.environ.get("SOL_WALLET")
        api_public = os.environ.get("API_PUBLIC")
        api_private = os.environ.get("API_PRIVATE")

        if not all([sol_wallet, api_public, api_private]):
            print("Missing required environment variables in .env file. Aborting.")
            return

        client = PacificaClient(sol_wallet, api_public, api_private)

        # --- Define Test Logic ---
        coins_to_test = ["ETH", "BTC", "DOGE"]

        print("\n=== Test 1: Getting Max Leverage for Symbols ===")
        for coin in coins_to_test:
            try:
                max_leverage = client.get_max_leverage(coin)
                print(f"{coin}: Max leverage = {max_leverage}x")
            except Exception as e:
                print(f"Failed to get max leverage for {coin}: {e}")

        print("\n=== Test 2: Setting Leverage to 5x ===")
        target_leverage = 5
        for coin in coins_to_test:
            try:
                print(f"\n{coin}: Setting leverage to {target_leverage}x...")
                success = client.set_leverage(coin, target_leverage)
                if success:
                    print(f"{coin}: [OK] Leverage set successfully")
                else:
                    print(f"{coin}: [FAILED] Could not set leverage")
            except Exception as e:
                print(f"{coin}: [ERROR] {e}")

        print("\n=== Test 3: Waiting 2 seconds for leverage to update ===")
        import time
        time.sleep(2)

        print("\n=== Test 2: Checking Current Positions (Leverage is per-position) ===")
        for coin in coins_to_test:
            try:
                position = await client.get_position(coin)
                qty = position.get('qty', 0)
                entry_price = position.get('entry_price', 0)
                notional = position.get('notional', 0)

                if qty != 0 and entry_price > 0:
                    # Estimate effective leverage if we know account equity
                    equity = client.get_equity()
                    if equity > 0 and notional > 0:
                        effective_leverage = notional / equity
                        print(f"{coin}: Position found - Qty: {qty}, Entry: ${entry_price:.4f}, Notional: ${notional:.2f}")
                        print(f"       Estimated effective leverage: {effective_leverage:.2f}x (based on equity: ${equity:.2f})")
                    else:
                        print(f"{coin}: Position found - Qty: {qty}, Entry: ${entry_price:.4f}, Notional: ${notional:.2f}")
                        print(f"       Cannot calculate effective leverage (equity or notional is 0)")
                else:
                    print(f"{coin}: No open position")
            except Exception as e:
                print(f"Failed to get position for {coin}: {e}")

        print("\n=== Test 4: Verifying Leverage Was Set ===")
        print("Note: Leverage settings apply to new positions.")
        print("If you have existing positions, the leverage set might differ from the actual effective leverage.")
        print("\nLeverage on Pacifica works as follows:")
        print("  1. You can set a leverage limit per symbol (what we just did)")
        print("  2. When you open a position, it uses the set leverage")
        print("  3. Effective leverage = Notional Position Value / Account Equity")
        print("  4. For existing positions, you can only INCREASE leverage, not decrease")

        print("\n=== Test 5: Account Information ===")
        try:
            equity = client.get_equity()
            available = client.get_available_balance()
            print(f"Total Equity: ${equity:.2f}")
            print(f"Available Balance: ${available:.2f}")
            print(f"Used Margin: ${equity - available:.2f}")
            if equity > 0 and available > 0:
                margin_usage_pct = ((equity - available) / equity) * 100
                print(f"Margin Usage: {margin_usage_pct:.2f}%")
        except Exception as e:
            print(f"Failed to get account info: {e}")

    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n" + "=" * 60)
        print("Leverage test script finished.")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
