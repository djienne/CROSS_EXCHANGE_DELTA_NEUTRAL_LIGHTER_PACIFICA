import sys
import os
# Add parent directory to path to allow imports from parent folder
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

"""
Test script to place market orders on Lighter (open and close positions).
"""
import asyncio
from typing import Optional
from dotenv import load_dotenv
import lighter
from lighter_client import (
    BalanceFetchError,
    get_lighter_balance,
    get_lighter_best_bid_ask,
    get_lighter_market_details,
    get_lighter_position_details,
    lighter_close_position,
    lighter_place_aggressive_order,
)

POLL_INTERVAL_SECONDS = 1.0
POSITION_TIMEOUT_SECONDS = 30.0
CLOSE_TIMEOUT_SECONDS = 30.0


async def wait_for_position(
    account_api,
    account_index: int,
    market_id: int,
    amount_tick: float,
) -> Optional[dict]:
    """Poll until a position is visible (size above the minimum tick)."""
    attempts = int(max(POSITION_TIMEOUT_SECONDS / POLL_INTERVAL_SECONDS, 1))
    for attempt in range(attempts):
        details = await get_lighter_position_details(account_api, account_index, market_id)
        if details and details["abs_size"] >= amount_tick:
            return details
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    return None


async def wait_for_flat(
    account_api,
    account_index: int,
    market_id: int,
    amount_tick: float,
) -> bool:
    """Poll until the position size drops below the minimum tick."""
    attempts = int(max(CLOSE_TIMEOUT_SECONDS / POLL_INTERVAL_SECONDS, 1))
    for attempt in range(attempts):
        details = await get_lighter_position_details(account_api, account_index, market_id)
        if not details or details["abs_size"] < amount_tick:
            return True
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    return False


