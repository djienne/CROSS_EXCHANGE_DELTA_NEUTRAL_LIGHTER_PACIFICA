"""
Dynamic spread order placement using Avellaneda-Stoikov model

Uses calibrated parameters from params/spread_parameters_{SYMBOL}.json
Calculates optimal bid/ask spreads based on:
- Current volatility
- Inventory position (auto-tracked from params/current_position.json)
- Microprice signal
- Adverse selection costs
- Fill probability (k, A parameters)

Configuration loaded from params/trading_config.json

Usage:
    python open_dynamic_spread_order.py                        # Uses trading_config.json
    python open_dynamic_spread_order.py --sell                 # Sell order (short)
    python open_dynamic_spread_order.py --override-symbol SOL  # Override config symbol
"""
import asyncio
import json
import time
import uuid
import sys
import argparse
from pathlib import Path
from decimal import Decimal, ROUND_DOWN
import numpy as np

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

# Default paths
TRADING_CONFIG_PATH = Path("params") / "trading_config.json"


def load_trading_config():
    """Load trading configuration from JSON file"""
    if not TRADING_CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Trading config file not found: {TRADING_CONFIG_PATH}\n"
            f"Please create this file with symbol and notional_amount parameters"
        )

    with open(TRADING_CONFIG_PATH, 'r') as f:
        config = json.load(f)

    print(f"[INFO] Loaded trading config from {TRADING_CONFIG_PATH}")
    print(f"  Symbol: {config.get('symbol', 'N/A')}")
    print(f"  Notional Amount: ${config.get('notional_amount', 'N/A')}")

    return config


def load_spread_parameters(symbol):
    """Load calibrated spread parameters from JSON file"""
    params_file = Path("params") / f"spread_parameters_{symbol}.json"

    if not params_file.exists():
        raise FileNotFoundError(
            f"Spread parameters file not found: {params_file}\n"
            f"Please run: python spread_calculator.py --symbol {symbol}"
        )

    with open(params_file, 'r') as f:
        params = json.load(f)

    print(f"[INFO] Loaded spread parameters for {symbol} from {params_file}")
    print(f"  gamma (Risk Aversion): {params['gamma']:.4e}")
    print(f"  tau (Time Horizon): {params['tau']:.4f}s")
    print(f"  beta_alpha (Microprice Mult): {params['beta_alpha']:.4f}")

    return params


def get_market_specs(symbol, params):
    """Get market specifications from params or API fallback"""
    # Use tick_size from params, but clean up floating-point artifacts
    tick_size_raw = params.get('tick_size', 0.001)

    # Round tick_size to clean value (handles floating-point artifacts like 0.0009999999999994458)
    from decimal import Decimal, ROUND_HALF_UP
    tick_size = float(Decimal(str(tick_size_raw)).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP))

    # Estimate lot_size (not in params, use conservative default)
    lot_size_map = {
        'BTC': 0.001,
        'ETH': 0.001,
        'SOL': 0.1,
        'UNI': 0.1,
        'BNB': 0.001,
    }
    lot_size = lot_size_map.get(symbol, 0.1)

    print(f"[INFO] Market specs: tick_size={tick_size}, lot_size={lot_size}")

    return {'tick_size': tick_size, 'lot_size': lot_size}


