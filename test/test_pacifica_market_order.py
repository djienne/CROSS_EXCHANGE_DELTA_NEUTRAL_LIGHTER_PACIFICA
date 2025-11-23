import sys
import os
# Add parent directory to path to allow imports from parent folder
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


import asyncio
import time
from dotenv import load_dotenv
from pacifica_client import PacificaClient

async def main():
    print("=" * 60)
    print("Pacifica Market Order Test")
    print("=" * 60)

    try:
        # --- Load Environment and Initialize Client ---
        load_dotenv()
        sol_wallet = os.environ.get("SOL_WALLET")
        api_public = os.environ.get("API_PUBLIC")
        api_private = os.environ.get("API_PRIVATE")

        if not all([sol_wallet, api_public, api_private]):
            print("Missing required environment variables in .env file. Aborting.")
            return

        client = PacificaClient(sol_wallet, api_public, api_private)

        # --- Define Trading Logic ---
        coins_to_trade = ["ETH", "BTC"]
        notional_per_trade = 20.0

        print("\n=== Initial Account State ===")
        equity = client.get_equity()
        available = client.get_available_balance()
        print(f"Total Equity: ${equity:.2f}")
        print(f"Available Balance: ${available:.2f}")

        # --- Open Positions ---
        print("\n=== Opening Market Positions ===")
        order_ids = []

        for coin in coins_to_trade:
            try:
                # Get current price to calculate quantity
                mark_price = await client.get_mark_price(coin)
                if mark_price <= 0:
                    print(f"Could not get valid price for {coin}. Skipping.")
                    continue

                # Calculate quantity from notional
                quantity = notional_per_trade / mark_price
                rounded_qty = client.round_quantity(quantity, coin)

                print(f"\n{coin}:")
                print(f"  Price: ${mark_price:.2f}")
                print(f"  Target Notional: ${notional_per_trade:.2f}")
                print(f"  Quantity: {rounded_qty:.4f}")

                # Place market buy order
                order_id = client.place_market_order(
                    symbol=coin,
                    side='buy',
                    quantity=rounded_qty
                )
                order_ids.append((coin, order_id))
                print(f"  [OK] Order placed: {order_id}")

            except Exception as e:
                print(f"  [ERROR] Failed to open position for {coin}: {e}")

        # --- Wait ---
        wait_time = 5
        print(f"\n=== Waiting for {wait_time} seconds ===")
        time.sleep(wait_time)

        # --- Check Positions ---
        print("\n=== Checking Open Positions ===")
        for coin in coins_to_trade:
            try:
                position = await client.get_position(coin)
                qty = position.get('qty', 0)
                entry_price = position.get('entry_price', 0)
                unrealized_pnl = position.get('unrealized_pnl', 0)
                notional = position.get('notional', 0)

                if qty != 0:
                    print(f"\n{coin}:")
                    print(f"  Quantity: {qty:.4f}")
                    print(f"  Entry Price: ${entry_price:.4f}")
                    print(f"  Notional: ${notional:.2f}")
                    print(f"  Unrealized PnL: ${unrealized_pnl:+.2f}")
                else:
                    print(f"\n{coin}: No position")

            except Exception as e:
                print(f"  ✗ Failed to get position for {coin}: {e}")

        # --- Close Positions ---
        print("\n=== Closing Market Positions ===")
        for coin in coins_to_trade:
            try:
                position = await client.get_position(coin)
                qty = position.get('qty', 0)

                if qty == 0:
                    print(f"{coin}: No position to close")
                    continue

                # Close position with market order (reduce only)
                close_qty = abs(qty)
                close_side = 'sell' if qty > 0 else 'buy'

                print(f"\n{coin}:")
                print(f"  Closing {close_qty:.4f} {coin} ({close_side})")

                order_id = client.place_market_order(
                    symbol=coin,
                    side=close_side,
                    quantity=close_qty,
                    reduce_only=True
                )
                print(f"  [OK] Close order placed: {order_id}")

            except Exception as e:
                print(f"  [ERROR] Failed to close position for {coin}: {e}")

        # --- Wait for closes to execute ---
        print("\n=== Waiting 3 seconds for closes to execute ===")
        time.sleep(3)

        # --- Final Account State ---
        print("\n=== Final Account State ===")
        final_equity = client.get_equity()
        final_available = client.get_available_balance()
        print(f"Total Equity: ${final_equity:.2f}")
        print(f"Available Balance: ${final_available:.2f}")
        print(f"Change in Equity: ${final_equity - equity:+.2f}")

        # --- Verify All Positions Closed ---
        print("\n=== Verifying Positions Closed ===")
        all_closed = True
        for coin in coins_to_trade:
            try:
                position = await client.get_position(coin)
                qty = position.get('qty', 0)
                if qty != 0:
                    print(f"{coin}: WARNING - Still has position of {qty:.4f}")
                    all_closed = False
                else:
                    print(f"{coin}: [OK] Closed")
            except Exception as e:
                print(f"{coin}: Error checking: {e}")

        if all_closed:
            print("\n[SUCCESS] All positions successfully closed")
        else:
            print("\n[WARNING] Some positions may still be open")

    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n" + "=" * 60)
        print("Market order test script finished.")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