async def run_trade_flow() -> None:
    print("=" * 60)
    print("Lighter Market Order & Position Test")
    print("=" * 60)

    lighter_signer = None
    lighter_api_client = None

    try:
        load_dotenv()
        api_key = os.getenv("API_KEY_PRIVATE_KEY")
        account_index = int(os.getenv("ACCOUNT_INDEX", "0"))
        api_key_index = int(os.getenv("API_KEY_INDEX", "0"))
        ws_url = (
            os.getenv("LIGHTER_WS_URL")
            or os.getenv("WEBSOCKET_URL")
            or "wss://mainnet.zklighter.elliot.ai/stream"
        )
        base_url = (
            os.getenv("LIGHTER_BASE_URL")
            or os.getenv("BASE_URL")
            or "https://mainnet.zklighter.elliot.ai"
        )

        if not api_key:
            print("Missing required environment variable: API_KEY_PRIVATE_KEY. Aborting.")
            return

        lighter_signer = lighter.SignerClient(
            url=base_url,
            private_key=api_key,
            account_index=account_index,
            api_key_index=api_key_index,
        )
        signer_check = lighter_signer.check_client()
        if signer_check:
            print(f"Lighter signer validation failed: {signer_check}")
            return

        lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=base_url))
        account_api = lighter.AccountApi(lighter_api_client)
        order_api = lighter.OrderApi(lighter_api_client)

        symbols_env = os.getenv("LIGHTER_TEST_SYMBOLS")
        if symbols_env:
            coins_to_trade = [
                sym.strip().upper() for sym in symbols_env.split(",") if sym.strip()
            ]
        else:
            coins_to_trade = ["ASTER"]

        if not coins_to_trade:
            print("No symbols configured for testing. Set LIGHTER_TEST_SYMBOLS env var.")
            return

        notional_per_trade = float(os.getenv("LIGHTER_TEST_NOTIONAL", "50"))

        print("\n=== Initial Account State ===")
        try:
            available_balance, portfolio_value = await get_lighter_balance(ws_url, account_index)
            print(f"Total Equity: ${portfolio_value:.2f}")
            print(f"Available Balance: ${available_balance:.2f}")
        except BalanceFetchError as e:
            print(f"Error fetching balance: {e}")
            return

        equity_start = portfolio_value
        open_positions = {}

        # --- Open Positions ---
        print("\n=== Opening Market Positions ===")
        for coin in coins_to_trade:
            try:
                market_id, price_tick, amount_tick = await get_lighter_market_details(order_api, coin)
                bid, ask = await get_lighter_best_bid_ask(order_api, coin, market_id)

                if not bid or not ask:
                    print(f"{coin}: Skipping - could not retrieve bid/ask.")
                    continue

                quantity = notional_per_trade / ask
                rounded_quantity = max(amount_tick, round(quantity / amount_tick) * amount_tick)

                print(f"\n{coin}:")
                print(f"  Ask Price: ${ask:.2f}")
                print(f"  Target Notional: ${notional_per_trade:.2f}")
                print(f"  Quantity (raw): {quantity:.6f}")
                print(f"  Quantity (rounded): {rounded_quantity:.6f}")

                result = await lighter_place_aggressive_order(
                    lighter_signer,
                    market_id=market_id,
                    price_tick=price_tick,
                    amount_tick=amount_tick,
                    side="buy",
                    size_base=quantity,
                    ref_price=ask,
                    cross_ticks=100,
                )

                if not result:
                    print("  [ERROR] Order placement failed")
                    continue

                print("  [OK] Order placed successfully, waiting for fill...")

                details = await wait_for_position(account_api, account_index, market_id, amount_tick)
                if not details:
                    print("  [WARNING] Position not visible after timeout. Please verify manually.")
                    continue

                side = details["side"]
                entry_price = details["entry_price"]
                unrealized = details["unrealized_pnl"]
                size = abs(details["size"])
                print(f"  [POSITION] Side: {side}, Size: {size:.6f}, Entry: ${entry_price:.4f}, Unrealized PnL: ${unrealized:+.2f}")

                open_positions[coin] = {
                    "market_id": market_id,
                    "price_tick": price_tick,
                    "amount_tick": amount_tick,
                    "entry_snapshot": details,
                }

            except Exception as e:
                print(f"  [ERROR] Failed to open position for {coin}: {e}")
                import traceback
                traceback.print_exc()

        if not open_positions:
            print("\n[WARNING] No positions detected; aborting before close to avoid stray orders.")
            return

        await asyncio.sleep(2)

        # --- Close Positions ---
        print("\n=== Closing Market Positions ===")
        for coin in coins_to_trade:
            try:
                if coin not in open_positions:
                    print(f"{coin}: Skipping close - no tracked open position.")
                    continue

                meta = open_positions[coin]
                market_id = meta["market_id"]
                price_tick = meta["price_tick"]
                amount_tick = meta["amount_tick"]

                details = await get_lighter_position_details(account_api, account_index, market_id)
                if not details or details["abs_size"] < amount_tick:
                    print(f"{coin}: Position already flat.")
                    continue

                bid, ask = await get_lighter_best_bid_ask(order_api, coin, market_id)
                close_side = "sell" if details["size"] > 0 else "buy"
                ref_price = bid if close_side == "sell" else ask

                if not ref_price:
                    print(f"{coin}: Missing reference price for close. Skipping.")
                    continue

                print(f"\n{coin}: Closing {details['abs_size']:.6f} ({close_side})")

                result = await lighter_close_position(
                    lighter_signer,
                    market_id=market_id,
                    price_tick=price_tick,
                    amount_tick=amount_tick,
                    side=close_side,
                    size_base=details["abs_size"],
                    ref_price=ref_price,
                    cross_ticks=100,
                )

                if not result:
                    print("  [ERROR] Close order failed to submit.")
                    continue

                closed = await wait_for_flat(account_api, account_index, market_id, amount_tick)
                if closed:
                    print("  [OK] Position fully closed.")
                else:
                    print("  [WARNING] Position still open after timeout. Please verify manually.")

            except Exception as e:
                print(f"  [ERROR] Failed to close position for {coin}: {e}")

        # --- Final Account State ---
        print("\n=== Final Account State ===")
        try:
            final_available, final_equity = await get_lighter_balance(ws_url, account_index)
            print(f"Total Equity: ${final_equity:.2f}")
            print(f"Available Balance: ${final_available:.2f}")
            print(f"Change in Equity: ${final_equity - equity_start:+.2f}")
        except BalanceFetchError as e:
            print(f"Error fetching final balance: {e}")

        # --- Verify All Positions Closed ---
        print("\n=== Verifying Positions Closed ===")
        for coin in coins_to_trade:
            try:
                meta = open_positions.get(coin)
                if not meta:
                    print(f"{coin}: [INFO] No position was tracked for verification.")
                    continue

                details = await get_lighter_position_details(account_api, account_index, meta["market_id"])
                if not details or details["abs_size"] < meta["amount_tick"]:
                    print(f"{coin}: [OK] Flat.")
                else:
                    print(f"{coin}: [WARNING] Residual size {details['size']:.6f} remains.")
            except Exception as e:
                print(f"{coin}: [ERROR] Verification failed: {e}")

    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if lighter_signer is not None:
            try:
                await lighter_signer.close()
            except Exception:
                pass
        if lighter_api_client is not None:
            try:
                await lighter_api_client.close()
            except Exception:
                pass
        print("\n" + "=" * 60)
        print("Trade-flow test script finished.")
        print("=" * 60)


async def main() -> None:
    await run_trade_flow()


if __name__ == "__main__":
    asyncio.run(main())