async def get_current_orderbook_ws(symbol, timeout=10):
    """Get current orderbook via WebSocket to calculate microprice"""
    uri = WS_URL

    try:
        async with websockets.connect(uri) as websocket:
            # Subscribe to orderbook
            subscribe_msg = {
                "method": "subscribe",
                "params": {
                    "source": "book",
                    "symbol": symbol,
                    "agg_level": 1
                }
            }
            await websocket.send(json.dumps(subscribe_msg))
            print(f"[INFO] Subscribed to {symbol} orderbook via WebSocket")

            # Wait for orderbook update with timeout
            start_time = time.time()
            while True:
                if time.time() - start_time > timeout:
                    raise TimeoutError(f"Timeout waiting for {symbol} orderbook data")

                try:
                    response = await asyncio.wait_for(websocket.recv(), timeout=5)
                    data = json.loads(response)

                    # Check if this is an orderbook update
                    if data.get("channel") == "book":
                        book_data = data.get("data", {})
                        symbol_received = book_data.get("s", "")

                        if symbol_received == symbol:
                            levels = book_data.get("l", [[], []])

                            if len(levels) >= 2 and levels[0] and levels[1]:
                                # Extract top 3 levels for microprice calculation
                                bids = []
                                asks = []

                                for i in range(min(3, len(levels[0]))):
                                    bid = levels[0][i]
                                    bids.append({'price': float(bid.get('p', 0)), 'qty': float(bid.get('a', 0))})

                                for i in range(min(3, len(levels[1]))):
                                    ask = levels[1][i]
                                    asks.append({'price': float(ask.get('p', 0)), 'qty': float(ask.get('a', 0))})

                                if bids and asks:
                                    # Calculate mid price
                                    best_bid = bids[0]['price']
                                    best_ask = asks[0]['price']
                                    mid_price = (best_bid + best_ask) / 2

                                    # Calculate microprice (weighted by volumes)
                                    numerator = 0
                                    denominator = 0
                                    for i in range(len(bids)):
                                        if i < len(asks):
                                            numerator += asks[i]['price'] * bids[i]['qty'] + bids[i]['price'] * asks[i]['qty']
                                            denominator += bids[i]['qty'] + asks[i]['qty']

                                    microprice = numerator / denominator if denominator > 0 else mid_price
                                    micro_deviation = microprice - mid_price

                                    print(f"[INFO] Mid: ${mid_price:.6f}, Microprice: ${microprice:.6f}, Deviation: ${micro_deviation:.6f}")

                                    return {
                                        'mid_price': mid_price,
                                        'microprice': microprice,
                                        'micro_deviation': micro_deviation,
                                        'best_bid': best_bid,
                                        'best_ask': best_ask,
                                        'bids': bids,
                                        'asks': asks
                                    }
                except asyncio.TimeoutError:
                    continue
    except Exception as e:
        print(f"[ERROR] WebSocket error: {e}")
        raise


def calculate_optimal_quotes(market_data, params, inventory):
    """
    Calculate optimal bid/ask quotes using Avellaneda-Stoikov model

    Args:
        market_data: dict with mid_price, micro_deviation
        params: calibrated parameters from JSON
        inventory: current inventory position

    Returns:
        dict with bid_price, ask_price, reservation_price, half_spread, etc.
    """
    # Extract parameters
    gamma = params['gamma']
    tau = params['tau']
    beta_alpha = params['beta_alpha']
    k_bid = params['k_bid']
    k_ask = params['k_ask']
    c_AS_bid = params['c_AS_bid']
    c_AS_ask = params['c_AS_ask']
    tick_size = params['tick_size']
    maker_fee_bps = params['maker_fee_bps']

    # Use latest calibrated volatility from params
    sigma = params['latest_volatility']

    # Extract market data
    mid_price = market_data['mid_price']
    micro_deviation = market_data['micro_deviation']

    # Calculate k_mid and k_price_based
    k_mid = (k_bid + k_ask) / 2
    k_mid_price_based = k_mid * mid_price / 10000

    # 1. Reservation price (with inventory skew)
    reservation_price = mid_price + (beta_alpha * micro_deviation) - (inventory * gamma * (sigma**2) * tau)

    # 2. Optimal half-spread (phi)
    phi_inventory = (1 / gamma) * np.log(1 + gamma / k_mid_price_based) if gamma > 0 and k_mid_price_based > 0 else 0.01
    phi_adverse_sel = 0.5 * gamma * (sigma**2) * tau
    phi_adverse_cost = (c_AS_bid + c_AS_ask) / 2
    phi_fee = maker_fee_bps / 10000 * mid_price

    half_spread = max(phi_inventory + phi_adverse_sel + phi_adverse_cost + phi_fee, tick_size)

    # 3. Asymmetric adjustments
    c_AS_mid = (c_AS_bid + c_AS_ask) / 2
    delta_bid_as = c_AS_bid - c_AS_mid
    delta_ask_as = c_AS_ask - c_AS_mid

    # 4. Final quotes
    bid_price = reservation_price - half_spread - delta_bid_as
    ask_price = reservation_price + half_spread + delta_ask_as

    # 5. Sanity check: prevent crossing
    bid_price = min(bid_price, mid_price - tick_size)
    ask_price = max(ask_price, mid_price + tick_size)
    if bid_price >= ask_price:
        bid_price = ask_price - tick_size

    # Calculate spreads in percentage
    bid_spread_pct = (mid_price - bid_price) / mid_price * 100
    ask_spread_pct = (ask_price - mid_price) / mid_price * 100
    total_spread_bps = (ask_price - bid_price) / mid_price * 10000

    return {
        'bid_price': bid_price,
        'ask_price': ask_price,
        'reservation_price': reservation_price,
        'half_spread': half_spread,
        'bid_spread_pct': bid_spread_pct,
        'ask_spread_pct': ask_spread_pct,
        'total_spread_bps': total_spread_bps,
        'phi_inventory': phi_inventory,
        'phi_adverse_sel': phi_adverse_sel,
        'phi_adverse_cost': phi_adverse_cost,
        'phi_fee': phi_fee,
        'volatility_used': sigma
    }


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


