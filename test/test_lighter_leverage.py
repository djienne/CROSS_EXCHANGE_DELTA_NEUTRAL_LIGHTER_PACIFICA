import sys
import os
# Add parent directory to path to allow imports from parent folder
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from typing import Dict, Optional

from dotenv import load_dotenv
import lighter
from lighter_client import BalanceFetchError, get_lighter_balance, get_lighter_market_details

POLL_INTERVAL_SECONDS = 1.0
LEVERAGE_UPDATE_TIMEOUT = 30.0


def margin_mode_from_env() -> str:
    mode = os.getenv("LIGHTER_MARGIN_MODE", "cross").lower()
    return "isolated" if mode.startswith("iso") else "cross"


def margin_mode_to_code(mode: str) -> int:
    return 0 if mode == "cross" else 1


def code_to_margin_mode(code: int) -> str:
    return "cross" if code == 0 else "isolated"


def derive_leverage(initial_margin_fraction: float) -> Optional[float]:
    if initial_margin_fraction <= 0:
        return None
    return round(100.0 / initial_margin_fraction, 4)


async def fetch_margin_snapshot(
    account_api: lighter.AccountApi,
    account_index: int,
    market_id: int,
) -> Optional[Dict[str, float]]:
    try:
        account_details = await account_api.account(by="index", value=str(account_index))
    except Exception as exc:
        print(f"Failed to fetch account info: {exc}")
        return None

    if not account_details or not account_details.accounts:
        return None

    acc = account_details.accounts[0]
    positions = getattr(acc, "positions", [])
    for pos in positions:
        if pos.market_id != market_id:
            continue

        try:
            imf = float(pos.initial_margin_fraction or "0")
        except Exception:
            imf = 0.0
        leverage = derive_leverage(imf)
        mode_code = int(getattr(pos, "margin_mode", 0))
        return {
            "initial_margin_fraction": imf,
            "leverage": leverage if leverage else 0.0,
            "margin_mode": mode_code,
        }

    return None


async def wait_for_leverage(
    account_api: lighter.AccountApi,
    account_index: int,
    market_id: int,
    expected_leverage: int,
    tolerance: float = 0.25,
) -> Optional[Dict[str, float]]:
    attempts = int(max(LEVERAGE_UPDATE_TIMEOUT / POLL_INTERVAL_SECONDS, 1))
    last_snapshot: Optional[Dict[str, float]] = None
    for _ in range(attempts):
        last_snapshot = await fetch_margin_snapshot(account_api, account_index, market_id)
        if last_snapshot and last_snapshot.get("leverage"):
            lev = last_snapshot["leverage"]
            if abs(lev - expected_leverage) <= tolerance:
                return last_snapshot
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    return last_snapshot


async def main():
    print("=" * 60)
    print("Lighter Leverage Update Test")
    print("=" * 60)

    lighter_signer = None
    lighter_api_client = None

    try:
        load_dotenv()

        api_key = os.getenv("API_KEY_PRIVATE_KEY")
        if not api_key:
            print("Missing API_KEY_PRIVATE_KEY in environment; aborting.")
            return

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

        symbol = (os.getenv("LIGHTER_LEVERAGE_SYMBOL") or "ASTER").upper()
        target_leverage = int(os.getenv("LIGHTER_LEVERAGE_TARGET", "5"))
        revert_after = os.getenv("LIGHTER_LEVERAGE_REVERT", "true").lower() != "false"

        # Initialise clients
        lighter_signer = lighter.SignerClient(
            url=base_url,
            private_key=api_key,
            account_index=account_index,
            api_key_index=api_key_index,
        )
        signer_check = lighter_signer.check_client()
        if signer_check:
            print(f"Signer validation failed: {signer_check}")
            return

        lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=base_url))
        account_api = lighter.AccountApi(lighter_api_client)
        order_api = lighter.OrderApi(lighter_api_client)

        # Fetch baseline balance
        try:
            available, portfolio_value = await get_lighter_balance(ws_url, account_index)
            print(f"Account Equity: ${portfolio_value:.2f} (available: ${available:.2f})")
        except BalanceFetchError as exc:
            print(f"Warning: could not fetch balance ({exc})")

        # Resolve market details
        market_id, _, _ = await get_lighter_market_details(order_api, symbol)
        margin_mode = margin_mode_from_env()

        print(f"\nTesting leverage updates for {symbol} (market_id={market_id})")
        print(f"Margin mode: {margin_mode} | Target leverage: {target_leverage}x")

        # Snapshot before change
        baseline = await fetch_margin_snapshot(account_api, account_index, market_id)
        if not baseline:
            print("Could not retrieve current leverage settings; aborting.")
            return

        original_leverage = int(round(baseline.get("leverage", 0))) or 3
        original_mode_code = baseline.get("margin_mode", 0)

        print(
            f"Current leverage ~{baseline.get('leverage', 0):.2f}x "
            f"(initial margin {baseline.get('initial_margin_fraction', 0):.2f}%, "
            f"mode={code_to_margin_mode(original_mode_code)})"
        )

        if abs(original_leverage - target_leverage) <= 0.25:
            target_leverage = min(20, original_leverage + 1)
            print(f"Adjusted target leverage to {target_leverage}x to ensure a change.")

        # Apply new leverage
        print(f"\nSetting leverage to {target_leverage}x …")
        _, _, err = await lighter_signer.update_leverage(
            market_index=market_id,
            margin_mode=margin_mode_to_code(margin_mode),
            leverage=target_leverage,
        )
        if err:
            print(f"Leverage update failed: {err}")
            return

        updated = await wait_for_leverage(account_api, account_index, market_id, target_leverage)
        if updated and abs(updated.get("leverage", 0) - target_leverage) <= 0.25:
            print(
                f"✓ Leverage updated: {updated.get('leverage', 0):.2f}x "
                f"(initial margin {updated.get('initial_margin_fraction', 0):.2f}%, "
                f"mode={code_to_margin_mode(updated.get('margin_mode', 0))})"
            )
        else:
            print("⚠️  Unable to confirm leverage change within timeout; latest snapshot:")
            print(f"    {updated}")

        # Optionally revert to original leverage
        if revert_after:
            print(f"\nReverting leverage to original setting ({original_leverage}x)…")
            _, _, revert_err = await lighter_signer.update_leverage(
                market_index=market_id,
                margin_mode=original_mode_code,
                leverage=original_leverage,
            )
            if revert_err:
                print(f"Revert failed: {revert_err}")
            else:
                reverted = await wait_for_leverage(
                    account_api, account_index, market_id, original_leverage
                )
                if reverted and abs(reverted.get("leverage", 0) - original_leverage) <= 0.25:
                    print(
                        f"✓ Reverted: {reverted.get('leverage', 0):.2f}x "
                        f"(initial margin {reverted.get('initial_margin_fraction', 0):.2f}%)"
                    )
                else:
                    print("⚠️  Unable to confirm revert; latest snapshot:")
                    print(f"    {reverted}")

    except Exception as exc:
        print(f"An unexpected error occurred: {exc}")
        import traceback

        traceback.print_exc()
    finally:
        if lighter_signer:
            try:
                await lighter_signer.close()
            except Exception:
                pass
        if lighter_api_client:
            try:
                await lighter_api_client.close()
            except Exception:
                pass

        print("\n" + "=" * 60)
        print("Leverage test script finished.")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
