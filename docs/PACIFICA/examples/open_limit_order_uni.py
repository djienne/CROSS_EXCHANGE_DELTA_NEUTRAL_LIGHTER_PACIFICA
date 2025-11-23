"""
Simple script to place, verify, and cancel limit orders on Pacifica DEX

Features:
- Dynamically fetches tick_size and lot_size for any market (with fallbacks)
- Places limit order with agent wallet authentication
- Verifies order was placed successfully
- Cancels order and verifies cancellation

Usage:
    python open_limit_order_uni.py                                    # Default: $20 buy UNI
    python open_limit_order_uni.py --sell                             # Sell order (short)
    python open_limit_order_uni.py --symbol SOL --notional 50        # $50 SOL order
    python open_limit_order_uni.py --symbol BTC --notional 100 --sell # $100 BTC short
"""
import asyncio
import json
import time
import uuid
import sys
import argparse
from pathlib import Path
from decimal import Decimal, ROUND_DOWN

import requests
import websockets
from solders.keypair import Keypair
from dotenv import load_dotenv
import os

# Make project packages importable when running this script directly
sys.path.insert(0, str(Path(__file__).parent))
from pacifica_sdk.common.constants import REST_URL, WS_URL
from pacifica_sdk.common.utils import sign_message


# Load environment variables
load_dotenv()
SOL_WALLET = os.getenv("SOL_WALLET")  # Main account
AGENT_WALLET_PUBLIC = os.getenv("API_PUBLIC")  # Agent wallet public key
AGENT_WALLET_PRIVATE = os.getenv("API_PRIVATE")  # Agent wallet private key

# Configuration
SYMBOL = "UNI"
NOTIONAL_USD = 20.0
SPREAD_PERCENT = 0.5  # 0.5% spread from mid price

# Fallback market specifications (if API fetch fails)
FALLBACK_MARKET_SPECS = {
    'BTC': {'tick_size': 0.1, 'lot_size': 0.001},
    'ETH': {'tick_size': 0.1, 'lot_size': 0.001},
    'SOL': {'tick_size': 0.01, 'lot_size': 0.1},
    'UNI': {'tick_size': 0.001, 'lot_size': 0.1},
    'WLFI': {'tick_size': 0.0001, 'lot_size': 0.1},
    'PAXG': {'tick_size': 0.01, 'lot_size': 0.001},
    'ASTER': {'tick_size': 0.0001, 'lot_size': 0.1},
    'BNB': {'tick_size': 0.1, 'lot_size': 0.001},
    'PACIFICA': {'tick_size': 0.0001, 'lot_size': 0.1},
}


async def get_current_price_ws(symbol, timeout=10):
    """Get current price via WebSocket"""
    uri = WS_URL

    try:
        async with websockets.connect(uri) as websocket:
            # Subscribe to orderbook to get price (more reliable than prices stream)
            subscribe_msg = {
                "method": "subscribe",
                "params": {
                    "source": "book",
                    "symbol": symbol,
                    "agg_level": 1
                }
            }
            await websocket.send(json.dumps(subscribe_msg))
            print(f"Subscribed to {symbol} orderbook via WebSocket")

            # Wait for orderbook update with timeout
            start_time = time.time()
            while True:
                if time.time() - start_time > timeout:
                    raise TimeoutError(f"Timeout waiting for {symbol} price data")

                try:
                    response = await asyncio.wait_for(websocket.recv(), timeout=5)
                    data = json.loads(response)

                    # Debug: print received message type
                    if 'channel' in data:
                        print(f"Received: channel={data.get('channel')}")

                    # Check if this is an orderbook update
                    if data.get("channel") == "book":
                        book_data = data.get("data", {})
                        symbol_received = book_data.get("s", "")

                        if symbol_received == symbol:
                            levels = book_data.get("l", [[], []])

                            if len(levels) >= 2 and levels[0] and levels[1]:
                                # Extract best bid and ask
                                best_bid = float(levels[0][0].get('p', 0))
                                best_ask = float(levels[1][0].get('p', 0))

                                if best_bid > 0 and best_ask > 0:
                                    mid_price = (best_bid + best_ask) / 2
                                    print(f"Current mid price for {symbol}: ${mid_price:.6f} (bid: ${best_bid:.6f}, ask: ${best_ask:.6f})")
                                    return mid_price
                except asyncio.TimeoutError:
                    continue
    except Exception as e:
        print(f"WebSocket error: {e}")
        raise