def get_fills(account, symbol=None, limit=100):
    """Get recent fills/trades for an account"""
    api_url = f"{REST_URL}/fills"
    params = {"account": account}
    if symbol:
        params["symbol"] = symbol
    if limit:
        params["limit"] = limit

    response = requests.get(api_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if data.get("success"):
            return data.get("data", [])

    return []


def calculate_inventory_from_fills(symbol, account, notional_amount):
    """
    Calculate current inventory from fill history
    Returns inventory as a multiple of the notional_amount

    For example:
    - If you bought 2.5 UNI (1x notional) and sold 1.5 UNI (0.6x notional), inventory = +0.4
    - Inventory represents: (total_buys - total_sells) / notional_amount
    """
    fills = get_fills(account, symbol=symbol, limit=500)

    if not fills:
        print(f"[INFO] No fill history found for {symbol}, assuming zero inventory")
        return 0.0

    total_bought = 0.0  # Total base currency bought
    total_sold = 0.0    # Total base currency sold

    for fill in fills:
        if fill.get('symbol') != symbol:
            continue

        amount = float(fill.get('amount', 0))
        side = fill.get('side', '')

        if side == 'bid':  # Buy order filled
            total_bought += amount
        elif side == 'ask':  # Sell order filled
            total_sold += amount

    net_position = total_bought - total_sold

    # Calculate inventory as multiple of notional amount
    # For example: if net_position is 2.5 UNI and notional_amount is $20 (~2.5 UNI), inventory = 1.0
    inventory = net_position

    print(f"[INFO] Calculated inventory from {len(fills)} fills:")
    print(f"  Total bought: {total_bought:.2f} {symbol}")
    print(f"  Total sold: {total_sold:.2f} {symbol}")
    print(f"  Net position: {net_position:.2f} {symbol}")
    print(f"  Inventory: {inventory:.2f}")

    return inventory


class PositionTracker:
    """Async position tracker using WebSocket with REST API fallback"""

    def __init__(self, account, symbol):
        self.account = account
        self.symbol = symbol
        self.inventory = 0.0
        self.total_bought = 0.0
        self.total_sold = 0.0
        self.last_update = None
        self.running = False
        self.use_websocket = True

    async def track_fills_websocket(self, refresh_interval=5):
        """Track fills via WebSocket subscription"""
        ws_url = f"{WS_URL}/fills/{self.account}"

        try:
            print(f"[INFO] Connecting to fills WebSocket: {ws_url}")
            async with websockets.connect(ws_url) as websocket:
                print(f"[INFO] Connected to fills WebSocket for {self.account}")

                # Subscribe to fills
                subscribe_msg = {
                    "type": "subscribe",
                    "channel": "fills",
                    "account": self.account
                }
                await websocket.send(json.dumps(subscribe_msg))

                self.running = True

                while self.running:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=refresh_interval)
                        data = json.loads(message)

                        if data.get('type') == 'fill':
                            self._process_fill(data)

                    except asyncio.TimeoutError:
                        # No new fills, refresh from REST API
                        await self._refresh_from_rest()
                        continue

        except Exception as e:
            print(f"[WARN] WebSocket connection failed: {e}")
            print(f"[INFO] Falling back to REST API polling")
            self.use_websocket = False
            await self._poll_rest_api(refresh_interval)

    async def _poll_rest_api(self, refresh_interval=5):
        """Fallback: Poll REST API for fills"""
        while self.running:
            await self._refresh_from_rest()
            await asyncio.sleep(refresh_interval)

    async def _refresh_from_rest(self):
        """Refresh inventory from REST API fills endpoint"""
        fills = get_fills(self.account, symbol=self.symbol, limit=500)

        if not fills:
            return

        self.total_bought = 0.0
        self.total_sold = 0.0

        for fill in fills:
            if fill.get('symbol') != self.symbol:
                continue

            amount = float(fill.get('amount', 0))
            side = fill.get('side', '')

            if side == 'bid':
                self.total_bought += amount
            elif side == 'ask':
                self.total_sold += amount

        self.inventory = self.total_bought - self.total_sold
        self.last_update = time.time()

    def _process_fill(self, fill_data):
        """Process a single fill from WebSocket"""
        if fill_data.get('symbol') != self.symbol:
            return

        amount = float(fill_data.get('amount', 0))
        side = fill_data.get('side', '')

        if side == 'bid':
            self.total_bought += amount
        elif side == 'ask':
            self.total_sold += amount

        self.inventory = self.total_bought - self.total_sold
        self.last_update = time.time()

        print(f"[FILL] {side.upper()} {amount} {self.symbol} | Inventory: {self.inventory:.2f}")

    def get_inventory(self):
        """Get current inventory"""
        return self.inventory

    def stop(self):
        """Stop tracking"""
        self.running = False


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


