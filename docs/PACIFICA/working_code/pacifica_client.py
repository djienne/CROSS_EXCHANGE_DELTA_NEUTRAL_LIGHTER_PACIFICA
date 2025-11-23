"""
Pacifica DEX client wrapper for hedge bot.
Handles all exchange interactions: positions, orders, market data.
"""
import logging
import os
import sys
import time
import uuid
import asyncio
import requests
import websockets
import json
from pathlib import Path
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from solders.keypair import Keypair
from typing import Optional, Dict, Any, Tuple
from datetime import datetime

# Add parent directory to path for SDK imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from pacifica_sdk.common.constants import REST_URL, WS_URL
from pacifica_sdk.common.utils import sign_message

logger = logging.getLogger(__name__)

class PacificaClient:
    """Simplified Pacifica client for hedge bot operations."""

    def __init__(
        self,
        sol_wallet: str,
        api_public: str,
        api_private: str,
        slippage_bps: int = 50,
        allow_fallback: bool = True
    ):
        self.sol_wallet = sol_wallet
        self.api_public = api_public
        self.agent_keypair = Keypair.from_base58_string(api_private)
        self.slippage_bps = slippage_bps
        self.allow_fallback = allow_fallback

        # Cache for market info
        self._market_info: Dict[str, Any] = {}
        self._market_info_decimal: Dict[str, Dict[str, Decimal]] = {}
        self._load_market_info()

    def _load_market_info(self):
        """Load market information for BTC-PERP and ETH-PERP."""
        url = f"{REST_URL}/info"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()

            if not data.get("success"):
                raise ValueError("API call to /info was not successful")

            markets = data.get("data", [])

            for market in markets:
                symbol = market.get("symbol")
                if symbol in ["BTC", "ETH"]:
                    # Store float versions for backward compatibility
                    tick_size_raw = market.get("tick_size", 0.1)
                    lot_size_raw = market.get("lot_size", 0.001)

                    # Clean up tick size to avoid floating point errors
                    tick_size_cleaned = round(float(tick_size_raw), 12)
                    tick_size_dec = Decimal(str(tick_size_cleaned))

                    # Ensure positive
                    if tick_size_dec <= 0:
                        tick_size_dec = Decimal('0.000001')

                    lot_size_dec = Decimal(str(lot_size_raw))
                    if lot_size_dec <= 0:
                        lot_size_dec = Decimal('0.001')

                    self._market_info[symbol] = {
                        "tick_size": float(tick_size_dec),
                        "lot_size": float(lot_size_dec),
                        "min_notional": float(market.get("min_notional", 10.0)),
                        "max_leverage": int(market.get("max_leverage", 20))
                    }

                    # Store Decimal versions for precise rounding
                    self._market_info_decimal[symbol] = {
                        "tick_size_dec": tick_size_dec,
                        "lot_size_dec": lot_size_dec
                    }

            if not self._market_info:
                 raise ValueError("Failed to load BTC-PERP or ETH-PERP market info from API")

        except Exception as e:
            print(f"Warning: Failed to load market info from API: {e}. Using fallback values.")

            if not self.allow_fallback:
                raise RuntimeError("Failed to load market info from API") from e

            fallback_specs = {
                "BTC": {"tick_size": 0.1, "lot_size": 0.001, "min_notional": 10.0, "max_leverage": 20},
                "ETH": {"tick_size": 0.01, "lot_size": 0.01, "min_notional": 10.0, "max_leverage": 20}
            }

            for symbol, specs in fallback_specs.items():
                tick_size_dec = Decimal(str(specs["tick_size"]))
                lot_size_dec = Decimal(str(specs["lot_size"]))

                self._market_info[symbol] = {
                    "tick_size": specs["tick_size"],
                    "lot_size": specs["lot_size"],
                    "min_notional": specs["min_notional"],
                    "max_leverage": specs["max_leverage"]
                }
                self._market_info_decimal[symbol] = {
                    "tick_size_dec": tick_size_dec,
                    "lot_size_dec": lot_size_dec
                }
            
            if not self._market_info:
                 raise RuntimeError(f"Failed to load market info from API and fallback failed.")

    def get_tick_size(self, symbol: str) -> float:
        """Get tick size for symbol."""
        return self._market_info.get(symbol, {}).get("tick_size", 0.1)

    def get_lot_size(self, symbol: str) -> float:
        """Get lot size for symbol."""
        return self._market_info.get(symbol, {}).get("lot_size", 0.001)

    def get_min_notional(self, symbol: str) -> float:
        """Get minimum notional for symbol."""
        return self._market_info.get(symbol, {}).get("min_notional", 10.0)

    def get_max_leverage(self, symbol: str) -> int:
        """Get max leverage for symbol."""
        return self._market_info.get(symbol, {}).get("max_leverage", 20)

    def round_price(self, price: float, symbol: str, side: str = "bid") -> float:
        """
        Round price to tick size using Decimal for precision.

        Args:
            price: Price to round
            symbol: Trading symbol
            side: 'bid' (round down) or 'ask' (round up)

        Returns:
            Rounded price as float
        """
        if symbol not in self._market_info_decimal:
            # Fallback to simple rounding if no Decimal info
            tick = self.get_tick_size(symbol)
            return round(price / tick) * tick

        tick_size_dec = self._market_info_decimal[symbol]["tick_size_dec"]
        price_dec = Decimal(str(price))

        try:
            # For bids, round down. For asks, round up.
            rounding_mode = ROUND_DOWN if side == "bid" else ROUND_UP
            rounded = price_dec.quantize(tick_size_dec, rounding=rounding_mode)
            return float(rounded)
        except Exception:
            # Fallback to simple rounding
            tick = float(tick_size_dec)
            return round(price / tick) * tick

    def round_quantity(self, qty: float, symbol: str) -> float:
        """
        Round quantity to lot size using Decimal for precision.
        Always rounds down to avoid exceeding position limits.

        Args:
            qty: Quantity to round
            symbol: Trading symbol

        Returns:
            Rounded quantity as float
        """
        if symbol not in self._market_info_decimal:
            # Fallback to simple rounding if no Decimal info
            lot = self.get_lot_size(symbol)
            return round(qty / lot) * lot

        lot_size_dec = self._market_info_decimal[symbol]["lot_size_dec"]
        qty_dec = Decimal(str(qty))

        try:
            # Always round down for quantity to avoid exceeding limits
            lots = (qty_dec / lot_size_dec).quantize(Decimal('1'), rounding=ROUND_DOWN)
            rounded = (lots * lot_size_dec).normalize()
            return float(rounded)
        except Exception:
            # Fallback to simple rounding
            lot = float(lot_size_dec)
            return round(qty / lot) * lot

    def get_equity(self) -> float:
        """Get available account equity in USD."""
        url = f"{REST_URL}/account"
        params = {"account": self.sol_wallet}

        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            if not data.get("success"):
                raise ValueError("get_equity API call was not successful")

            account_data = data.get("data", {})
            # Return available balance (equity = balance + unrealized PnL)
            balance = float(account_data.get("balance", 0))
            unrealized_pnl = float(account_data.get("unrealized_pnl", 0))
            return balance + unrealized_pnl

        except Exception as e:
            print(f"Warning: Failed to get equity from API: {e}. Using fallback value of 10000.")
            if self.allow_fallback:
                return 10000.0
            raise RuntimeError("Failed to get equity from API") from e

    async def get_mark_price_ws(self, symbol: str) -> float:
        """Get current mark price via WebSocket."""
        async with websockets.connect(WS_URL) as ws:
            # Subscribe to prices
            subscribe_msg = {
                "method": "subscribe",
                "params": {
                    "source": "prices",
                    "symbol": symbol
                }
            }
            await ws.send(json.dumps(subscribe_msg))

            # Wait for price data
            timeout = 10
            start = time.time()
            while time.time() - start < timeout:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    data = json.loads(msg)

                    if data.get("channel") == "prices":
                        price_data = data.get("data", [])
                        for item in price_data:
                            if item.get("symbol") == symbol:
                                mark_price = float(item.get("mark", 0))
                                if mark_price > 0:
                                    return mark_price
                except asyncio.TimeoutError:
                    continue

            raise RuntimeError(f"Failed to get mark price for {symbol}")

    def get_mark_price(self, symbol: str) -> float:
        """Get current mark price (sync wrapper)."""
        try:
            return asyncio.run(self.get_mark_price_ws(symbol))
        except Exception as e:
            print(f"Warning: Failed to get mark price for {symbol} from API: {e}. Using fallback value.")
            if not self.allow_fallback:
                raise RuntimeError(f"Failed to get mark price for {symbol}") from e
            if symbol == "BTC":
                return 60000.0
            elif symbol == "ETH":
                return 3000.0
            else:
                return 0.0

    def get_position(self, symbol: str) -> Dict[str, Any]:
        """Get current position for symbol."""
        url = f"{REST_URL}/positions"
        params = {"account": self.sol_wallet}

        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            if not data.get("success"):
                raise ValueError("get_position API call was not successful")

            positions = data.get("data", [])
            logger.info(f"Received {len(positions)} positions from API.")

            # Find position for symbol
            for pos in positions:
                if pos.get("symbol") == symbol:
                    logger.info(f"Raw position data for {symbol}: {pos}")
                    opened_at = self._parse_position_timestamp(pos)
                    qty = float(pos.get("amount", 0))
                    if pos.get("side") == "ask":
                        qty = -qty
                    
                    entry_price = float(pos.get("entry_price", 0))
                    mark_price = self.get_mark_price(symbol)
                    
                    unrealized_pnl = 0
                    if entry_price > 0:
                        unrealized_pnl = (mark_price - entry_price) * qty

                    return {
                        "qty": qty,
                        "entry_price": entry_price,
                        "unrealized_pnl": unrealized_pnl,
                        "notional": abs(qty * entry_price),
                        "opened_at": opened_at
                    }

            # No position
            return {
                "qty": 0.0,
                "entry_price": 0.0,
                "unrealized_pnl": 0.0,
                "notional": 0.0,
                "opened_at": None
            }

        except Exception as e:
            print(f"Warning: Failed to get position for {symbol} from API: {e}. Using fallback value.")
            if not self.allow_fallback:
                raise RuntimeError(f"Failed to get position for {symbol}") from e
            if symbol == "BTC":
                return {
                    "qty": 0.1,
                    "entry_price": 60000.0,
                    "unrealized_pnl": 0.0,
                    "notional": 6000.0,
                    "opened_at": None
                }
            else:
                return {
                    "qty": 0.0,
                    "entry_price": 0.0,
                    "unrealized_pnl": 0.0,
                    "notional": 0.0,
                    "opened_at": None
                }

    def _parse_position_timestamp(self, pos: Dict[str, Any]) -> Optional[float]:
        """
        Extract a Unix timestamp (seconds) from position payload if available.

        Supports multiple possible field names and formats returned by the API.
        Returns None when no timestamp is present or parsing fails.
        """
        timestamp_fields = [
            "opened_at",
            "open_time",
            "created_at",
            "updated_at",
            "timestamp",
            "createdAt",
            "updatedAt"
        ]

        for field in timestamp_fields:
            if field not in pos:
                continue
            raw = pos.get(field)
            if raw is None:
                continue

            # Numeric types (seconds / milliseconds / microseconds)
            if isinstance(raw, (int, float)):
                value = float(raw)
                if value > 1e12:  # assume milliseconds
                    return value / 1000.0
                if value > 1e10:  # assume microseconds
                    return value / 1e6
                return value

            # Attempt to parse ISO strings
            if isinstance(raw, str):
                cleaned = raw.strip()
                if not cleaned:
                    continue
                try:
                    # Replace trailing Z with UTC designator compatible with fromisoformat
                    if cleaned.endswith("Z"):
                        cleaned = cleaned[:-1] + "+00:00"
                    dt = datetime.fromisoformat(cleaned)
                    return dt.timestamp()
                except Exception:
                    try:
                        value = float(cleaned)
                        if value > 1e12:
                            return value / 1000.0
                        if value > 1e10:
                            return value / 1e6
                        return value
                    except Exception:
                        continue

        return None

    def place_limit_order(
        self,
        symbol: str,
        side: str,  # "bid" or "ask"
        quantity: float,
        price: float,
        reduce_only: bool = False,
        post_only: bool = True
    ) -> str:
        """
        Place a limit order on Pacifica.

        Args:
            symbol: Trading symbol
            side: 'bid' or 'ask'
            quantity: Order quantity (will be rounded to lot size)
            price: Order price (will be rounded to tick size)
            reduce_only: Whether order should only reduce position
            post_only: If True, uses ALO (Add Liquidity Only) for maker-only orders

        Returns:
            order_id: The order ID returned by the exchange
        """
        url = f"{REST_URL}/orders/create"

        # Round to exchange precision using Decimal
        # Convert to Decimal for precise rounding
        if symbol in self._market_info_decimal:
            tick_size_dec = self._market_info_decimal[symbol]["tick_size_dec"]
            lot_size_dec = self._market_info_decimal[symbol]["lot_size_dec"]

            price_dec = Decimal(str(price))
            qty_dec = Decimal(str(quantity))

            # Round price based on side
            rounding_mode = ROUND_DOWN if side == "bid" else ROUND_UP
            price_dec = price_dec.quantize(tick_size_dec, rounding=rounding_mode)

            # Round quantity (always down)
            lots = (qty_dec / lot_size_dec).quantize(Decimal('1'), rounding=ROUND_DOWN)
            qty_dec = (lots * lot_size_dec).normalize()

            # Normalize to clean string representation
            price_str = str(price_dec.normalize())
            amount_str = str(qty_dec)
        else:
            # Fallback to float rounding
            price_rounded = self.round_price(price, symbol, side)
            qty_rounded = self.round_quantity(quantity, symbol)
            price_str = str(price_rounded)
            amount_str = str(qty_rounded)

        # Generate client order ID
        client_order_id = str(uuid.uuid4())

        # Create signature
        timestamp = int(time.time() * 1000)
        signature_header = {
            "timestamp": timestamp,
            "expiry_window": 5000,
            "type": "create_order"
        }

        # Use ALO (Add Liquidity Only) for post-only orders, GTC otherwise
        tif = "ALO" if post_only else "GTC"

        signature_payload = {
            "symbol": symbol,
            "price": price_str,
            "amount": amount_str,
            "side": side,
            "tif": tif,
            "client_order_id": client_order_id,
            "reduce_only": reduce_only
        }

        signature = sign_message(signature_header, signature_payload, self.agent_keypair)

        # Build full payload
        full_payload = {
            "account": self.sol_wallet,
            "agent_wallet": self.api_public,
            "signature": signature,
            "timestamp": timestamp,
            "expiry_window": 5000,
            **signature_payload
        }

        try:
            response = requests.post(url, json=full_payload, timeout=10, headers={"Content-Type": "application/json"})
            response.raise_for_status()
            result = response.json()

            if not result.get("success"):
                error = result.get("error", "Unknown error")
                raise ValueError(f"Order rejected: {error}")

            order_id = result.get("data", {}).get("order_id")
            if not order_id:
                raise ValueError(f"No order_id in response: {result}")

            return order_id

        except Exception as e:
            raise RuntimeError(f"Failed to place order: {e}")

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order by ID."""
        url = f"{REST_URL}/orders/cancel"

        # Create signature
        timestamp = int(time.time() * 1000)
        signature_header = {
            "timestamp": timestamp,
            "expiry_window": 5000,
            "type": "cancel_order"
        }

        signature_payload = {
            "symbol": symbol,
            "order_id": order_id
        }

        signature = sign_message(signature_header, signature_payload, self.agent_keypair)

        # Build full payload
        full_payload = {
            "account": self.sol_wallet,
            "agent_wallet": self.api_public,
            "signature": signature,
            "timestamp": timestamp,
            "expiry_window": 5000,
            **signature_payload
        }

        try:
            response = requests.post(url, json=full_payload, timeout=10)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"Warning: Failed to cancel order {order_id}: {e}")
            return False

    def place_market_order(
        self,
        symbol: str,
        side: str,  # "buy" or "sell"
        quantity: float,
        reduce_only: bool = False,
    ) -> str:
        """
        Place a market order on Pacifica.
        Args:
            symbol: Trading symbol
            side: 'buy' or 'sell'
            quantity: Order quantity (will be rounded to lot size)
            reduce_only: Whether order should only reduce position
        Returns:
            order_id: The order ID returned by the exchange
        """
        url = f"{REST_URL}/orders/create_market"

        # Round quantity to exchange precision
        if symbol in self._market_info_decimal:
            lot_size_dec = self._market_info_decimal[symbol]["lot_size_dec"]
            qty_dec = Decimal(str(quantity))
            lots = (qty_dec / lot_size_dec).quantize(Decimal('1'), rounding=ROUND_DOWN)
            qty_dec = (lots * lot_size_dec).normalize()
            amount_str = str(qty_dec)
        else:
            qty_rounded = self.round_quantity(quantity, symbol)
            amount_str = str(qty_rounded)

        # Generate client order ID
        client_order_id = str(uuid.uuid4())

        # Create signature
        timestamp = int(time.time() * 1000)
        signature_header = {
            "timestamp": timestamp,
            "expiry_window": 5000,
            "type": "create_market_order"
        }

        signature_payload = {
            "symbol": symbol,
            "amount": amount_str,
            "side": "bid" if side == "buy" else "ask",
            "client_order_id": client_order_id,
            "reduce_only": reduce_only,
            "slippage_percent": str(self.slippage_bps / 100),  # Convert bps to percent
        }

        _message, signature = sign_message(signature_header, signature_payload, self.agent_keypair)

        # Build full payload
        full_payload = {
            "account": self.sol_wallet,
            "agent_wallet": self.api_public,
            "signature": signature,
            "timestamp": timestamp,
            "expiry_window": 5000,
            **signature_payload
        }

        try:
            response = requests.post(url, json=full_payload, timeout=10, headers={"Content-Type": "application/json"})
            response.raise_for_status()
            result = response.json()

            if not result.get("success"):
                error = result.get("error", "Unknown error")
                raise ValueError(f"Order rejected: {error}")

            order_id = result.get("data", {}).get("order_id")
            if not order_id:
                raise ValueError(f"No order_id in response: {result}")

            return order_id

        except Exception as e:
            raise RuntimeError(f"Failed to place market order: {e}")

    def wait_fills_or_cancel(self, symbol: str, order_id: str, ttl_ms: int = 2500):
        """
        Wait for order fills or cancel after TTL.
        Accepts partial fills.
        """
        time.sleep(ttl_ms / 1000.0)
        self.cancel_order(symbol, order_id)

    def cancel_all_orders(self, symbol: Optional[str] = None):
        """Cancel all open orders, optionally for a specific symbol."""
        url = f"{REST_URL}/orders/cancel_all"

        timestamp = int(time.time() * 1000)
        signature_header = {
            "timestamp": timestamp,
            "expiry_window": 5000,
            "type": "cancel_all_orders"
        }

        signature_payload = {
            "all_symbols": symbol is None,
            "exclude_reduce_only": False
        }
        if symbol:
            signature_payload["symbol"] = symbol

        _message, signature = sign_message(signature_header, signature_payload, self.agent_keypair)

        request_header = {
            "account": self.sol_wallet,
            "agent_wallet": self.api_public,
            "signature": signature,
            "timestamp": signature_header["timestamp"],
            "expiry_window": signature_header["expiry_window"],
        }

        full_payload = {**request_header, **signature_payload}

        try:
            response = requests.post(url, json=full_payload, timeout=10)
            response.raise_for_status()
        except Exception as e:
            print(f"Warning: Failed to cancel all orders: {e}")
