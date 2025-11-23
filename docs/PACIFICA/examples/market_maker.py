"""
Continuous Market Making using Avellaneda-Stoikov model

Runs indefinitely with independent async processes for bid and ask orders.
Each side continuously:
1. Places order at optimal price
2. Waits for configured time
3. Cancels order
4. Repeats

Uses shared state for:
- Market data (orderbook, microprice)
- Inventory tracking (from positions)
- Spread parameters

Configuration loaded from params/trading_config.json

Usage:
    python market_maker.py                    # Start market making
"""
import asyncio
import json
import time
import uuid
import sys
import signal
import random
from pathlib import Path
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime
import numpy as np
import logging

import requests
import websockets
from solders.keypair import Keypair
from dotenv import load_dotenv
import os
from typing import Optional

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init()
except Exception:  # pragma: no cover - graceful fallback when colorama missing
    class _NoColor:
        RESET_ALL = ""

    class _NoFore:
        RED = GREEN = CYAN = MAGENTA = YELLOW = BLUE = WHITE = RESET = ""

    Fore = _NoFore()  # type: ignore
    Style = _NoColor()  # type: ignore

# Make project packages importable when running from various entrypoints
sys.path.insert(0, str(Path(__file__).parent))
from pacifica_sdk.common.constants import REST_URL, WS_URL
from pacifica_sdk.common.utils import sign_message


# Load environment variables
load_dotenv()
SOL_WALLET = os.getenv("SOL_WALLET")
AGENT_WALLET_PUBLIC = os.getenv("API_PUBLIC")
AGENT_WALLET_PRIVATE = os.getenv("API_PRIVATE")

# Default paths
TRADING_CONFIG_PATH = Path("params") / "trading_config.json"

LOG_DIR = Path("output")
LOG_FILE = LOG_DIR / "market_maker.log"

logger = logging.getLogger("market_maker")


CONSOLE_STYLES = {
    "INIT": (Fore.CYAN, "🚀"),
    "CONFIG": (Fore.CYAN, "⚙️"),
    "PARAMS": (Fore.MAGENTA, "📐"),
    "STARTUP": (Fore.YELLOW, "🧹"),
    "MARKET": (Fore.BLUE, "📈"),
    "INVENTORY": (Fore.YELLOW, "📦"),
    "QUOTES": (Fore.CYAN, "📝"),
    "BUY": (Fore.GREEN, "🟢"),
    "SELL": (Fore.RED, "🔴"),
    "STATS": (Fore.MAGENTA, "📊"),
    "SHUTDOWN": (Fore.YELLOW, "🛑"),
    "ERROR": (Fore.RED, "❌"),
    "WARN": (Fore.YELLOW, "⚠️"),
}


def console_log(tag: str, message: str, *, icon: Optional[str] = None, color: Optional[str] = None) -> None:
    """Pretty-print structured console logs with optional color and emoji."""
    base_color, base_icon = CONSOLE_STYLES.get(tag, ("", ""))
    color_code = color or base_color
    icon_char = icon or base_icon
    timestamp = get_timestamp_ms()
    prefix = f"[{timestamp}] [{tag}]"
    icon_prefix = f"{icon_char} " if icon_char else ""
    reset = Style.RESET_ALL if color_code else ""
    log_line = f"{color_code}{icon_prefix}{prefix} {message}{reset}"

    try:
        print(log_line, flush=True)
    except UnicodeEncodeError:
        # Fallback for Windows console that can't handle emojis
        log_line_safe = f"{color_code}[{tag}] {message}{reset}"
        print(log_line_safe.encode('ascii', 'ignore').decode('ascii'), flush=True)