async def place_single_order(side, symbol, notional, inventory, params, market_specs, quotes, auto_cancel):
    """
    Place a single order (bid or ask) with optional cancellation

    Args:
        side: "bid" or "ask"
        symbol: trading symbol
        notional: order size in USD
        inventory: current inventory
        params: spread parameters
        market_specs: tick_size, lot_size
        quotes: calculated optimal quotes
        auto_cancel: whether to cancel after placement
    """
    side_name = "BUY" if side == "bid" else "SELL"
    tick_size = market_specs['tick_size']
    lot_size = market_specs['lot_size']

    print(f"\n[{side_name}] Preparing {side} order...")

    # Select price based on side
    if side == "bid":
        limit_price_raw = quotes['bid_price']
    else:
        limit_price_raw = quotes['ask_price']

    # Use Decimal for precise rounding
    limit_price_dec = Decimal(str(limit_price_raw))
    tick_size_dec = Decimal(str(tick_size))
    lot_size_dec = Decimal(str(lot_size))
    notional_dec = Decimal(str(notional))

    # Round to tick size
    limit_price_dec = (limit_price_dec / tick_size_dec).quantize(Decimal('1'), rounding=ROUND_DOWN) * tick_size_dec

    # Calculate amount
    amount_raw = notional_dec / limit_price_dec
    amount_dec = (amount_raw / lot_size_dec).quantize(Decimal('1'), rounding=ROUND_DOWN) * lot_size_dec

    limit_price = float(limit_price_dec)
    amount = float(amount_dec)
    actual_notional = limit_price * amount

    print(f"[{side_name}] Price: ${limit_price} (tick size {tick_size})")
    print(f"[{side_name}] Amount: {amount} {symbol} (${actual_notional:.2f} notional, lot size {lot_size})")

    # Place order
    print(f"\n[{side_name}] Placing limit {side} order...")
    response, client_order_id = place_limit_order(symbol, limit_price, amount, side=side)

    if response.status_code != 200:
        print(f"[{side_name}] FAILED to place order")
        return None

    data = response.json()
    if not data.get("success"):
        print(f"[{side_name}] FAILED to place order")
        return None

    order_id = data.get("data", {}).get("order_id")
    print(f"[{side_name}] SUCCESS - Order ID: {order_id}, Client ID: {client_order_id}")

    # Wait for order to be registered
    await asyncio.sleep(2)

    # Verify order
    print(f"[{side_name}] Verifying order...")
    open_orders = get_open_orders(SOL_WALLET)
    order_found = False
    for order in open_orders:
        if order.get("client_order_id") == client_order_id:
            order_found = True
            print(f"[{side_name}] Order verified in open orders")
            break

    if not order_found:
        print(f"[{side_name}] WARNING - Order not found in open orders")

    # Cancel if requested
    if auto_cancel:
        print(f"[{side_name}] Cancelling order...")
        cancel_order(symbol, client_order_id)

        await asyncio.sleep(2)

        # Verify cancellation
        open_orders_after = get_open_orders(SOL_WALLET)
        still_exists = False
        for order in open_orders_after:
            if order.get("client_order_id") == client_order_id:
                still_exists = True
                break

        if still_exists:
            print(f"[{side_name}] WARNING - Order still exists after cancellation")
        else:
            print(f"[{side_name}] Order successfully cancelled")
    else:
        print(f"[{side_name}] Order left open (auto_cancel=False)")

    return {
        'side': side,
        'order_id': order_id,
        'client_order_id': client_order_id,
        'price': limit_price,
        'amount': amount
    }