def get_market_specs(symbol):
    """
    Get market specifications (tick_size and lot_size) for a symbol.
    Tries to fetch from API via WebSocket, falls back to hardcoded values.

    Returns:
        dict: {'tick_size': float, 'lot_size': float}
    """
    try:
        # Try to fetch from REST API (may fail due to Cloudflare)
        api_url = f"{REST_URL}/markets"
        response = requests.get(api_url, timeout=5)

        if response.status_code == 200:
            data = response.json()
            markets = data.get('data', [])

            for market in markets:
                if market.get('symbol') == symbol:
                    tick_size = float(market.get('tick_size', 0.01))
                    lot_size = float(market.get('lot_size', 0.1))
                    print(f"[INFO] Fetched market specs from API: tick_size={tick_size}, lot_size={lot_size}")
                    return {'tick_size': tick_size, 'lot_size': lot_size}
    except Exception as e:
        print(f"[WARNING] Failed to fetch market specs from API: {e}")

    # Fallback to hardcoded values
    if symbol in FALLBACK_MARKET_SPECS:
        specs = FALLBACK_MARKET_SPECS[symbol]
        print(f"[INFO] Using fallback market specs: tick_size={specs['tick_size']}, lot_size={specs['lot_size']}")
        return specs
    else:
        # Default fallback
        default_specs = {'tick_size': 0.01, 'lot_size': 0.1}
        print(f"[WARNING] No specs found for {symbol}, using defaults: tick_size={default_specs['tick_size']}, lot_size={default_specs['lot_size']}")
        return default_specs


def get_agent_keypair():
    """Get agent wallet keypair with validation"""
    if not SOL_WALLET:
        raise ValueError("SOL_WALLET not found in .env file")
    if not AGENT_WALLET_PRIVATE:
        raise ValueError("API_PRIVATE (agent wallet private key) not found in .env file")
    if not AGENT_WALLET_PUBLIC:
        raise ValueError("API_PUBLIC (agent wallet public key) not found in .env file")

    if len(AGENT_WALLET_PRIVATE) < 80:
        raise ValueError(
            f"API_PRIVATE appears to be invalid (length: {len(AGENT_WALLET_PRIVATE)}). "
            "You need a full Solana private key (base58 encoded, ~88 characters)."
        )

    return Keypair.from_base58_string(AGENT_WALLET_PRIVATE)


def place_limit_order(symbol, price, amount, side="bid"):
    """Place a limit order using REST API with agent wallet authentication"""

    agent_keypair = get_agent_keypair()

    # Scaffold the signature header
    timestamp = int(time.time() * 1_000)

    signature_header = {
        "timestamp": timestamp,
        "expiry_window": 5_000,
        "type": "create_order",
    }

    # Construct the signature payload
    client_order_id = str(uuid.uuid4())
    signature_payload = {
        "symbol": symbol,
        "price": str(price),
        "reduce_only": False,
        "amount": str(amount),
        "side": side,
        "tif": "GTC",  # Good Till Cancel
        "client_order_id": client_order_id,
    }

    # Sign the message with agent wallet
    message, signature = sign_message(signature_header, signature_payload, agent_keypair)

    # Construct the request header with agent wallet
    request_header = {
        "account": SOL_WALLET,  # Main account
        "agent_wallet": AGENT_WALLET_PUBLIC,  # Agent wallet public key
        "signature": signature,
        "timestamp": signature_header["timestamp"],
        "expiry_window": signature_header["expiry_window"],
    }

    # Send the request
    headers = {"Content-Type": "application/json"}

    request = {
        **request_header,
        **signature_payload,
    }

    api_url = f"{REST_URL}/orders/create"
    response = requests.post(api_url, json=request, headers=headers)

    print(f"\n--- Order Placement Response ---")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")

    if response.status_code == 200:
        print(f"\n[SUCCESS] Successfully placed limit {side} order for {amount} {symbol} at ${price}")
        print(f"Client Order ID: {client_order_id}")
        return response, client_order_id
    else:
        print(f"\n[FAILED] Failed to place order")
        return response, None