def setup_logging():
    """Configure file logging and reset the log file on each launch."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    file_handler = logging.FileHandler(LOG_FILE, mode='w', encoding='utf-8')
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s | %(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.info("Logging initialized at %s", LOG_FILE)

    return logger


class SharedState:
    """Shared state across async processes"""
    def __init__(self):
        self.lock = asyncio.Lock()

        # Market data
        self.mid_price = 0.0
        self.microprice = 0.0
        self.micro_deviation = 0.0
        self.last_market_update = 0.0

        # Inventory
        self.inventory_raw = 0.0
        self.inventory = 0.0  # Normalized inventory used in AS model
        self.total_bought = 0.0
        self.total_sold = 0.0
        self.last_inventory_update = 0.0
        self.inventory_order_size = 1.0  # Reference order size for normalization

        # Quotes
        self.bid_price = 0.0
        self.ask_price = 0.0
        self.reservation_price = 0.0
        self.half_spread = 0.0

        # Control
        self.running = True
        self.stoploss_triggered = False  # Flag to indicate stoploss was hit

        # Stats
        self.orders_placed = {'bid': 0, 'ask': 0}
        self.orders_filled = {'bid': 0, 'ask': 0}
        self.errors = {'bid': 0, 'ask': 0}

        # Active order tracking for graceful shutdown
        self.active_orders = []  # List of (symbol, client_order_id) tuples


def get_timestamp_ms():
    """Get current timestamp with millisecond precision"""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def load_trading_config():
    """Load trading configuration from JSON file"""
    if not TRADING_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Trading config file not found: {TRADING_CONFIG_PATH}")

    with open(TRADING_CONFIG_PATH, 'r') as f:
        config = json.load(f)

    console_log("CONFIG", "Loaded trading config")
    console_log("CONFIG", f"Symbol: {config.get('symbol', 'N/A')}", icon="")
    console_log(
        "CONFIG",
        f"Notional: ${config.get('notional_amount', 'N/A')} | Order lifetime: {config.get('order_lifetime_seconds', 10)}s",
        icon="",
    )

    return config


def load_spread_parameters(symbol):
    """Load calibrated spread parameters from JSON file"""
    params_file = Path("params") / f"spread_parameters_{symbol}.json"

    if not params_file.exists():
        raise FileNotFoundError(f"Spread parameters file not found: {params_file}")

    with open(params_file, 'r') as f:
        params = json.load(f)

    console_log("PARAMS", f"Loaded spread parameters for {symbol}")
    console_log(
        "PARAMS",
        f"gamma={params['gamma']:.4e} | tau={params['tau']:.4f}s | volatility={params['latest_volatility']:.6e}",
        icon="",
    )

    return params


def get_market_specs(symbol, params):
    """Get market specifications from params - works for any symbol"""
    from decimal import Decimal, ROUND_HALF_UP
    import math

    tick_size_raw = params.get('tick_size', 0.001)

    # Clean up tick size - round to reasonable precision to avoid floating point errors
    try:
        # Round the raw tick size to a safe number of decimal places (e.g., 12)
        # before converting to Decimal to eliminate floating point noise from JSON parsing.
        tick_size_cleaned = round(float(tick_size_raw), 12)
        tick_size_dec = Decimal(str(tick_size_cleaned))
    except (TypeError, ValueError):
        tick_size_dec = Decimal('0.000001')  # Fallback

    # Ensure tick size is positive
    if tick_size_dec <= 0:
        tick_size_dec = Decimal('0.000001')

    tick_size = float(tick_size_dec)

    # Lot size map for known symbols
    lot_size_map = {
        'BTC': 0.001,
        'ETH': 0.001,
        'SOL': 0.1,
        'UNI': 0.1,
        'BNB': 0.001,
        'PENGU': 1.0,
        'AVAX': 0.1,
        'LINK': 0.1,
        'MATIC': 1.0,
        'ARB': 1.0,
        'OP': 1.0,
    }

    # Default lot size: use 0.1 for most tokens, unless price suggests otherwise
    lot_size = lot_size_map.get(symbol)

    if lot_size is None:
        # Estimate lot size based on latest mid price if available
        latest_price = params.get('latest_mid_price', 0)
        if latest_price > 0:
            if latest_price > 1000:  # High price assets (like BTC)
                lot_size = 0.001
            elif latest_price > 10:  # Medium price (like ETH, SOL)
                lot_size = 0.01
            elif latest_price < 0.01:  # Very low price (like some memecoins)
                lot_size = 10.0
            else:  # Default range
                lot_size = 0.1
        else:
            lot_size = 0.1  # Fallback default

    logger.info(
        "Market specs | symbol=%s tick_size=%.8f lot_size=%.6f tick_size_raw=%s",
        symbol,
        tick_size,
        lot_size,
        tick_size_raw
    )

    # Return tick_size as Decimal to preserve precision throughout
    return {
        'tick_size': tick_size,
        'tick_size_dec': tick_size_dec,  # Use the cleaned Decimal directly
        'lot_size': lot_size,
        'lot_size_dec': Decimal(str(lot_size))  # Decimal version for calculations
    }


def compute_reference_order_size(notional_amount, params, market_specs):
    """Approximate per-order position size for inventory normalization."""
    if notional_amount <= 0:
        return 1.0

    reference_price = params.get('latest_mid_price') or params.get('latest_optimal_bid')
    if not reference_price or reference_price <= 0:
        reference_price = 1.0

    tick_size = market_specs['tick_size']
    lot_size = market_specs['lot_size']

    # Safety check: ensure tick_size and lot_size are positive
    if tick_size <= 0:
        tick_size = 0.000001
    if lot_size <= 0:
        lot_size = 0.1

    price_dec = Decimal(str(reference_price))
    tick_dec = Decimal(str(tick_size))
    lot_dec = Decimal(str(lot_size))
    notional_dec = Decimal(str(notional_amount))

    # Round price to tick size
    try:
        price_dec = (price_dec / tick_dec).quantize(Decimal('1'), rounding=ROUND_DOWN) * tick_dec
    except Exception as e:
        logger.warning("Failed to round price to tick size: %s, using raw price", e)
        price_dec = Decimal(str(reference_price))

    if price_dec <= 0:
        price_dec = Decimal('1')

    # Calculate amount
    amount_dec = (notional_dec / price_dec)
    try:
        amount_dec = (amount_dec / lot_dec).quantize(Decimal('1'), rounding=ROUND_DOWN) * lot_dec
    except Exception as e:
        logger.warning("Failed to round amount to lot size: %s, using raw amount", e)

    reference_size = float(amount_dec)
    if reference_size <= 0:
        reference_size = float(lot_size)

    return reference_size


def get_agent_keypair():
    """Get agent wallet keypair for signing"""
    if not AGENT_WALLET_PRIVATE:
        raise ValueError("API_PRIVATE (agent wallet private key) not found in .env file")
    if not AGENT_WALLET_PUBLIC:
        raise ValueError("API_PUBLIC (agent wallet public key) not found in .env file")
    if len(AGENT_WALLET_PRIVATE) < 80:
        raise ValueError(f"API_PRIVATE appears to be invalid (length: {len(AGENT_WALLET_PRIVATE)})")

    return Keypair.from_base58_string(AGENT_WALLET_PRIVATE)


async def get_current_orderbook_ws(symbol, timeout=10):
    """Get current orderbook via WebSocket to calculate microprice"""
    uri = WS_URL

    try:
        async with websockets.connect(uri) as websocket:
            # Subscribe to orderbook - use correct format from working code
            subscribe_msg = {
                "method": "subscribe",
                "params": {
                    "source": "book",
                    "symbol": symbol,
                    "agg_level": 1
                }
            }
            await websocket.send(json.dumps(subscribe_msg))

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

                                    return {
                                        'mid_price': mid_price,
                                        'microprice': microprice,
                                        'micro_deviation': micro_deviation
                                    }
                except asyncio.TimeoutError:
                    continue
    except Exception as e:
        console_log("ERROR", f"WebSocket error: {e}")
        raise


def calculate_optimal_quotes(market_data, params, inventory):
    """Calculate optimal bid/ask quotes using Avellaneda-Stoikov model"""
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
    sigma = params['latest_volatility']

    # Extract market data
    mid_price = market_data['mid_price']
    micro_deviation = market_data['micro_deviation']

    # Average k
    k_mid = (k_bid + k_ask) / 2
    k_mid_price_based = k_mid * mid_price / 10000

    # 1. Reservation price (with inventory skew)
    reservation_price = mid_price + (beta_alpha * micro_deviation) - (inventory * gamma * (sigma**2) * tau)

    # 2. Optimal half-spread
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

    # 5. Sanity checks
    bid_price = min(bid_price, mid_price - tick_size)
    ask_price = max(ask_price, mid_price + tick_size)

    return {
        'bid_price': bid_price,
        'ask_price': ask_price,
        'reservation_price': reservation_price,
        'half_spread': half_spread,
    }


def place_limit_order(symbol, price, amount, side="bid", post_only=True):
    """Place a limit order using REST API

    Args:
        symbol: Trading symbol
        price: Price as Decimal or float
        amount: Amount as Decimal or float
        side: 'bid' or 'ask'
        post_only: If True, uses ALO (Add Liquidity Only) to ensure order is maker-only (default: True)
    """
    agent_keypair = get_agent_keypair()
    timestamp = int(time.time() * 1_000)

    signature_header = {
        "timestamp": timestamp,
        "expiry_window": 2_000,
        "type": "create_order",
    }

    # Handle both Decimal and float inputs
    # If already Decimal, keep it; otherwise convert from string to avoid float precision issues
    if isinstance(price, Decimal):
        price_dec = price
    else:
        price_dec = Decimal(str(price))

    if isinstance(amount, Decimal):
        amount_dec = amount
    else:
        amount_dec = Decimal(str(amount))

    # Normalize to remove trailing zeros and convert to clean string
    price_str = str(price_dec.normalize())
    amount_str = str(amount_dec.normalize())

    # Use ALO (Add Liquidity Only) for post-only orders, GTC otherwise
    tif = "ALO" if post_only else "GTC"

    client_order_id = str(uuid.uuid4())
    signature_payload = {
        "symbol": symbol,
        "price": price_str,
        "reduce_only": False,
        "amount": amount_str,
        "side": side,
        "tif": tif,
        "client_order_id": client_order_id,
    }

    message, signature = sign_message(signature_header, signature_payload, agent_keypair)

    request_header = {
        "account": SOL_WALLET,
        "agent_wallet": AGENT_WALLET_PUBLIC,
        "signature": signature,
        "timestamp": signature_header["timestamp"],
        "expiry_window": signature_header["expiry_window"],
    }

    api_url = f"{REST_URL}/orders/create"
    headers = {"Content-Type": "application/json"}
    request = {**request_header, **signature_payload}
    response = requests.post(api_url, json=request, headers=headers)

    if response.status_code == 200:
        data = response.json()
        if data.get("success"):
            return response, client_order_id

    return response, None


def cancel_order(symbol, client_order_id):
    """Cancel an order by client_order_id"""
    agent_keypair = get_agent_keypair()
    timestamp = int(time.time() * 1_000)

    signature_header = {
        "timestamp": timestamp,
        "expiry_window": 2_000,
        "type": "cancel_order",
    }

    signature_payload = {
        "symbol": symbol,
        "client_order_id": client_order_id,
    }

    message, signature = sign_message(signature_header, signature_payload, agent_keypair)

    request_header = {
        "account": SOL_WALLET,
        "agent_wallet": AGENT_WALLET_PUBLIC,
        "signature": signature,
        "timestamp": signature_header["timestamp"],
        "expiry_window": signature_header["expiry_window"],
    }

    api_url = f"{REST_URL}/orders/cancel"
    headers = {"Content-Type": "application/json"}
    request = {**request_header, **signature_payload}
    response = requests.post(api_url, json=request, headers=headers)

    return response


def cancel_all_orders_rest(symbol=None):
    """Cancel all orders using REST API (optional symbol filter)"""
    agent_keypair = get_agent_keypair()
    timestamp = int(time.time() * 1_000)

    signature_header = {
        "timestamp": timestamp,
        "expiry_window": 2_000,
        "type": "cancel_all_orders",
    }

    signature_payload = {
        "all_symbols": symbol is None,
    }

    if symbol is not None:
        signature_payload["symbol"] = symbol

    signature_payload["exclude_reduce_only"] = False

    message, signature = sign_message(signature_header, signature_payload, agent_keypair)

    request_header = {
        "account": SOL_WALLET,
        "agent_wallet": AGENT_WALLET_PUBLIC,
        "signature": signature,
        "timestamp": signature_header["timestamp"],
        "expiry_window": signature_header["expiry_window"],
    }

    api_url = f"{REST_URL}/orders/cancel_all"
    headers = {"Content-Type": "application/json"}
    request = {**request_header, **signature_payload}
    response = requests.post(api_url, json=request, headers=headers)

    return response


def set_leverage(symbol, leverage):
    """Set leverage for a specific symbol"""
    agent_keypair = get_agent_keypair()
    timestamp = int(time.time() * 1_000)

    signature_header = {
        "type": "update_leverage",
        "timestamp": timestamp,
        "expiry_window": 2_000
    }

    signature_payload = {
        "symbol": symbol,
        "leverage": leverage
    }

    message, signature = sign_message(signature_header, signature_payload, agent_keypair)

    request_header = {
        "account": SOL_WALLET,
        "agent_wallet": AGENT_WALLET_PUBLIC,
        "signature": signature,
        "timestamp": signature_header["timestamp"],
        "expiry_window": signature_header["expiry_window"]
    }

    api_url = f"{REST_URL}/account/leverage"
    headers = {"Content-Type": "application/json"}
    request = {**request_header, **signature_payload}
    response = requests.post(api_url, json=request, headers=headers)

    return response


def get_market_info(symbol):
    """Get market information including max leverage"""
    api_url = f"{REST_URL}/info"

    try:
        response = requests.get(api_url, timeout=5)
        response.raise_for_status()
        data = response.json()

        if data.get("success"):
            markets = data.get("data", [])
            for market in markets:
                if market.get("symbol") == symbol:
                    return market
        return None
    except Exception as e:
        logger.warning("Failed to fetch market info for %s: %s", symbol, e)
        return None


def get_account_leverage_settings(account):
    """Get current leverage settings for account"""
    api_url = f"{REST_URL}/account/settings"
    params = {"account": account}

    try:
        response = requests.get(api_url, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()

        if data.get("success"):
            return data.get("data", [])
        return []
    except Exception as e:
        logger.warning("Failed to fetch account leverage settings: %s", e)
        return []


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


def get_positions(account, symbol=None, timeout=5):
    """Get current positions for an account."""
    api_url = f"{REST_URL}/positions"
    params = {"account": account}
    symbol_upper = str(symbol).upper() if symbol else None

    logger.debug(
        "Fetching positions | url=%s params=%s timeout=%s",
        api_url,
        params,
        timeout,
    )
    request_started = time.time()
    try:
        response = requests.get(api_url, params=params, timeout=timeout)
        logger.debug(
            "Positions request | url=%s params=%s status=%s",
            api_url,
            params,
            response.status_code,
        )
    except Exception as exc:
        logger.exception("Positions request failed: %s", exc)
        return []

    latency = time.time() - request_started
    logger.debug(
        "Positions response metadata | status=%s elapsed=%.3fs headers=%s",
        response.status_code,
        latency,
        dict(response.headers),
    )

    if response.status_code != 200:
        body_preview = response.text[:500]
        logger.warning(
            "Positions request returned status %s | body=%s",
            response.status_code,
            body_preview,
        )
        return []

    raw_preview = response.text[:1000]
    logger.debug("Positions response preview | body=%s", raw_preview)
    try:
        data = response.json()
    except ValueError:
        body_preview = response.text[:500]
        logger.error("Positions response not JSON | body=%s", body_preview)
        return []

    if not data.get("success"):
        logger.warning("Positions response success flag false | payload=%s", data)
        return []

    positions = data.get("data", []) or []
    logger.debug("Positions payload parsed | count=%d payload=%s", len(positions), positions)

    if symbol_upper:
        filtered_positions = [
            pos
            for pos in positions
            if str(pos.get("symbol") or pos.get("s") or "").upper() == symbol_upper
        ]
        if len(filtered_positions) != len(positions):
            logger.debug(
                "Positions filtered client-side | requested_symbol=%s before=%d after=%d",
                symbol_upper,
                len(positions),
                len(filtered_positions),
            )
        positions = filtered_positions

    return positions


def _compute_inventory_from_positions(positions, symbol):
    """Calculate inventory totals from raw position payloads."""
    symbol_upper = (symbol or "").upper()
    total_bought = 0.0
    total_sold = 0.0
    if not positions:
        logger.debug("No positions returned for %s", symbol_upper)
        return total_bought, total_sold, 0.0
    for pos in positions:
        pos_symbol = str(pos.get("symbol") or pos.get("s") or "").upper()
        if symbol_upper and pos_symbol != symbol_upper:
            continue
        raw_amount = pos.get("amount", pos.get("size", pos.get("a", 0)))
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            logger.warning("Unable to parse position amount | position=%s", pos)
            continue
        side_value = str(pos.get("side", pos.get("d", ""))).lower()
        logger.debug(
            "Parsing position | symbol=%s side=%s raw_amount=%s parsed_amount=%.6f payload=%s",
            pos_symbol,
            side_value,
            raw_amount,
            amount,
            pos,
        )
        if side_value in ("long", "bid", "buy"):
            total_bought += amount
        elif side_value in ("short", "ask", "sell"):
            total_sold += amount
        else:
            logger.warning("Unknown side '%s' in position payload | position=%s", side_value, pos)
    net_inventory = total_bought - total_sold
    logger.debug(
        "Inventory computation complete | symbol=%s total_bought=%.6f total_sold=%.6f net=%.6f",
        symbol_upper,
        total_bought,
        total_sold,
        net_inventory,
    )
    return total_bought, total_sold, net_inventory


async def bootstrap_initial_inventory(state, symbol, order_size_ref):
    """Fetch existing positions and seed shared state before async tasks spin up."""
    logger.info("Bootstrapping inventory from existing positions | symbol=%s", symbol)
    positions = get_positions(SOL_WALLET, symbol=symbol)
    total_bought, total_sold, net_inventory = _compute_inventory_from_positions(positions, symbol)
    logger.info(
        "Bootstrap inventory snapshot | symbol=%s total_bought=%.6f total_sold=%.6f net=%.6f | positions=%s",
        symbol,
        total_bought,
        total_sold,
        net_inventory,
        positions,
    )
    normalized_inventory = net_inventory / order_size_ref if order_size_ref > 0 else net_inventory
    async with state.lock:
        state.total_bought = total_bought
        state.total_sold = total_sold
        state.inventory_raw = net_inventory
        state.inventory = normalized_inventory
        state.inventory_order_size = order_size_ref
        state.last_inventory_update = time.time()
    print(
        f"[{get_timestamp_ms()}] [INVENTORY] Bootstrap totals -> bought={total_bought:.6f} "
        f"sold={total_sold:.6f} net_raw={net_inventory:.6f} net_norm={normalized_inventory:.6f} "
        f"(order_size_ref={order_size_ref:.6f})",
        flush=True,
    )
    if not positions:
        print(
            f"[{get_timestamp_ms()}] [INVENTORY] No existing positions returned during bootstrap",
            flush=True,
        )
    return positions, total_bought, total_sold, net_inventory


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


def get_account_info(account, timeout=5):
    """Get account information including equity and balances"""
    api_url = f"{REST_URL}/account"
    params = {"account": account}

    try:
        response = requests.get(api_url, params=params, timeout=timeout)
        logger.debug(
            "Account info request | url=%s params=%s status=%s",
            api_url,
            params,
            response.status_code,
        )
    except Exception as exc:
        logger.exception("Account info request failed: %s", exc)
        return None

    if response.status_code != 200:
        logger.warning(
            "Account info request returned status %s | body=%s",
            response.status_code,
            response.text[:500],
        )
        return None

    try:
        data = response.json()
    except ValueError:
        logger.error("Account info response not JSON | body=%s", response.text[:500])
        return None

    if not data.get("success"):
        logger.warning("Account info response success flag false | payload=%s", data)
        return None

    return data.get("data", {})


async def market_data_updater(state, symbol):
    """Continuously update market data via WebSocket"""
    console_log("MARKET", f"Starting market data updater for {symbol}")

    while state.running:
        try:
            market_data = await get_current_orderbook_ws(symbol, timeout=5)

            async with state.lock:
                state.mid_price = market_data['mid_price']
                state.microprice = market_data['microprice']
                state.micro_deviation = market_data['micro_deviation']
                state.last_market_update = time.time()

        except Exception as e:
            console_log("MARKET", f"Error updating market data: {e}", icon="⚠️", color=Fore.YELLOW)
            await asyncio.sleep(0.5)
            continue

        await asyncio.sleep(0.05)  # Update every 50ms for very fast market data


async def inventory_updater(state, symbol):
    """Continuously update inventory using positions data"""
    console_log("INVENTORY", f"Starting inventory updater for {symbol}")

    previous_totals = None
    order_size_ref = None

    async with state.lock:
        if state.last_inventory_update > 0:
            previous_totals = (
                state.total_bought,
                state.total_sold,
                state.inventory_raw,
            )
            order_size_ref = state.inventory_order_size
            normalized_snapshot = state.inventory
    if previous_totals:
        logger.info(
            "Inventory updater using preloaded snapshot | symbol=%s total_bought=%.6f total_sold=%.6f net_raw=%.6f net_norm=%.6f",
            symbol,
            previous_totals[0],
            previous_totals[1],
            previous_totals[2],
            normalized_snapshot,
        )
    else:
        logger.info(
            "No preloaded inventory found; fetching initial snapshot | symbol=%s",
            symbol,
        )
        positions = get_positions(SOL_WALLET, symbol=symbol)
        total_bought, total_sold, net_inventory = _compute_inventory_from_positions(positions, symbol)
        if order_size_ref is None:
            order_size_ref = state.inventory_order_size or 1.0
        normalized_inventory = net_inventory / order_size_ref if order_size_ref > 0 else net_inventory
        logger.info(
            "Initial inventory snapshot | symbol=%s total_bought=%.6f total_sold=%.6f net_raw=%.6f net_norm=%.6f | positions=%s",
            symbol,
            total_bought,
            total_sold,
            net_inventory,
            normalized_inventory,
            positions,
        )
        async with state.lock:
            state.total_bought = total_bought
            state.total_sold = total_sold
            state.inventory_raw = net_inventory
            state.inventory = normalized_inventory
            state.last_inventory_update = time.time()
            logger.debug(
                "State inventory bootstrapped | total_bought=%.6f total_sold=%.6f net_raw=%.6f net_norm=%.6f",
                state.total_bought,
                state.total_sold,
                state.inventory_raw,
                state.inventory,
            )
        previous_totals = (total_bought, total_sold, net_inventory)
        console_log(
            "INVENTORY",
            f"Initial snapshot -> bought={total_bought:.6f} sold={total_sold:.6f} "
            f"net_raw={net_inventory:.6f} net_norm={normalized_inventory:.6f} (ref={order_size_ref:.6f})",
            icon="",
        )

    while state.running:
        try:
            poll_started = time.time()
            positions = get_positions(SOL_WALLET, symbol=symbol)
            total_bought, total_sold, net_inventory = _compute_inventory_from_positions(positions, symbol)
            poll_latency = time.time() - poll_started
            if order_size_ref is None or order_size_ref <= 0:
                order_size_ref = state.inventory_order_size or 1.0
            normalized_inventory = net_inventory / order_size_ref if order_size_ref > 0 else net_inventory

            delta_tuple = None
            if previous_totals is not None:
                delta_tuple = (
                    total_bought - previous_totals[0],
                    total_sold - previous_totals[1],
                    net_inventory - previous_totals[2],
                )
            logger.debug(
                "Inventory poll complete | symbol=%s total_bought=%.6f total_sold=%.6f net_raw=%.6f "
                "net_norm=%.6f delta=%s poll_latency=%.3fs positions=%s",
                symbol,
                total_bought,
                total_sold,
                net_inventory,
                normalized_inventory,
                delta_tuple,
                poll_latency,
                positions,
            )

            if (
                previous_totals is None
                or any(
                    abs(curr - prev) > 1e-9
                    for curr, prev in zip(
                        (total_bought, total_sold, net_inventory),
                        previous_totals,
                    )
                )
            ):
                logger.info(
                    "Inventory refresh | symbol=%s total_bought=%.6f total_sold=%.6f net_raw=%.6f net_norm=%.6f | positions=%s",
                    symbol,
                    total_bought,
                    total_sold,
                    net_inventory,
                    normalized_inventory,
                    positions,
                )
                previous_totals = (total_bought, total_sold, net_inventory)
                console_log(
                    "INVENTORY",
                    f"Update -> bought={total_bought:.6f} sold={total_sold:.6f} "
                    f"net_raw={net_inventory:.6f} net_norm={normalized_inventory:.6f}",
                    icon="",
                )

            async with state.lock:
                state.total_bought = total_bought
                state.total_sold = total_sold
                state.inventory_raw = net_inventory
                state.inventory = normalized_inventory
                state.last_inventory_update = time.time()
                state.inventory_order_size = order_size_ref
                logger.debug(
                    "State inventory updated | total_bought=%.6f total_sold=%.6f net_raw=%.6f net_norm=%.6f",
                    state.total_bought,
                    state.total_sold,
                    state.inventory_raw,
                    state.inventory,
                )

        except Exception as e:
            logger.exception("Inventory updater error for %s", symbol)
            console_log("ERROR", f"Inventory updater error: {e}")

        for _ in range(10):
            if not state.running:
                return
            await asyncio.sleep(0.5)


async def quote_calculator(state, params):
    """Continuously calculate optimal quotes"""
    console_log("QUOTES", "Starting quote calculator")

    while state.running:
        try:
            async with state.lock:
                if state.mid_price > 0:
                    market_data = {
                        'mid_price': state.mid_price,
                        'microprice': state.microprice,
                        'micro_deviation': state.micro_deviation
                    }
                    inventory = state.inventory

                    quotes = calculate_optimal_quotes(market_data, params, inventory)

                    state.bid_price = quotes['bid_price']
                    state.ask_price = quotes['ask_price']
                    state.reservation_price = quotes['reservation_price']
                    state.half_spread = quotes['half_spread']

        except Exception as e:
            console_log("QUOTES", f"Error calculating quotes: {e}", icon="⚠️", color=Fore.YELLOW)

        await asyncio.sleep(0.01)  # Update every 10ms (extremely fast)


async def order_manager(state, symbol, side, notional, market_specs, order_lifetime):
    """Continuously manage orders for one side (bid or ask)"""
    side_name = "BUY" if side == "bid" else "SELL"
    tick_size = market_specs['tick_size']
    lot_size = market_specs['lot_size']
    console_log(side_name, f"Starting order manager (lifetime={order_lifetime}s)")

    # Exponential backoff parameters
    consecutive_failures = 0
    max_backoff = 30.0  # Maximum backoff delay in seconds
    base_delay = 1.0    # Base delay for first retry

    while state.running:
        try:
            # Check if stoploss was triggered and if we should skip this side
            async with state.lock:
                if state.stoploss_triggered:
                    inventory_raw = state.inventory_raw

                    # If inventory is near zero, we can stop
                    if abs(inventory_raw) < lot_size:
                        console_log(
                            side_name,
                            "Stoploss active and inventory near zero - stopping order manager",
                            icon="🛑",
                            color=Fore.YELLOW
                        )
                        return

                    # Only trade in the direction that reduces inventory
                    if inventory_raw > 0 and side == "bid":
                        # We have long position, don't buy more
                        await asyncio.sleep(0.5)
                        continue
                    elif inventory_raw < 0 and side == "ask":
                        # We have short position, don't sell more
                        await asyncio.sleep(0.5)
                        continue

            # Get current quote
            async with state.lock:
                if side == "bid":
                    limit_price_raw = state.bid_price
                else:
                    limit_price_raw = state.ask_price

                if limit_price_raw <= 0:
                    # Price not ready yet, yield and try again immediately
                    pass

            if limit_price_raw <= 0:
                await asyncio.sleep(0.01)  # Brief sleep to avoid spinning
                continue

            # Round to tick size using Decimal - keep as Decimal throughout
            # Use Decimal versions from market_specs to avoid any float conversion
            tick_size_dec = market_specs['tick_size_dec']
            lot_size_dec = market_specs['lot_size_dec']

            limit_price_dec = Decimal(str(limit_price_raw))
            notional_dec = Decimal(str(notional))

            # Round price to tick size
            try:
                # For bids, round down to nearest tick. For asks, round up.
                rounding_mode = ROUND_DOWN if side == "bid" else ROUND_UP
                limit_price_dec = limit_price_dec.quantize(tick_size_dec, rounding=rounding_mode)
            except Exception as e:
                logger.warning("Price rounding failed for %s: %s | Using raw price", side, e)
                limit_price_dec = Decimal(str(limit_price_raw))

            # Calculate amount
            if limit_price_dec <= 0:
                await asyncio.sleep(0.01)  # Brief sleep to avoid spinning
                continue
            amount_raw = notional_dec / limit_price_dec
            try:
                lots = (amount_raw / lot_size_dec).quantize(Decimal('1'), rounding=ROUND_DOWN)
                amount_dec = (lots * lot_size_dec).normalize()
            except Exception as e:
                logger.warning("Amount rounding failed for %s: %s | Using raw amount", side, e)
                amount_dec = amount_raw

            if amount_dec <= 0:
                await asyncio.sleep(0.01)  # Brief sleep to avoid spinning
                continue

            # Log order details before placement
            logger.debug(
                "Preparing order | side=%s symbol=%s price=%.8f amount=%.6f notional=%.2f tick_size=%.8f lot_size=%.6f",
                side,
                symbol,
                float(limit_price_dec),
                float(amount_dec),
                notional,
                tick_size,
                lot_size
            )

            # Place order - pass Decimal objects directly, not floats
            console_log(side_name, f"Placing {side} @ ${float(limit_price_dec):.6f} x {float(amount_dec):.6f}", icon="")
            response, client_order_id = place_limit_order(symbol, limit_price_dec, amount_dec, side=side)

            if response.status_code == 200 and client_order_id:
                # Reset backoff on success
                consecutive_failures = 0

                async with state.lock:
                    state.orders_placed[side] += 1
                    state.active_orders.append((symbol, client_order_id))

                console_log(side_name, f"Order placed (ID: {client_order_id[:8]}...)", icon="")

                # Wait for order lifetime with interruptible sleep
                elapsed = 0
                sleep_interval = 0.01  # Check every 10ms
                while elapsed < order_lifetime and state.running:
                    await asyncio.sleep(sleep_interval)
                    elapsed += sleep_interval

                # Check if still running (exit if shutdown requested)
                if not state.running:
                    break

                # Cancel order
                console_log(side_name, "Cancelling order", icon="")
                cancel_response = cancel_order(symbol, client_order_id)

                if cancel_response.status_code == 200:
                    console_log(side_name, "Order cancelled", icon="✅")
                    async with state.lock:
                        if (symbol, client_order_id) in state.active_orders:
                            state.active_orders.remove((symbol, client_order_id))
                else:
                    console_log(side_name, "Failed to cancel order", icon="⚠️", color=Fore.YELLOW)
                    async with state.lock:
                        state.errors[side] += 1

            else:
                # Increment failure counter for exponential backoff
                consecutive_failures += 1

                # Calculate exponential backoff with jitter
                # Formula: min(max_backoff, base_delay * 2^failures) + jitter
                backoff_delay = min(max_backoff, base_delay * (2 ** (consecutive_failures - 1)))
                # Add jitter: random value between 0 and 25% of the delay
                jitter = random.uniform(0, backoff_delay * 0.25)
                total_delay = backoff_delay + jitter

                # Log detailed error information
                error_msg = f"Failed to place order (status: {response.status_code})"
                try:
                    error_data = response.json()
                    error_detail = error_data.get('error') or error_data.get('message') or error_data
                    error_msg += f" - {error_detail}"
                    logger.error(
                        "Order placement failed | side=%s price=%.8f amount=%.6f status=%s response=%s failures=%d backoff=%.2fs",
                        side,
                        float(limit_price_dec),
                        float(amount_dec),
                        response.status_code,
                        error_data,
                        consecutive_failures,
                        total_delay
                    )
                except Exception:
                    logger.error(
                        "Order placement failed | side=%s price=%.8f amount=%.6f status=%s body=%s failures=%d backoff=%.2fs",
                        side,
                        float(limit_price_dec),
                        float(amount_dec),
                        response.status_code,
                        response.text[:500],
                        consecutive_failures,
                        total_delay
                    )

                console_log(
                    side_name,
                    f"{error_msg} (retry in {total_delay:.1f}s, attempt {consecutive_failures})",
                    icon="⚠️",
                    color=Fore.YELLOW,
                )
                async with state.lock:
                    state.errors[side] += 1

                # Apply exponential backoff with jitter
                await asyncio.sleep(total_delay)

        except Exception as e:
            # Increment failure counter for exponential backoff
            consecutive_failures += 1

            # Calculate exponential backoff with jitter
            backoff_delay = min(max_backoff, base_delay * (2 ** (consecutive_failures - 1)))
            jitter = random.uniform(0, backoff_delay * 0.25)
            total_delay = backoff_delay + jitter

            console_log(
                side_name,
                f"Error: {e} (retry in {total_delay:.1f}s, attempt {consecutive_failures})",
                icon="❌",
                color=Fore.RED
            )
            logger.error(
                "Order manager exception | side=%s error=%s failures=%d backoff=%.2fs",
                side,
                str(e),
                consecutive_failures,
                total_delay
            )
            async with state.lock:
                state.errors[side] += 1

            # Apply exponential backoff with jitter
            await asyncio.sleep(total_delay)


async def equity_monitor(state, stoploss_config):
    """Monitor account equity and stop trading if below minimum threshold"""
    if not stoploss_config.get("enabled", False):
        console_log("WARN", "Equity monitor disabled in config", icon="⚠️")
        return

    minimum_equity = stoploss_config.get("minimum_equity_usd", 100.0)
    check_interval = stoploss_config.get("check_interval_seconds", 10)

    # Get initial equity to show at startup
    initial_equity = None
    try:
        account_info = get_account_info(SOL_WALLET)
        if account_info:
            equity = account_info.get("equity") or account_info.get("total_equity") or account_info.get("account_equity")
            if equity is not None:
                initial_equity = float(equity)
    except Exception as e:
        logger.warning("Failed to fetch initial equity: %s", e)

    if initial_equity is not None:
        console_log(
            "INIT",
            f"Starting equity monitor | Current: ${initial_equity:.2f} | Min: ${minimum_equity:.2f} | Interval: {check_interval}s",
            icon="🛡️"
        )
    else:
        console_log("INIT", f"Starting equity monitor (min: ${minimum_equity:.2f}, interval: {check_interval}s)", icon="🛡️")

    while state.running:
        try:
            # Get account info
            account_info = get_account_info(SOL_WALLET)

            if account_info:
                # Extract equity - API may return different field names
                equity = account_info.get("equity") or account_info.get("total_equity") or account_info.get("account_equity")

                if equity is not None:
                    equity_value = float(equity)
                    logger.debug("Equity check | current=%.2f minimum=%.2f", equity_value, minimum_equity)

                    if equity_value < minimum_equity:
                        async with state.lock:
                            if not state.stoploss_triggered:
                                state.stoploss_triggered = True
                                console_log(
                                    "SHUTDOWN",
                                    f"STOPLOSS TRIGGERED: Equity ${equity_value:.2f} < Minimum ${minimum_equity:.2f}",
                                    icon="🛑",
                                    color=Fore.RED
                                )
                                console_log(
                                    "SHUTDOWN",
                                    "Unwinding positions - will stop when inventory reaches zero",
                                    icon="⚠️",
                                    color=Fore.YELLOW
                                )
                                logger.critical(
                                    "Stoploss triggered | current_equity=%.2f minimum_equity=%.2f | unwinding positions",
                                    equity_value,
                                    minimum_equity
                                )

                        # Continue monitoring - check if inventory is near zero
                        async with state.lock:
                            inventory_raw = abs(state.inventory_raw)

                        # Get lot size from market specs to determine "near zero"
                        # For now use a small threshold
                        if inventory_raw < 0.1:  # Adjust this threshold as needed
                            console_log(
                                "SHUTDOWN",
                                f"Inventory unwound (${inventory_raw:.6f}) - stopping market maker",
                                icon="✅",
                                color=Fore.GREEN
                            )
                            logger.info("Inventory unwound | inventory=%.6f | stopping", inventory_raw)
                            state.running = False
                            return
                else:
                    logger.warning("Equity field not found in account info | data=%s", account_info)
            else:
                logger.warning("Failed to fetch account info for equity check")

        except Exception as e:
            logger.exception("Equity monitor error")
            console_log("ERROR", f"Equity monitor error: {e}", icon="❌", color=Fore.RED)

        # Interruptible sleep
        for _ in range(int(check_interval)):
            if not state.running:
                return
            await asyncio.sleep(1)


async def periodic_cancel_all(state, symbol, interval_seconds=60):
    """Periodically cancel all orders as a safety measure to prevent order buildup"""
    console_log("INIT", f"Starting periodic cancel-all task (interval={interval_seconds}s)", icon="🧹")

    while state.running:
        # Interruptible sleep - check every second
        for _ in range(interval_seconds):
            if not state.running:
                return
            await asyncio.sleep(1)

        if not state.running:
            return

        try:
            # Run cancel_all in executor to avoid blocking
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, cancel_all_orders_rest, symbol)

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    cancelled_count = data.get("data", {}).get("cancelled_count", 0)
                    if cancelled_count > 0:
                        console_log(
                            "STARTUP",
                            f"Periodic cleanup: cancelled {cancelled_count} orders",
                            icon="🧹"
                        )
                        logger.info("Periodic cancel-all | symbol=%s cancelled=%d", symbol, cancelled_count)
                        # Clear active orders list since we just cancelled everything
                        async with state.lock:
                            state.active_orders.clear()
                    else:
                        logger.debug("Periodic cancel-all | symbol=%s cancelled=0", symbol)
                else:
                    logger.warning("Periodic cancel-all failed | symbol=%s response=%s", symbol, data)
            else:
                logger.warning(
                    "Periodic cancel-all HTTP error | symbol=%s status=%d body=%s",
                    symbol,
                    response.status_code,
                    response.text[:200]
                )
        except Exception as e:
            logger.exception("Periodic cancel-all exception | symbol=%s", symbol)
            console_log("WARN", f"Periodic cancel-all error: {e}", icon="⚠️", color=Fore.YELLOW)


async def stats_reporter(state):
    """Periodically report statistics"""
    console_log("STATS", "Starting stats reporter")

    while state.running:
        # Interruptible sleep - check every second
        for _ in range(30):
            if not state.running:
                return
            await asyncio.sleep(1)

        if not state.running:
            return

        async with state.lock:
            logger.debug(
                "Stats snapshot | mid=%.6f inventory=%.6f total_bought=%.6f total_sold=%.6f stoploss=%s",
                state.mid_price,
                state.inventory,
                state.total_bought,
                state.total_sold,
                state.stoploss_triggered,
            )
            print()
            console_log("STATS", "=== MARKET MAKER STATS ===", icon="📊")
            if state.stoploss_triggered:
                print(f"{Fore.RED}  ⚠️  STOPLOSS ACTIVE - UNWINDING POSITIONS{Style.RESET_ALL}")
            print(f"{Fore.CYAN}  Mid Price: ${state.mid_price:.4f}{Style.RESET_ALL}")
            print(
                f"{Fore.CYAN}  Inventory (norm): {state.inventory:.4f} | raw: {state.inventory_raw:.4f} "
                f"| order_size_ref: {state.inventory_order_size:.4f}{Style.RESET_ALL}"
            )
            print(
                f"{Fore.CYAN}  Bid: ${state.bid_price:.4f} | Ask: ${state.ask_price:.4f}{Style.RESET_ALL}"
            )
            print(
                f"{Fore.CYAN}  Orders Placed: BUY={state.orders_placed['bid']} "
                f"SELL={state.orders_placed['ask']}{Style.RESET_ALL}"
            )
            print(
                f"{Fore.CYAN}  Errors: BUY={state.errors['bid']} SELL={state.errors['ask']}{Style.RESET_ALL}"
            )
            print(f"{Fore.CYAN}========================{Style.RESET_ALL}\n")


async def cancel_all_existing_orders(symbol):
    """Cancel all existing orders for a symbol at startup"""
    console_log("STARTUP", "Checking for existing open orders...")

    # Get all open orders
    open_orders = get_open_orders(SOL_WALLET)

    # Filter by symbol
    symbol_orders = [order for order in open_orders if order.get('symbol') == symbol]

    if not symbol_orders:
        console_log("STARTUP", f"No existing orders found for {symbol}", icon="✅")
        return

    console_log(
        "STARTUP",
        f"Found {len(symbol_orders)} existing orders for {symbol}, cancelling in parallel...",
        icon="🧹",
    )

    async def cancel_single_order(order):
        """Cancel a single order asynchronously"""
        client_order_id = order.get('client_order_id')
        if not client_order_id:
            return False

        try:
            # Run the synchronous cancel_order in executor to avoid blocking
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, cancel_order, symbol, client_order_id)
            if response.status_code == 200:
                console_log("STARTUP", f"Cancelled order {client_order_id[:8]}...", icon="✅")
                return True
            else:
                console_log(
                    "STARTUP",
                    f"Failed to cancel {client_order_id[:8]}...",
                    icon="⚠️",
                    color=Fore.YELLOW,
                )
                return False
        except Exception as e:
            console_log("STARTUP", f"Error cancelling {client_order_id[:8]}...: {e}", icon="❌", color=Fore.RED)
            return False

    # Cancel all orders in parallel
    tasks = [cancel_single_order(order) for order in symbol_orders]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    cancelled_count = sum(1 for r in results if r is True)
    failed_count = len(results) - cancelled_count

    console_log(
        "STARTUP",
        f"Cancelled {cancelled_count} orders, {failed_count} failed",
        icon="",
    )


async def cancel_all_orders(state):
    """Cancel all active orders in parallel - used during shutdown"""
    if not state.active_orders:
        console_log("SHUTDOWN", "No active orders to cancel", icon="✅")
        return

    console_log(
        "SHUTDOWN",
        f"Cancelling {len(state.active_orders)} open orders in parallel...",
        icon="🧹",
    )

    async def cancel_single_order(symbol, client_order_id):
        """Cancel a single order asynchronously"""
        try:
            # Run the synchronous cancel_order in executor to avoid blocking
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, cancel_order, symbol, client_order_id)
            if response.status_code == 200:
                console_log("SHUTDOWN", f"Cancelled order {client_order_id[:8]}...", icon="✅")
                return True
            else:
                console_log(
                    "SHUTDOWN",
                    f"Failed to cancel {client_order_id[:8]}... (may already be filled)",
                    icon="⚠️",
                    color=Fore.YELLOW,
                )
                return False
        except Exception as e:
            console_log("SHUTDOWN", f"Error cancelling {client_order_id[:8]}...: {e}", icon="❌", color=Fore.RED)
            return False

    # Cancel all orders in parallel
    tasks = [cancel_single_order(symbol, client_order_id) for symbol, client_order_id in state.active_orders]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    cancelled_count = sum(1 for r in results if r is True)
    failed_count = len(results) - cancelled_count

    console_log(
        "SHUTDOWN",
        f"Cancelled {cancelled_count} orders, {failed_count} failed",
        icon="",
    )
    state.active_orders.clear()


async def main():
    setup_logging()
    logger.info("Starting market maker initialization...")
    console_log("INIT", "Starting market maker initialization...")

    # Load configuration
    try:
        config = load_trading_config()
    except Exception as e:
        console_log("ERROR", f"Failed to load config: {e}")
        return

    symbol = config.get('symbol', 'UNI')
    notional = config.get('notional_amount', 20.0)
    order_lifetime = config.get('order_lifetime_seconds', 10)
    stoploss_config = config.get('stoploss', {'enabled': False})
    periodic_cancel_config = config.get('periodic_cancel_all', {'enabled': True, 'interval_seconds': 60})

    logger.info("Config loaded | symbol=%s notional=%s order_lifetime=%s", symbol, notional, order_lifetime)
    logger.info("Stoploss config | enabled=%s minimum_equity=%s",
                stoploss_config.get('enabled'),
                stoploss_config.get('minimum_equity_usd'))
    logger.info("Periodic cancel-all config | enabled=%s interval=%s",
                periodic_cancel_config.get('enabled'),
                periodic_cancel_config.get('interval_seconds'))

    # Load spread parameters
    try:
        params = load_spread_parameters(symbol)
    except Exception as e:
        console_log("ERROR", f"Failed to load spread parameters: {e}")
        return

    market_specs = get_market_specs(symbol, params)
    order_size_reference = compute_reference_order_size(notional, params, market_specs)

    logger.info(
        "Reference order size computed | symbol=%s notional=%.6f ref_price=%.6f order_size=%.6f",
        symbol,
        notional,
        params.get('latest_mid_price'),
        order_size_reference,
    )

    logger.info(
        "Spread params summary | gamma=%s tau=%s tick_size=%s",
        params.get("gamma"),
        params.get("tau"),
        params.get("tick_size"),
    )

    # Initialize shared state
    state = SharedState()
    state.inventory_order_size = order_size_reference

    # Bootstrap inventory so the first snapshot is visible immediately
    try:
        await bootstrap_initial_inventory(state, symbol, order_size_reference)
    except Exception as exc:
        logger.exception("Bootstrap inventory failed | symbol=%s", symbol)
        console_log(
            "INVENTORY",
            f"Bootstrap failed ({exc}); defaulting to zeroed inventory",
            icon="⚠️",
            color=Fore.YELLOW,
        )

    # Handle graceful shutdown for Ctrl+C (SIGINT) and docker compose down (SIGTERM)
    def signal_handler(sig, frame):
        print()
        console_log("SHUTDOWN", f"Signal {sig} received, stopping market maker...")
        state.running = False

    try:
        signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
        signal.signal(signal.SIGTERM, signal_handler)  # docker compose down
    except Exception as e:
        console_log("WARN", f"Could not set signal handlers: {e}")

    # Handle leverage configuration
    desired_leverage = config.get('leverage')
    if desired_leverage is not None:
        console_log("INIT", f"Configuring leverage for {symbol}...", icon="⚙️")

        # Get market info to check max leverage
        market_info = get_market_info(symbol)
        if market_info:
            max_leverage = market_info.get('max_leverage', 50)

            if desired_leverage > max_leverage:
                console_log(
                    "WARN",
                    f"Desired leverage {desired_leverage}x exceeds max {max_leverage}x, using {max_leverage}x",
                    icon="⚠️",
                    color=Fore.YELLOW
                )
                desired_leverage = max_leverage

            # Check current leverage
            current_settings = get_account_leverage_settings(SOL_WALLET)
            current_leverage = None
            for setting in current_settings:
                if setting.get('symbol') == symbol:
                    current_leverage = setting.get('leverage')
                    break

            if current_leverage is None:
                current_leverage = max_leverage  # Default is max leverage

            if current_leverage != desired_leverage:
                console_log(
                    "INIT",
                    f"Setting leverage from {current_leverage}x to {desired_leverage}x",
                    icon=""
                )
                try:
                    response = set_leverage(symbol, desired_leverage)
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("success"):
                            console_log("INIT", f"✅ Leverage set to {desired_leverage}x", icon="")
                            logger.info("Leverage updated | symbol=%s leverage=%d", symbol, desired_leverage)
                        else:
                            error = data.get("error", "Unknown error")
                            console_log("WARN", f"Failed to set leverage: {error}", icon="⚠️", color=Fore.YELLOW)
                            logger.warning("Leverage update failed | symbol=%s error=%s", symbol, error)
                    else:
                        console_log(
                            "WARN",
                            f"Failed to set leverage (HTTP {response.status_code})",
                            icon="⚠️",
                            color=Fore.YELLOW
                        )
                        logger.warning("Leverage update HTTP error | symbol=%s status=%d", symbol, response.status_code)
                except Exception as e:
                    console_log("WARN", f"Error setting leverage: {e}", icon="⚠️", color=Fore.YELLOW)
                    logger.exception("Leverage update exception | symbol=%s", symbol)
            else:
                console_log("INIT", f"Leverage already set to {desired_leverage}x", icon="✅")
        else:
            console_log("WARN", "Could not fetch market info for leverage check", icon="⚠️", color=Fore.YELLOW)

    banner_color = Fore.CYAN
    print(f"\n{banner_color}{'='*70}{Style.RESET_ALL}")
    console_log("INIT", "AVELLANEDA-STOIKOV MARKET MAKER", icon="🚀")
    print(f"{banner_color}{'='*70}{Style.RESET_ALL}")
    print(f"{banner_color}Symbol: {symbol}{Style.RESET_ALL}")
    print(f"{banner_color}Notional: ${notional} per order{Style.RESET_ALL}")
    print(f"{banner_color}Order Lifetime: {order_lifetime}s{Style.RESET_ALL}")
    if desired_leverage is not None:
        print(f"{banner_color}Leverage: {desired_leverage}x{Style.RESET_ALL}")
    print(f"{banner_color}{'='*70}{Style.RESET_ALL}\n")

    # Cancel any existing orders from previous runs
    await cancel_all_existing_orders(symbol)

    # Start all async tasks
    logger.info("Async tasks started for symbol %s", symbol)

    tasks = [
        asyncio.create_task(market_data_updater(state, symbol)),
        asyncio.create_task(inventory_updater(state, symbol)),
        asyncio.create_task(quote_calculator(state, params)),
        asyncio.create_task(order_manager(state, symbol, "bid", notional, market_specs, order_lifetime)),
        asyncio.create_task(order_manager(state, symbol, "ask", notional, market_specs, order_lifetime)),
        asyncio.create_task(stats_reporter(state)),
        asyncio.create_task(equity_monitor(state, stoploss_config)),
    ]

    # Add periodic cancel-all task if enabled
    if periodic_cancel_config.get('enabled', True):
        interval = periodic_cancel_config.get('interval_seconds', 60)
        tasks.append(asyncio.create_task(periodic_cancel_all(state, symbol, interval)))
    else:
        console_log("WARN", "Periodic cancel-all disabled in config", icon="⚠️", color=Fore.YELLOW)

    # Wait for all tasks
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print()
        console_log("SHUTDOWN", "KeyboardInterrupt received")
        state.running = False
    except Exception as e:
        console_log("ERROR", str(e))
        state.running = False
    finally:
        # Cancel all active orders before exiting (in parallel)
        await cancel_all_orders(state)

        # Cancel all async tasks
        for task in tasks:
            if not task.done():
                task.cancel()

        # Wait for tasks to finish cancellation
        await asyncio.gather(*tasks, return_exceptions=True)

        console_log("SHUTDOWN", "Market maker stopped gracefully", icon="✅")


if __name__ == "__main__":
    asyncio.run(main())