async def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Dynamic spread order placement using Avellaneda-Stoikov model')
    parser.add_argument('--buy-only', action='store_true', help='Place only buy order')
    parser.add_argument('--sell-only', action='store_true', help='Place only sell order')
    parser.add_argument('--override-symbol', type=str, default=None, help='Override config file symbol')
    parser.add_argument('--override-notional', type=float, default=None, help='Override config file notional amount')
    parser.add_argument('--override-inventory', type=float, default=None, help='Override calculated inventory')
    parser.add_argument('--no-cancel', action='store_true', help='Do not cancel the order after placement')
    args = parser.parse_args()

    # Load trading configuration
    config = load_trading_config()

    # Use config values, allow command-line overrides
    symbol = (args.override_symbol or config.get('symbol', 'UNI')).upper()
    notional = args.override_notional or config.get('notional_amount', 20.0)
    auto_cancel = config.get('auto_cancel', True) and not args.no_cancel

    # Get current inventory from fill history (or override)
    if args.override_inventory is not None:
        inventory = args.override_inventory
        print(f"[INFO] Using inventory override: {inventory}")
    elif config.get('inventory_tracking', {}).get('enabled', True):
        print(f"\n[INFO] Calculating inventory from fill history...")
        inventory = calculate_inventory_from_fills(symbol, SOL_WALLET, notional)
    else:
        inventory = 0.0
        print(f"[INFO] Inventory tracking disabled, using zero inventory")

    # Determine which orders to place
    place_bid = not args.sell_only
    place_ask = not args.buy_only

    print(f"\n{'='*70}")
    print(f"AVELLANEDA-STOIKOV DYNAMIC SPREAD ORDER")
    print(f"{'='*70}")
    print(f"Symbol: {symbol}")
    print(f"Orders: {'BID' if place_bid else ''}{' & ' if place_bid and place_ask else ''}{'ASK' if place_ask else ''}")
    print(f"Notional: ${notional} per order")
    print(f"Current Inventory: {inventory}")
    print(f"{'='*70}\n")

    # Step 1: Load spread parameters
    print("Step 1: Loading calibrated spread parameters...")
    params = load_spread_parameters(symbol)
    market_specs = get_market_specs(symbol, params)

    # Step 2: Get current market data via WebSocket
    print("\nStep 2: Fetching current orderbook via WebSocket...")
    market_data = await get_current_orderbook_ws(symbol)

    # Step 3: Calculate optimal quotes using AS model
    print("\nStep 3: Calculating optimal quotes using Avellaneda-Stoikov model...")
    quotes = calculate_optimal_quotes(market_data, params, inventory)

    # Display calculations
    print(f"\n--- Dynamic Spread Calculation ---")
    print(f"Mid Price: ${market_data['mid_price']:.6f}")
    print(f"Microprice Deviation: ${market_data['micro_deviation']:.6f}")
    print(f"Volatility (sigma): {quotes['volatility_used']:.6e}")
    print(f"\nReservation Price: ${quotes['reservation_price']:.6f}")
    print(f"Half-Spread Components:")
    print(f"  - Inventory term: ${quotes['phi_inventory']:.6f}")
    print(f"  - Adverse selection: ${quotes['phi_adverse_sel']:.6f}")
    print(f"  - Adverse cost: ${quotes['phi_adverse_cost']:.6f}")
    print(f"  - Fee term: ${quotes['phi_fee']:.6f}")
    print(f"  - Total half-spread: ${quotes['half_spread']:.6f}")
    print(f"\nOptimal Quotes:")
    print(f"  Bid: ${quotes['bid_price']:.6f} ({quotes['bid_spread_pct']:.4f}% from mid)")
    print(f"  Ask: ${quotes['ask_price']:.6f} ({quotes['ask_spread_pct']:.4f}% from mid)")
    print(f"  Total Spread: {quotes['total_spread_bps']:.2f} bps")

    # Step 4: Place orders in parallel
    print("\nStep 4: Placing orders in parallel...")

    tasks = []
    if place_bid:
        tasks.append(place_single_order("bid", symbol, notional, inventory, params, market_specs, quotes, auto_cancel))
    if place_ask:
        tasks.append(place_single_order("ask", symbol, notional, inventory, params, market_specs, quotes, auto_cancel))

    # Run both order placements concurrently
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Display results
    print(f"\n{'='*70}")
    print("RESULTS")
    print(f"{'='*70}")
    for result in results:
        if isinstance(result, Exception):
            print(f"[ERROR] {result}")
        elif result:
            print(f"[{result['side'].upper()}] Order ID: {result['order_id']}, Price: ${result['price']}, Amount: {result['amount']}")

    print(f"\n{'='*70}")
    print("DONE")
    print(f"{'='*70}")


if __name__ == "__main__":
    asyncio.run(main())