def get_open_orders(account):
    """Get all open orders for an account"""
    api_url = f"{REST_URL}/orders"
    params = {"account": account}

    response = requests.get(api_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if data.get("success"):
            return data.get("data", [])

    return []


def cancel_order(symbol, client_order_id):
    """Cancel an order by client_order_id"""

    agent_keypair = get_agent_keypair()

    # Scaffold the signature header
    timestamp = int(time.time() * 1_000)

    signature_header = {
        "timestamp": timestamp,
        "expiry_window": 5_000,
        "type": "cancel_order",
    }

    # Construct the signature payload
    signature_payload = {
        "symbol": symbol,
        "client_order_id": client_order_id,
    }

    # Sign the message with agent wallet
    message, signature = sign_message(signature_header, signature_payload, agent_keypair)

    # Construct the request
    request_header = {
        "account": SOL_WALLET,
        "agent_wallet": AGENT_WALLET_PUBLIC,
        "signature": signature,
        "timestamp": signature_header["timestamp"],
        "expiry_window": signature_header["expiry_window"],
    }

    headers = {"Content-Type": "application/json"}

    request = {
        **request_header,
        **signature_payload,
    }

    api_url = f"{REST_URL}/orders/cancel"
    response = requests.post(api_url, json=request, headers=headers)

    print(f"\n--- Order Cancellation Response ---")
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")

    if response.status_code == 200:
        print(f"\n[SUCCESS] Successfully cancelled order {client_order_id}")
    else:
        print(f"\n[FAILED] Failed to cancel order")

    return response


async def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Test order lifecycle: place, verify, cancel, verify cancellation')
    parser.add_argument('--sell', action='store_true', help='Place sell order (ask) instead of buy order (bid)')
    parser.add_argument('--symbol', type=str, default=SYMBOL, help=f'Trading symbol (default: {SYMBOL})')
    parser.add_argument('--notional', type=float, default=NOTIONAL_USD, help=f'Order size in USD (default: ${NOTIONAL_USD})')
    args = parser.parse_args()

    # Use command line arguments
    symbol = args.symbol.upper()
    notional = args.notional

    # Determine side and price adjustment
    if args.sell:
        side = "ask"
        side_name = "Sell (Short)"
        price_direction = "above"
        price_multiplier = 1 + SPREAD_PERCENT / 100  # 0.5% above mid
    else:
        side = "bid"
        side_name = "Buy (Long)"
        price_direction = "below"
        price_multiplier = 1 - SPREAD_PERCENT / 100  # 0.5% below mid

    print(f"=== Opening ${notional} Limit {side_name} Order on {symbol} ===\n")

    # Step 1: Get market specifications
    print("Step 1: Fetching market specifications...")
    market_specs = get_market_specs(symbol)
    tick_size = market_specs['tick_size']
    lot_size = market_specs['lot_size']

    # Step 2: Get current price via WebSocket
    print("\nStep 2: Fetching current price via WebSocket...")
    mid_price = await get_current_price_ws(symbol)

    # Step 3: Calculate limit price using Decimal for precision
    mid_price_dec = Decimal(str(mid_price))
    price_multiplier_dec = Decimal(str(price_multiplier))
    tick_size_dec = Decimal(str(tick_size))
    lot_size_dec = Decimal(str(lot_size))
    notional_dec = Decimal(str(notional))

    limit_price_raw = mid_price_dec * price_multiplier_dec

    # Round to tick size using Decimal
    limit_price_dec = (limit_price_raw / tick_size_dec).quantize(Decimal('1'), rounding=ROUND_DOWN) * tick_size_dec

    # Determine decimal places for display
    tick_decimals = len(str(tick_size).split('.')[1]) if '.' in str(tick_size) else 0
    print(f"\nStep 3: Calculated limit price: ${float(limit_price_dec):.{tick_decimals}f} ({SPREAD_PERCENT}% {price_direction} mid, rounded to tick size {tick_size})")

    # Step 4: Calculate amount based on notional using Decimal
    amount_raw = notional_dec / limit_price_dec

    # Round to lot size using Decimal
    amount_dec = (amount_raw / lot_size_dec).quantize(Decimal('1'), rounding=ROUND_DOWN) * lot_size_dec
    actual_notional_dec = amount_dec * limit_price_dec

    # Check minimum notional (usually $10 on Pacifica)
    min_notional = Decimal('10')
    if actual_notional_dec < min_notional and amount_dec > 0:
        print(f"[WARNING] Notional ${float(actual_notional_dec):.2f} is below minimum $10, increasing amount...")
        amount_dec = (min_notional / limit_price_dec / lot_size_dec).quantize(Decimal('1'), rounding=ROUND_DOWN) * lot_size_dec
        actual_notional_dec = amount_dec * limit_price_dec

    # Determine decimal places for amount display
    lot_decimals = len(str(lot_size).split('.')[1]) if '.' in str(lot_size) else 0
    print(f"Step 4: Calculated amount: {float(amount_dec):.{lot_decimals}f} {symbol} (${float(actual_notional_dec):.2f} notional, rounded to lot size {lot_size})")

    # Convert to float for API call
    limit_price = float(limit_price_dec)
    amount = float(amount_dec)

    # Step 5: Place the limit order
    print(f"\nStep 5: Placing limit {side} order...")
    response, client_order_id = place_limit_order(
        symbol=symbol,
        price=limit_price,  # Already rounded to tick size
        amount=amount,  # Already rounded to lot size
        side=side
    )

    if not client_order_id:
        print("\n[ERROR] Failed to place order, exiting...")
        return

    # Step 6: Wait a bit for the order to be registered
    print(f"\nStep 6: Waiting 2 seconds for order to be registered...")
    await asyncio.sleep(2)

    # Step 7: Check open orders to find our order
    print(f"\nStep 7: Fetching open orders to verify our order exists...")
    open_orders = get_open_orders(SOL_WALLET)

    if not open_orders:
        print("[WARNING] No open orders found")
    else:
        print(f"[INFO] Found {len(open_orders)} open order(s):")
        order_found = False
        for order in open_orders:
            is_our_order = order.get("client_order_id") == client_order_id
            marker = "  >>> " if is_our_order else "      "
            print(f"{marker}Order ID: {order.get('order_id')}, Client ID: {order.get('client_order_id')}, "
                  f"{order.get('side')} {order.get('initial_amount')} {order.get('symbol')} @ ${order.get('price')}")
            if is_our_order:
                order_found = True

        if order_found:
            print(f"\n[SUCCESS] Our order (client_order_id: {client_order_id}) was found in open orders!")
        else:
            print(f"\n[WARNING] Our order (client_order_id: {client_order_id}) was NOT found in open orders")

    # Step 8: Cancel the order
    print(f"\nStep 8: Cancelling the order...")
    cancel_response = cancel_order(symbol, client_order_id)

    # Step 9: Wait a bit and verify cancellation
    print(f"\nStep 9: Waiting 2 seconds and verifying order was cancelled...")
    await asyncio.sleep(2)

    open_orders_after = get_open_orders(SOL_WALLET)

    if not open_orders_after:
        print("[SUCCESS] No open orders remaining - order successfully cancelled!")
    else:
        print(f"[INFO] Found {len(open_orders_after)} open order(s) after cancellation:")
        order_still_exists = False
        for order in open_orders_after:
            is_our_order = order.get("client_order_id") == client_order_id
            marker = "  >>> " if is_our_order else "      "
            print(f"{marker}Order ID: {order.get('order_id')}, Client ID: {order.get('client_order_id')}, "
                  f"{order.get('side')} {order.get('initial_amount')} {order.get('symbol')} @ ${order.get('price')}")
            if is_our_order:
                order_still_exists = True

        if not order_still_exists:
            print(f"\n[SUCCESS] Our order (client_order_id: {client_order_id}) was successfully cancelled!")
        else:
            print(f"\n[WARNING] Our order (client_order_id: {client_order_id}) is still in open orders - cancellation may have failed")

    print(f"\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
