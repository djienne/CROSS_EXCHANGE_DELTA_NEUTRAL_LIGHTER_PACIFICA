#!/usr/bin/env python3
"""
lighter_pacifica_hedge.py
-----------------------------
Automated delta-neutral funding rate arbitrage bot for Lighter and Pacifica.

This bot continuously:
1. Analyzes funding rates across multiple symbols on Lighter and Pacifica.
2. Opens a delta-neutral position (long on one, short on the other) to capture the funding rate spread.
3. Holds the position for a configurable duration (e.g., 8 hours) to collect funding payments.
4. Closes the position.
5. Waits for a brief period and repeats the cycle.

Features:
- Persistent state management to survive restarts.
- Automatic recovery from crashes by checking on-chain positions.
- PnL tracking and health monitoring during the holding period.
- Graceful shutdown handling.
"""

import asyncio
import argparse
import json
import logging
import os
import signal
import sys
import time
import requests
from datetime import datetime, timedelta, UTC
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, asdict

from dotenv import load_dotenv

# Import Lighter SDK
import lighter

# Import exchange connectors
from pacifica_client import PacificaClient
from lighter_client import (
    BalanceFetchError,
    get_lighter_balance,
    get_lighter_market_details,
    get_lighter_best_bid_ask,
    get_lighter_open_size,
    get_lighter_position_pnl,
    get_lighter_funding_rate,
    lighter_place_aggressive_order,
    lighter_close_position
)
from pacifica_sdk.common.constants import REST_URL

# ANSI color codes for console output
class Colors:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    GRAY = '\033[90m'

# ==================== Logging Setup ====================

os.makedirs('logs', exist_ok=True)
log_path = os.path.join('logs', 'lighter_pacifica_hedge.log')
try:
    if os.path.exists(log_path):
        os.remove(log_path)
except Exception:
    pass
file_handler = logging.FileHandler(log_path, mode='w', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s'))

# Use UTF-8 encoding for console output to support emojis
import io
console_stream = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
console_handler = logging.StreamHandler(console_stream)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
logging.basicConfig(level=logging.DEBUG, handlers=[file_handler, console_handler], force=True)
logger = logging.getLogger(__name__)

# Silence noisy third-party loggers
logging.getLogger('websockets').setLevel(logging.WARNING)
logging.getLogger('websockets.client').setLevel(logging.WARNING)
logging.getLogger('websockets.server').setLevel(logging.WARNING)
logging.getLogger('websockets.protocol').setLevel(logging.WARNING)
logging.getLogger('asyncio').setLevel(logging.WARNING)
logging.getLogger('lighter').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('aiohttp').setLevel(logging.WARNING)

# ==================== State Management ====================

class BotState:
    """State machine for the hedge bot."""
    IDLE = "IDLE"
    ANALYZING = "ANALYZING"
    OPENING = "OPENING"
    HOLDING = "HOLDING"
    CLOSING = "CLOSING"
    WAITING = "WAITING"
    ERROR = "ERROR"
    SHUTDOWN = "SHUTDOWN"

@dataclass
class BotConfig:
    """Bot configuration parameters."""
    symbols_to_monitor: List[str]
    leverage: int = 3
    base_capital_allocation: float = 100.0
    hold_duration_hours: float = 8.0
    wait_between_cycles_minutes: float = 5.0
    check_interval_seconds: int = 60
    min_net_apr_threshold: float = 5.0

    @staticmethod
    def load_from_file(config_file: str) -> 'BotConfig':
        """Load configuration from JSON file."""
        try:
            with open(config_file, 'r') as f:
                data = json.load(f)

            # Filter out comment keys before initializing
            data = {k: v for k, v in data.items() if not k.startswith('comment_')}

            # Provide defaults for any missing fields
            defaults = {
                'symbols_to_monitor': ["BTC", "ETH", "SOL"],
                'leverage': 3,
                'base_capital_allocation': 100.0,
                'hold_duration_hours': 8.0,
                'wait_between_cycles_minutes': 5.0,
                'check_interval_seconds': 60,
                'min_net_apr_threshold': 5.0,
            }
            # Remove old keys if they exist
            data.pop('stop_loss_percent', None)
            data.pop('enable_stop_loss', None)
            # Handle rename of notional_per_position to base_capital_allocation
            if 'notional_per_position' in data:
                data['base_capital_allocation'] = data.pop('notional_per_position')
            for key, default_value in defaults.items():
                if key not in data:
                    data[key] = default_value
            return BotConfig(**data)
        except FileNotFoundError:
            logger.warning(f"Config file {config_file} not found, using defaults.")
            return BotConfig(symbols_to_monitor=["BTC", "ETH", "SOL"])
        except Exception as e:
            logger.error(f"Error loading config: {e}, using defaults.")
            return BotConfig(symbols_to_monitor=["BTC", "ETH", "SOL"])

class StateManager:
    """Manages bot state persistence and recovery."""
    def __init__(self, state_file: str = "bot_state.json"):
        self.state_file = state_file
        self.state = {
            "version": "1.0",
            "state": BotState.IDLE,
            "current_cycle_number": 0,
            "current_position": None,
            "completed_cycles": [],
            "cumulative_stats": {
                "total_cycles": 0,
                "successful_cycles": 0,
                "failed_cycles": 0,
                "total_realized_pnl": 0.0,
                "last_error": None,
                "last_error_at": None
            },
            "initial_capital": None,
            "last_updated": datetime.now(UTC).isoformat()
        }

    def load(self) -> bool:
        if not os.path.exists(self.state_file):
            logger.info(f"No state file found at {self.state_file}, starting fresh.")
            return False
        try:
            with open(self.state_file, 'r') as f:
                loaded_state = json.load(f)
            self.state.update(loaded_state)
            # Ensure backward compatibility for cycle number
            if "current_cycle_number" not in self.state:
                self.state["current_cycle_number"] = 0
            logger.info(f"Loaded state from {self.state_file}. Current state: {self.state['state']}")
            return True
        except Exception as e:
            logger.warning(f"Could not load state file: {e}. Starting fresh.")
            return False

    def save(self):
        self.state["last_updated"] = datetime.now(UTC).isoformat()
        try:
            temp_file = self.state_file + ".tmp"
            with open(temp_file, 'w') as f:
                json.dump(self.state, f, indent=2)
            os.replace(temp_file, self.state_file)
            logger.debug(f"Saved state to {self.state_file}")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def set_state(self, new_state: str):
        logger.info(f"State transition: {self.state['state']} -> {new_state}")
        self.state["state"] = new_state
        self.save()

    def get_state(self) -> str:
        return self.state["state"]

# ==================== Balance & Position Helpers ====================

def get_pacifica_balance(client: PacificaClient) -> Tuple[float, float]:
    """Get Pacifica total and available USD balance."""
    try:
        total = client.get_equity()
        available = client.get_available_balance()
        return total, available
    except Exception as e:
        logger.error(f"Error fetching Pacifica balance: {e}", exc_info=True)
        raise BalanceFetchError(f"Pacifica balance fetch failed: {e}") from e

# ==================== Main Bot Logic ====================

async def fetch_funding_rates(lighter_api_client: lighter.ApiClient, pacifica_client: PacificaClient,
                              symbols: List[str], market_cache: Dict[str, int]) -> List[Dict]:
    """Fetch and compare funding rates for a list of symbols."""
    funding_api = lighter.FundingApi(lighter_api_client)

    results = []
    for i, symbol in enumerate(symbols):
        try:
            # Get Lighter market ID
            if symbol not in market_cache:
                logger.debug(f"Market ID for {symbol} not in cache, skipping")
                continue

            lighter_market_id = market_cache[symbol]

            # Get funding rates
            pacifica_rate_hourly = pacifica_client.get_funding_rate(symbol)
            await asyncio.sleep(0.1)  # Rate limit for Pacifica

            lighter_rate_raw = await get_lighter_funding_rate(funding_api, lighter_market_id)

            # Rate limit: 1 second delay between Lighter API calls (except for last symbol)
            if i < len(symbols) - 1:
                await asyncio.sleep(1.0)

            if lighter_rate_raw is None:
                logger.info(f"No funding rate for {symbol} on Lighter.")
                continue

            # Lighter returns cumulative 8-hour funding; convert to hourly before annualizing.
            lighter_rate_hourly = lighter_rate_raw / 8.0
            lighter_apr = lighter_rate_hourly * 24 * 365 * 100

            # Pacifica returns hourly funding.
            pacifica_apr = pacifica_rate_hourly * 24 * 365 * 100

            # Positive net APR means shorting the higher APR exchange is profitable
            net_apr = abs(lighter_apr - pacifica_apr)

            long_exch = "Lighter" if lighter_apr < pacifica_apr else "Pacifica"
            short_exch = "Pacifica" if long_exch == "Lighter" else "Lighter"

            results.append({
                "symbol": symbol,
                "lighter_apr": lighter_apr,
                "pacifica_apr": pacifica_apr,
                "net_apr": net_apr,
                "long_exch": long_exch,
                "short_exch": short_exch,
                "available": True,
                "lighter_market_id": lighter_market_id,
                "lighter_rate_8h": lighter_rate_raw,
                "pacifica_rate_hourly": pacifica_rate_hourly,
            })
        except Exception as e:
            logger.warning(f"Could not process funding for {symbol}: {e}")

    return results

async def get_position_pnl(lighter_api_client: lighter.ApiClient, pacifica_client: PacificaClient,
                          account_index: int, symbol: str, lighter_market_id: int) -> Dict:
    """Get unrealized PnL from both exchanges."""
    pnl_data = {
        "lighter_unrealized_pnl": 0.0,
        "pacifica_unrealized_pnl": 0.0,
        "total_unrealized_pnl": 0.0,
    }

    try:
        account_api = lighter.AccountApi(lighter_api_client)
        pnl_data["lighter_unrealized_pnl"] = await get_lighter_position_pnl(account_api, account_index, lighter_market_id)
    except Exception as e:
        logger.error(f"Error fetching Lighter PnL: {e}")

    try:
        pacifica_pos = await pacifica_client.get_position(symbol)
        if pacifica_pos:
            pnl_data["pacifica_unrealized_pnl"] = pacifica_pos.get("unrealized_pnl", 0.0)
    except Exception as e:
        logger.error(f"Error fetching Pacifica PnL: {e}")

    pnl_data["total_unrealized_pnl"] = pnl_data["lighter_unrealized_pnl"] + pnl_data["pacifica_unrealized_pnl"]
    return pnl_data

def calculate_dynamic_stop_loss(leverage: int) -> float:
    """Calculate dynamic stop-loss percentage based on leverage."""
    if leverage <= 1:
        return 50.0
    elif leverage == 2:
        return 30.0
    elif leverage == 3:
        return 20.0
    elif leverage == 4:
        return 15.0
    elif leverage == 5:
        return 12.0
    else:
        return max(2.0, 60.0 / leverage)

def check_stop_loss(pnl_data: Dict, notional: float, stop_loss_percent: float) -> Tuple[bool, str]:
    """Check if stop-loss should be triggered based on unrealized PnL."""
    total_unrealized_pnl = pnl_data.get("total_unrealized_pnl", 0.0)

    if notional <= 0:
        return False, ""

    loss_percent = (total_unrealized_pnl / notional) * 100

    if loss_percent <= -stop_loss_percent:
        return True, f"Loss of {loss_percent:.2f}% exceeds stop-loss threshold of -{stop_loss_percent:.2f}%"

    return False, ""

# ==================== State Recovery ====================

async def scan_symbols_for_positions(lighter_api_client: lighter.ApiClient, pacifica_client: PacificaClient,
                                     account_index: int, symbols: List[str], market_cache: Dict[str, int]) -> List[dict]:
    """Scan multiple symbols for open positions on both exchanges."""
    positions_found = []

    logger.info(f"Scanning {len(symbols)} symbols for existing positions: {', '.join(symbols)}")

    account_api = lighter.AccountApi(lighter_api_client)

    for i, symbol in enumerate(symbols, 1):
        try:
            logger.debug(f"[{i}/{len(symbols)}] Checking {symbol}...")

            if symbol not in market_cache:
                logger.debug(f"Market ID for {symbol} not in cache, skipping")
                continue

            lighter_market_id = market_cache[symbol]

            # Check Pacifica position first (faster)
            pacifica_pos = await pacifica_client.get_position(symbol)
            await asyncio.sleep(0.1)  # Rate limit for Pacifica

            # Check Lighter position
            lighter_size = await get_lighter_open_size(account_api, account_index, lighter_market_id, symbol=symbol)

            # Rate limit: 1 second delay between Lighter API calls (except for last symbol)
            if i < len(symbols):
                await asyncio.sleep(1.0)

            pacifica_size = pacifica_pos['qty'] if pacifica_pos else 0.0

            if lighter_size != 0 or pacifica_size != 0:
                logger.info(f"[FOUND] Position on {symbol}: Lighter={lighter_size}, Pacifica={pacifica_size}")
                positions_found.append({
                    "symbol": symbol,
                    "lighter_size": lighter_size,
                    "pacifica_size": pacifica_size,
                    "lighter_market_id": lighter_market_id
                })
            else:
                logger.debug(f"[{i}/{len(symbols)}] {symbol}: No positions")
        except Exception as e:
            logger.debug(f"[{i}/{len(symbols)}] Could not check {symbol}: {e}")

    if positions_found:
        logger.info(f"{Colors.GREEN}Position scan complete: {len(positions_found)} position(s) found{Colors.RESET}")
    else:
        logger.info(f"Position scan complete: No existing positions found")
    return positions_found

async def recover_state(state_mgr: StateManager, lighter_api_client: lighter.ApiClient,
                       pacifica_client: PacificaClient, config: BotConfig, env: dict,
                       market_cache: Dict[str, int]) -> bool:
    """Recover bot state by checking actual positions on exchanges."""
    logger.info(f"{Colors.YELLOW}Performing state recovery...{Colors.RESET}")
    state = state_mgr.get_state()

    account_index = int(env.get("ACCOUNT_INDEX", env.get("LIGHTER_ACCOUNT_INDEX", "0")))

    if state in [BotState.OPENING, BotState.CLOSING]:
        logger.error(f"{Colors.RED}Bot was last in {state} state. Manual intervention required.{Colors.RESET}")
        return False

    try:
        positions_found = await scan_symbols_for_positions(lighter_api_client, pacifica_client, account_index,
                                                           config.symbols_to_monitor, market_cache)

        if not positions_found:
            logger.info(f"{Colors.GREEN}No existing positions found. Resetting to IDLE.{Colors.RESET}")
            state_mgr.state["current_position"] = None
            state_mgr.set_state(BotState.IDLE)
            return True

        if len(positions_found) > 1:
            logger.error(f"{Colors.RED}Multiple positions found on {len(positions_found)} symbols! Manual cleanup required.{Colors.RESET}")
            state_mgr.set_state(BotState.ERROR)
            return False

        # Single position found, attempt recovery
        pos_info = positions_found[0]
        symbol = pos_info["symbol"]
        lighter_size = pos_info["lighter_size"]
        pacifica_size = pos_info["pacifica_size"]
        lighter_market_id = pos_info["lighter_market_id"]

        # Check if it's an orphan leg
        is_orphan_lighter = (lighter_size != 0 and pacifica_size == 0)
        is_orphan_pacifica = (pacifica_size != 0 and lighter_size == 0)

        if is_orphan_lighter or is_orphan_pacifica:
            orphan_exchange = "Lighter" if is_orphan_lighter else "Pacifica"
            orphan_size = lighter_size if is_orphan_lighter else pacifica_size
            logger.warning(f"{Colors.YELLOW}Detected orphan leg on {orphan_exchange} for {symbol}: {orphan_size:+.4f}{Colors.RESET}")
            logger.info(f"{Colors.CYAN}Attempting to close orphan position...{Colors.RESET}")

            try:
                # Close the orphan leg
                if is_orphan_lighter:
                    logger.info(f"Closing orphan Lighter position for {symbol}...")
                    # Get market details and prices
                    order_api = lighter.OrderApi(lighter_api_client)
                    market_id, price_tick, amount_tick = await get_lighter_market_details(order_api, symbol)
                    lighter_bid, lighter_ask = await get_lighter_best_bid_ask(order_api, symbol, market_id)

                    # Determine close side and ref_price
                    if lighter_size > 0:
                        close_side = "sell"
                        ref_price = lighter_bid
                    else:
                        close_side = "buy"
                        ref_price = lighter_ask

                    close_size = abs(lighter_size)

                    # Need SignerClient from env
                    from lighter_client import lighter_close_position
                    signer = lighter.SignerClient(
                        url=env["LIGHTER_BASE_URL"],
                        private_key=env["LIGHTER_PRIVATE_KEY"],
                        account_index=int(env["ACCOUNT_INDEX"]),
                        api_key_index=int(env.get("API_KEY_INDEX", 0)),
                    )
                    signer_check = signer.check_client()
                    if signer_check:
                        raise RuntimeError(f"Lighter SignerClient verification failed during orphan close: {signer_check}")

                    try:
                        lighter_result = await lighter_close_position(
                            signer, market_id, price_tick, amount_tick,
                            close_side, close_size, ref_price
                        )
                    finally:
                        try:
                            await signer.close()
                        except Exception:
                            pass
                    if not lighter_result:
                        raise RuntimeError("Failed to close orphan Lighter position")
                    logger.info(f"{Colors.GREEN}Orphan Lighter position closed{Colors.RESET}")
                else:  # Orphan on Pacifica
                    close_qty = abs(pacifica_size)
                    close_side = 'sell' if pacifica_size > 0 else 'buy'
                    logger.info(f"Closing orphan Pacifica position for {symbol}: {close_qty:.4f} {close_side}...")
                    pacifica_result = pacifica_client.place_market_order(symbol, side=close_side, quantity=close_qty, reduce_only=True)
                    if pacifica_result is None:
                        raise RuntimeError("Failed to close orphan Pacifica position")
                    logger.info(f"{Colors.GREEN}Orphan Pacifica position closed{Colors.RESET}")

                # Reset state to IDLE after successful cleanup
                logger.info(f"{Colors.GREEN}Orphan leg cleaned up successfully. Resetting to IDLE.{Colors.RESET}")
                state_mgr.state["current_position"] = None
                state_mgr.set_state(BotState.IDLE)
                return True

            except Exception as e:
                logger.error(f"{Colors.RED}Failed to close orphan leg: {e}. Manual intervention required.{Colors.RESET}")
                state_mgr.set_state(BotState.ERROR)
                return False

        # Check if it's a valid delta-neutral position
        if abs(lighter_size + pacifica_size) > (abs(max(abs(lighter_size), abs(pacifica_size))) * 0.05):
            logger.error(f"{Colors.RED}Position sizes for {symbol} are not delta-neutral! Lighter: {lighter_size}, Pacifica: {pacifica_size}. Manual cleanup required.{Colors.RESET}")
            state_mgr.set_state(BotState.ERROR)
            return False

        logger.info(f"{Colors.GREEN}Detected delta-neutral position on {symbol}. Recovering state...{Colors.RESET}")

        long_exchange = "Lighter" if lighter_size > 0 else "Pacifica"
        short_exchange = "Pacifica" if long_exchange == "Lighter" else "Lighter"

        # If state was already HOLDING, keep original open time. Otherwise, set to now.
        opened_at_str = (state_mgr.state.get("current_position") or {}).get("opened_at")
        if state == BotState.HOLDING and opened_at_str:
            # Ensure timezone-aware datetime
            opened_at = datetime.fromisoformat(opened_at_str.replace('Z', '+00:00'))
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=UTC)
        else:
            opened_at = datetime.now(UTC)

        target_close_at = opened_at + timedelta(hours=config.hold_duration_hours)

        # Estimate notional value with error handling
        order_api = lighter.OrderApi(lighter_api_client)
        lighter_bid, lighter_ask = await get_lighter_best_bid_ask(order_api, symbol, lighter_market_id)

        if lighter_bid and lighter_ask:
            lighter_mid = (lighter_bid + lighter_ask) / 2
            estimated_notional = abs(lighter_size) * lighter_mid
        else:
            logger.warning(f"Could not get price for {symbol}, estimating notional as 100")
            estimated_notional = 100.0

        # Use configured leverage (Lighter doesn't expose current leverage via API)
        actual_leverage = config.leverage

        state_mgr.state["current_position"] = {
            "symbol": symbol,
            "opened_at": opened_at.isoformat(),
            "target_close_at": target_close_at.isoformat(),
            "long_exchange": long_exchange,
            "short_exchange": short_exchange,
            "notional": estimated_notional,
            "leverage": actual_leverage,
            "lighter_market_id": lighter_market_id
        }
        state_mgr.set_state(BotState.HOLDING)
        logger.info(f"{Colors.GREEN}Position recovered successfully. Now in HOLDING state.{Colors.RESET}")
        return True

    except Exception as e:
        logger.error(f"Error during state recovery: {e}", exc_info=True)
        state_mgr.set_state(BotState.ERROR)
        return False

# ==================== Main Bot Class ====================

class RotationBot:
    def __init__(self, state_file: str, config_file: str):
        self.state_mgr = StateManager(state_file)
        self.config_file = config_file
        self.config = BotConfig.load_from_file(config_file)
        self.shutdown_requested = False

        load_dotenv()
        self.env = {
            "LIGHTER_WS_URL": os.getenv("LIGHTER_WS_URL") or os.getenv("WEBSOCKET_URL") or "wss://mainnet.zklighter.elliot.ai/stream",
            "LIGHTER_BASE_URL": os.getenv("LIGHTER_BASE_URL") or os.getenv("BASE_URL") or "https://mainnet.zklighter.elliot.ai",
            "LIGHTER_PRIVATE_KEY": os.getenv("LIGHTER_PRIVATE_KEY") or os.getenv("API_KEY_PRIVATE_KEY"),
            "ACCOUNT_INDEX": os.getenv("ACCOUNT_INDEX", os.getenv("LIGHTER_ACCOUNT_INDEX", "0")),
            "API_KEY_INDEX": os.getenv("API_KEY_INDEX", os.getenv("LIGHTER_API_KEY_INDEX", "0")),
            "LIGHTER_MARGIN_MODE": (os.getenv("LIGHTER_MARGIN_MODE") or "cross").lower(),
            "SOL_WALLET": os.getenv("SOL_WALLET"),
            "API_PUBLIC": os.getenv("API_PUBLIC"),
            "API_PRIVATE": os.getenv("API_PRIVATE")
        }

        if not all([self.env["LIGHTER_WS_URL"], self.env["LIGHTER_BASE_URL"], self.env["LIGHTER_PRIVATE_KEY"],
                   self.env["SOL_WALLET"], self.env["API_PUBLIC"], self.env["API_PRIVATE"]]):
            logger.error("Missing required environment variables. Please check your .env file.")
            sys.exit(1)

        self.pacifica_client = PacificaClient(self.env["SOL_WALLET"], self.env["API_PUBLIC"], self.env["API_PRIVATE"])
        self.market_cache = {}  # Symbol -> market_id mapping

        # Initialize Lighter SignerClient
        try:
            self.lighter_signer = lighter.SignerClient(
                url=self.env["LIGHTER_BASE_URL"],
                private_key=self.env["LIGHTER_PRIVATE_KEY"],
                account_index=int(self.env["ACCOUNT_INDEX"]),
                api_key_index=int(self.env.get("API_KEY_INDEX", 0)),
            )
            signer_check = self.lighter_signer.check_client()
            if signer_check:
                raise RuntimeError(f"Lighter SignerClient verification failed: {signer_check}")
            logger.info("Lighter SignerClient initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Lighter SignerClient: {e}")
            sys.exit(1)

        logger.debug(
            "Lighter environment: base_url=%s, ws_url=%s, account_index=%s, api_key_index=%s, margin_mode=%s",
            self.env["LIGHTER_BASE_URL"],
            self.env["LIGHTER_WS_URL"],
            self.env["ACCOUNT_INDEX"],
            self.env.get("API_KEY_INDEX", 0),
            self.env.get("LIGHTER_MARGIN_MODE", "cross"),
        )

    def _log_funding_snapshot(self, opportunities: List[Dict], title: str = "Funding Snapshot") -> None:
        if not opportunities:
            return

        header = (
            f"{Colors.BOLD}{Colors.MAGENTA}{title}:{Colors.RESET}\n"
            f"{'Symbol':<10}{'Lighter APR%':>14}{'Pacifica APR%':>16}{'Spread%':>12}{'Long':>10}{'Short':>10}"
        )
        lines = ["", header]
        for opp in sorted(opportunities, key=lambda o: o.get("net_apr", 0), reverse=True):
            lines.append(
                f"{opp['symbol']:<10}{opp['lighter_apr']:+12.2f}{opp['pacifica_apr']:+16.2f}"
                f"{opp['net_apr']:+12.2f}{opp['long_exch'][:10]:>10}{opp['short_exch'][:10]:>10}"
            )
        lines.append("")
        logger.info("\n".join(lines))

    async def emergency_close_all(self, reason: str) -> None:
        """Attempt to close any residual positions on both exchanges."""
        logger.warning(f"{Colors.YELLOW}Emergency close triggered ({reason}). Checking all symbols...{Colors.RESET}")
        lighter_api_client = None
        closed_any = False
        account_index = int(self.env["ACCOUNT_INDEX"])
        try:
            lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.env["LIGHTER_BASE_URL"]))
            order_api = lighter.OrderApi(lighter_api_client)
            account_api = lighter.AccountApi(lighter_api_client)

            symbols = self.config.symbols_to_monitor or list(self.market_cache.keys())
            for symbol in symbols:
                market_id = self.market_cache.get(symbol)
                if market_id is None:
                    try:
                        market_id, _, _ = await get_lighter_market_details(order_api, symbol)
                        self.market_cache[symbol] = market_id
                    except Exception as exc:
                        logger.debug(f"Emergency close: unable to fetch market_id for {symbol}: {exc}")
                        continue

                try:
                    market_id, price_tick, amount_tick = await get_lighter_market_details(order_api, symbol)
                    lighter_size = await get_lighter_open_size(account_api, account_index, market_id, symbol=symbol)
                    if abs(lighter_size) >= amount_tick:
                        lighter_bid, lighter_ask = await get_lighter_best_bid_ask(order_api, symbol, market_id)
                        close_side = "sell" if lighter_size > 0 else "buy"
                        ref_price = lighter_bid if close_side == "sell" else lighter_ask
                        if ref_price:
                            logger.warning(f"Emergency close: closing Lighter {symbol} position ({lighter_size:+.4f})")
                            result = await lighter_close_position(
                                self.lighter_signer,
                                market_id,
                                price_tick,
                                amount_tick,
                                close_side,
                                abs(lighter_size),
                                ref_price,
                                cross_ticks=200,
                            )
                            closed_any = closed_any or bool(result)
                        else:
                            logger.warning(f"Emergency close: no reference price to close Lighter {symbol}")
                except Exception as exc:
                    logger.error(f"Emergency close: failed to close Lighter {symbol}: {exc}")

                try:
                    pac_pos = await self.pacifica_client.get_position(symbol)
                    qty = pac_pos.get("qty", 0.0) if pac_pos else 0.0
                    if qty:
                        close_side = "sell" if qty > 0 else "buy"
                        logger.warning(f"Emergency close: closing Pacifica {symbol} position ({qty:+.4f})")
                        result = self.pacifica_client.place_market_order(
                            symbol,
                            side=close_side,
                            quantity=abs(qty),
                            reduce_only=True,
                        )
                        closed_any = closed_any or bool(result)
                except Exception as exc:
                    logger.error(f"Emergency close: failed to close Pacifica {symbol}: {exc}")

        except Exception as exc:
            logger.error(f"Emergency close aborted due to error: {exc}", exc_info=True)
        finally:
            if lighter_api_client:
                await lighter_api_client.close()

        if closed_any:
            logger.warning(f"{Colors.YELLOW}Emergency close executed. Re-checking state on next cycle.{Colors.RESET}")
        else:
            logger.info("Emergency close completed: no positions required action.")

    async def _update_lighter_leverage(self, market_id: int, leverage: int) -> bool:
        """Set leverage on Lighter using the configured margin mode."""
        margin_mode = self.env.get("LIGHTER_MARGIN_MODE", "cross")
        margin_code = 0 if margin_mode == "cross" else 1
        try:
            _, _, err = await self.lighter_signer.update_leverage(
                market_index=market_id,
                margin_mode=margin_code,
                leverage=int(leverage),
            )
            if err:
                logger.warning(f"Lighter leverage update failed for market {market_id}: {err}")
                return False
            logger.info(f"{Colors.GREEN}Set Lighter leverage to {leverage}x ({margin_mode} margin).{Colors.RESET}")
            return True
        except Exception as e:
            logger.warning(f"Error updating Lighter leverage: {e}")
            return False

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    async def _get_pacifica_24h_volume_usd(self, symbol: str) -> Optional[float]:
        """
        Get 24-hour trading volume in USD for a symbol on Pacifica.

        Returns:
            USD volume or None if failed
        """
        try:
            # Calculate timestamps (24 hours ago to now)
            end_time = int(time.time() * 1000)  # milliseconds
            start_time = end_time - (24 * 60 * 60 * 1000)  # 24 hours ago

            # Fetch klines
            url = f"{REST_URL}/kline"
            params = {
                'symbol': symbol,
                'interval': '1h',  # 1 hour candles
                'start_time': start_time,
                'end_time': end_time
            }

            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            if not data.get('success'):
                return None

            klines = data.get('data', [])

            if not klines:
                return None

            # Sum volume from all klines (volume in base tokens)
            total_volume = sum(float(kline.get('v', 0)) for kline in klines)

            # Get latest close price from most recent kline for USD conversion
            latest_price = float(klines[-1].get('c', 0)) if klines else 0.0

            if latest_price <= 0:
                return None

            # Calculate USD volume
            usd_volume = total_volume * latest_price

            # Rate limit: 0.1 second delay for Pacifica API
            await asyncio.sleep(0.1)

            return usd_volume

        except Exception as e:
            logger.debug(f"Could not fetch 24h volume for {symbol}: {e}")
            return None

    async def _filter_tradable_symbols(self):
        """Filters the config's symbols to only include those present on both exchanges and with sufficient volume."""
        logger.info("Filtering symbols to find those tradable on both exchanges...")

        # Get Lighter symbols from market cache (already populated)
        lighter_symbols = set(self.market_cache.keys())
        pacifica_symbols = set(self.pacifica_client._market_info.keys())

        common_symbols = lighter_symbols.intersection(pacifica_symbols)

        original_symbols = self.config.symbols_to_monitor
        filtered_symbols = [s for s in original_symbols if s in common_symbols]

        if len(filtered_symbols) < len(original_symbols):
            removed_symbols = set(original_symbols) - set(filtered_symbols)
            logger.warning(f"Removed symbols not available on both exchanges: {', '.join(removed_symbols)}")

        # Filter by volume (minimum $50M on Pacifica)
        MIN_VOLUME_USD = 50_000_000  # $50 million
        logger.info(f"Checking 24h trading volume on Pacifica (minimum: ${MIN_VOLUME_USD:,.0f})...")

        volume_filtered_symbols = []
        low_volume_symbols = []

        for symbol in filtered_symbols:
            volume_usd = await self._get_pacifica_24h_volume_usd(symbol)

            if volume_usd is None:
                logger.warning(f"Could not fetch volume for {symbol}, excluding from trading")
                low_volume_symbols.append(f"{symbol} (N/A)")
                continue

            if volume_usd >= MIN_VOLUME_USD:
                volume_filtered_symbols.append(symbol)
                logger.info(f"  {Colors.GREEN}✓{Colors.RESET} {symbol}: ${volume_usd:,.0f} (sufficient volume)")
            else:
                low_volume_symbols.append(f"{symbol} (${volume_usd:,.0f})")
                logger.warning(f"  {Colors.YELLOW}✗{Colors.RESET} {symbol}: ${volume_usd:,.0f} (below threshold)")

        if low_volume_symbols:
            logger.warning(f"Removed {len(low_volume_symbols)} symbols due to low volume: {', '.join(low_volume_symbols)}")

        if not volume_filtered_symbols:
            logger.error("No symbols meet the volume requirements. The bot cannot proceed.")
            sys.exit(1)

        # Filter by spread (maximum 0.15% absolute difference between exchanges)
        MAX_SPREAD_PERCENT = 0.15
        logger.info(f"Checking spreads between exchanges (maximum: {MAX_SPREAD_PERCENT}%)...")

        spread_filtered_symbols = []
        high_spread_symbols = []

        lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.env["LIGHTER_BASE_URL"]))
        try:
            order_api = lighter.OrderApi(lighter_api_client)

            for symbol in volume_filtered_symbols:
                try:
                    # Get market details
                    if symbol not in self.market_cache:
                        logger.warning(f"No market ID for {symbol}, skipping spread check")
                        high_spread_symbols.append(f"{symbol} (N/A)")
                        continue

                    market_id = self.market_cache[symbol]

                    # Get Lighter prices
                    lighter_bid, lighter_ask = await get_lighter_best_bid_ask(order_api, symbol, market_id)
                    await asyncio.sleep(1.0)  # Rate limit

                    # Get Pacifica price
                    pacifica_price = await self.pacifica_client.get_mark_price(symbol)
                    await asyncio.sleep(0.1)  # Rate limit

                    if not lighter_bid or not lighter_ask or pacifica_price <= 0:
                        logger.warning(f"Could not fetch prices for {symbol}, excluding from trading")
                        high_spread_symbols.append(f"{symbol} (N/A)")
                        continue

                    # Calculate spread
                    lighter_mid = (lighter_bid + lighter_ask) / 2
                    spread_pct = abs((lighter_mid - pacifica_price) / pacifica_price) * 100

                    if spread_pct <= MAX_SPREAD_PERCENT:
                        spread_filtered_symbols.append(symbol)
                        logger.info(f"  {Colors.GREEN}✓{Colors.RESET} {symbol}: {spread_pct:.3f}% spread (acceptable)")
                    else:
                        high_spread_symbols.append(f"{symbol} ({spread_pct:.3f}%)")
                        logger.warning(f"  {Colors.YELLOW}✗{Colors.RESET} {symbol}: {spread_pct:.3f}% spread (too high)")

                except Exception as e:
                    logger.warning(f"Error checking spread for {symbol}: {e}")
                    high_spread_symbols.append(f"{symbol} (error)")

        finally:
            await lighter_api_client.close()

        if high_spread_symbols:
            logger.warning(f"Removed {len(high_spread_symbols)} symbols due to high spread: {', '.join(high_spread_symbols)}")

        if not spread_filtered_symbols:
            logger.error("No symbols meet both volume and spread requirements. The bot cannot proceed.")
            sys.exit(1)

        logger.info(f"{Colors.GREEN}Final list of monitored symbols ({len(spread_filtered_symbols)}): {', '.join(spread_filtered_symbols)}{Colors.RESET}")
        self.config.symbols_to_monitor = spread_filtered_symbols

    def _signal_handler(self, signum, frame):
        logger.info(f"\n{Colors.YELLOW}Shutdown signal received. Stopping gracefully...{Colors.RESET}")
        self.shutdown_requested = True

    async def _initialize_market_cache(self):
        """Pre-fetch market IDs for all symbols."""
        logger.info("Initializing Lighter market cache...")
        lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.env["LIGHTER_BASE_URL"]))
        try:
            order_api = lighter.OrderApi(lighter_api_client)
            for i, symbol in enumerate(self.config.symbols_to_monitor):
                try:
                    market_id, price_tick, amount_tick = await get_lighter_market_details(order_api, symbol)
                    self.market_cache[symbol] = market_id
                    logger.debug(f"Cached {symbol}: market_id={market_id}")

                    # Rate limit: 1 second delay between Lighter API calls (except for last symbol)
                    if i < len(self.config.symbols_to_monitor) - 1:
                        await asyncio.sleep(1.0)
                except Exception as e:
                    logger.warning(f"Could not get market details for {symbol} on Lighter: {e}")
        finally:
            await lighter_api_client.close()

        logger.info(f"Market cache initialized: {len(self.market_cache)} symbols")

        # Filter symbols after cache is built (async operation)
        await self._filter_tradable_symbols()

    def reload_config(self):
        """Reload configuration from file."""
        try:
            if self.state_mgr.state.get("current_position") is not None:
                logger.warning(f"{Colors.YELLOW}Config reload attempted while position is open. Skipping reload for safety.{Colors.RESET}")
                return False

            logger.info(f"{Colors.CYAN}Reloading configuration from {self.config_file}...{Colors.RESET}")
            old_config = self.config
            new_config = BotConfig.load_from_file(self.config_file)

            # Check for changes
            changes = []
            if new_config.leverage != old_config.leverage:
                changes.append(f"leverage: {old_config.leverage}x -> {new_config.leverage}x")
            if new_config.base_capital_allocation != old_config.base_capital_allocation:
                changes.append(f"base_capital: ${old_config.base_capital_allocation:.2f} -> ${new_config.base_capital_allocation:.2f}")
            if new_config.hold_duration_hours != old_config.hold_duration_hours:
                changes.append(f"hold_duration: {old_config.hold_duration_hours}h -> {new_config.hold_duration_hours}h")
            if new_config.min_net_apr_threshold != old_config.min_net_apr_threshold:
                changes.append(f"min_apr_threshold: {old_config.min_net_apr_threshold}% -> {new_config.min_net_apr_threshold}%")
            if set(new_config.symbols_to_monitor) != set(old_config.symbols_to_monitor):
                changes.append(f"symbols: {old_config.symbols_to_monitor} -> {new_config.symbols_to_monitor}")

            self.config = new_config

            # Note: Volume filtering is skipped during config reload to avoid delays
            # Volume filtering only happens at startup
            original_symbols = self.config.symbols_to_monitor
            lighter_symbols = set(self.market_cache.keys())
            pacifica_symbols = set(self.pacifica_client._market_info.keys())
            common_symbols = lighter_symbols.intersection(pacifica_symbols)
            self.config.symbols_to_monitor = [s for s in original_symbols if s in common_symbols]

            if changes:
                logger.info(f"{Colors.GREEN}Config reloaded successfully. Changes detected:{Colors.RESET}")
                for change in changes:
                    logger.info(f"   • {change}")
            else:
                logger.info(f"{Colors.GREEN}Config reloaded (no changes detected){Colors.RESET}")

            return True
        except Exception as e:
            logger.error(f"{Colors.RED}Failed to reload config: {e}. Keeping existing configuration.{Colors.RESET}")
            return False

    async def _responsive_sleep(self, duration_seconds: int):
        """Sleeps for a duration in 1-second intervals, checking for shutdown."""
        for _ in range(int(duration_seconds)):
            if self.shutdown_requested:
                logger.info("Sleep interrupted by shutdown signal.")
                break
            await asyncio.sleep(1)

    async def run(self):
        logger.info(f"{Colors.BOLD}{Colors.CYAN}{'='*60}{Colors.RESET}")
        logger.info("  Automated Delta-Neutral Bot for Lighter & Pacifica")
        logger.info(f"{Colors.BOLD}{Colors.CYAN}{'='*60}{Colors.RESET}")

        await self._initialize_market_cache()

        self.state_mgr.load()

        # Initialize base capital if missing
        if self.state_mgr.state.get("initial_capital") is None:
            try:
                logger.info(f"{Colors.YELLOW}Fetching initial capital for long-term PnL tracking...{Colors.RESET}")
                account_index = int(self.env["ACCOUNT_INDEX"])
                _, lighter_total = await get_lighter_balance(self.env["LIGHTER_WS_URL"], account_index)
                pa_total, _ = get_pacifica_balance(self.pacifica_client)
                initial_capital = lighter_total + pa_total
                self.state_mgr.state["initial_capital"] = initial_capital
                self.state_mgr.save()
                logger.info(f"{Colors.GREEN}Initial capital set to: ${initial_capital:.2f}{Colors.RESET}")
            except Exception as e:
                logger.warning(f"Could not fetch initial capital: {e}. Will retry later.")
        else:
            logger.info(f"Initial capital: ${self.state_mgr.state['initial_capital']:.2f}")

        # Create Lighter API client for recovery
        lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.env["LIGHTER_BASE_URL"]))
        try:
            if not await recover_state(self.state_mgr, lighter_api_client, self.pacifica_client, self.config, self.env, self.market_cache):
                logger.error(f"{Colors.RED}State recovery failed. Exiting.{Colors.RESET}")
                return
        finally:
            await lighter_api_client.close()

        # Display funding rates at startup
        logger.info(f"\n{Colors.BOLD}{Colors.YELLOW}Fetching initial funding rates...{Colors.RESET}")
        lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.env["LIGHTER_BASE_URL"]))
        try:
            startup_opportunities = await fetch_funding_rates(lighter_api_client, self.pacifica_client, self.config.symbols_to_monitor, self.market_cache)
            if startup_opportunities:
                self._log_funding_snapshot(startup_opportunities, title="Startup Funding Rate Snapshot")
            else:
                logger.warning("No funding rate data available at startup.")
        except Exception as e:
            logger.warning(f"Could not fetch funding rates at startup: {e}")
        finally:
            await lighter_api_client.close()

        try:
            while not self.shutdown_requested:
                try:
                    state = self.state_mgr.get_state()
                    if state == BotState.IDLE:
                        await self.start_new_cycle()
                    elif state == BotState.HOLDING:
                        await self.monitor_position()
                    elif state == BotState.WAITING:
                        wait_seconds = self.config.wait_between_cycles_minutes * 60
                        logger.info(f"Waiting for {self.config.wait_between_cycles_minutes} minutes...")
                        await self._responsive_sleep(wait_seconds)
                        if not self.shutdown_requested:
                            self.state_mgr.set_state(BotState.IDLE)
                    elif state == BotState.ERROR:
                        logger.error("Bot is in ERROR state. Waiting 5 minutes before attempting recovery.")
                        await self._responsive_sleep(300)
                        if not self.shutdown_requested:
                            lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.env["LIGHTER_BASE_URL"]))
                            try:
                                if not await recover_state(self.state_mgr, lighter_api_client, self.pacifica_client, self.config, self.env, self.market_cache):
                                    logger.error(f"{Colors.RED}Recovery failed again. Manual intervention required.{Colors.RESET}")
                                    break
                            finally:
                                await lighter_api_client.close()
                    else:
                        await self._responsive_sleep(self.config.check_interval_seconds)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.exception(f"An unexpected error occurred in the main loop: {e}")
                    self.state_mgr.set_state(BotState.ERROR)
                    await self.emergency_close_all(f"main loop error: {e}")
                    await self._responsive_sleep(60)
        finally:
            logger.info("Bot shutting down. Performing cleanup...")
            if self.lighter_signer:
                try:
                    await self.lighter_signer.close()
                except Exception:
                    logger.debug("Error closing Lighter SignerClient", exc_info=True)
            logger.info("Cleanup complete. Goodbye.")

    async def start_new_cycle(self):
        """Analyze funding rates and start a new position cycle."""
        self.state_mgr.set_state(BotState.ANALYZING)

        self.reload_config()

        # Re-check volume filtering to ensure we're only trading liquid markets
        logger.info(f"{Colors.CYAN}Checking volume thresholds for all symbols...{Colors.RESET}")
        symbols_before = set(self.config.symbols_to_monitor)
        await self._filter_tradable_symbols()
        symbols_after = set(self.config.symbols_to_monitor)

        # Log any changes
        added = symbols_after - symbols_before
        removed = symbols_before - symbols_after
        if added:
            logger.info(f"{Colors.GREEN}Added symbols (now meet volume threshold): {', '.join(added)}{Colors.RESET}")
        if removed:
            logger.warning(f"{Colors.YELLOW}Removed symbols (below volume threshold): {', '.join(removed)}{Colors.RESET}")
        if not added and not removed:
            logger.info(f"{Colors.GREEN}Symbol list unchanged ({len(symbols_after)} symbols){Colors.RESET}")

        logger.info("Analyzing funding rates for opportunities...")

        try:
            # Check balances first
            account_index = int(self.env["ACCOUNT_INDEX"])
            lighter_avail, lighter_total = await get_lighter_balance(self.env["LIGHTER_WS_URL"], account_index)
            pa_total, pa_avail = get_pacifica_balance(self.pacifica_client)
            logger.info(f"Balances | Lighter: ${lighter_total:.2f} (Avail: ${lighter_avail:.2f}) | Pacifica: ${pa_total:.2f} (Avail: ${pa_avail:.2f})")

            # Check for minimum balance
            if lighter_avail < 20 or pa_avail < 20:
                logger.warning(f"Insufficient balance to start new cycle. Lighter: ${lighter_avail:.2f}, Pacifica: ${pa_avail:.2f}. Required: $20 on each.")
                self.state_mgr.set_state(BotState.WAITING)
                return

            # Find best funding rate opportunity
            lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.env["LIGHTER_BASE_URL"]))
            try:
                opportunities = await fetch_funding_rates(lighter_api_client, self.pacifica_client, self.config.symbols_to_monitor, self.market_cache)
                if not opportunities:
                    logger.warning("No funding rate opportunities found.")
                    self.state_mgr.set_state(BotState.WAITING)
                    return

                opportunities.sort(key=lambda x: x["net_apr"], reverse=True)
                self._log_funding_snapshot(opportunities, title="Funding Snapshot (Top Opportunities)")
                best_opportunity = opportunities[0]

                if best_opportunity["net_apr"] < self.config.min_net_apr_threshold:
                    logger.info(f"Best opportunity ({best_opportunity['symbol']}: {best_opportunity['net_apr']:.2f}%) is below threshold.")
                    self.state_mgr.set_state(BotState.WAITING)
                    return

                logger.info(f"Found best opportunity: {best_opportunity['symbol']} with {best_opportunity['net_apr']:.2f}% APR.")

                symbol = best_opportunity['symbol']
                lighter_market_id = best_opportunity['lighter_market_id']

                # Determine and set the safe, final leverage
                MAX_ALLOWED_LEVERAGE = 20  # Hard cap for safety

                # Lighter doesn't expose max leverage via API, assume 20x max
                lighter_max_leverage = 20
                pacifica_max_leverage = self.pacifica_client.get_max_leverage(symbol)

                # Calculate target leverage (minimum of config, exchange limits, and hard cap)
                final_leverage = min(self.config.leverage, lighter_max_leverage, pacifica_max_leverage, MAX_ALLOWED_LEVERAGE)

                # Log leverage information
                if final_leverage < self.config.leverage:
                    reasons = []
                    if self.config.leverage > MAX_ALLOWED_LEVERAGE:
                        reasons.append(f"hard cap: {MAX_ALLOWED_LEVERAGE}x")
                    if self.config.leverage > lighter_max_leverage:
                        reasons.append(f"Lighter max: {lighter_max_leverage}x")
                    if self.config.leverage > pacifica_max_leverage:
                        reasons.append(f"Pacifica max: {pacifica_max_leverage}x")
                    logger.warning(f"Leverage adjusted to {final_leverage}x due to limits ({', '.join(reasons)}). Config: {self.config.leverage}x.")
                else:
                    logger.info(f"Using leverage: {final_leverage}x for both exchanges (Lighter max: {lighter_max_leverage}x, Pacifica max: {pacifica_max_leverage}x, Hard cap: {MAX_ALLOWED_LEVERAGE}x)")

                # Set leverage on Pacifica (Lighter leverage is set per-order)
                logger.info(f"Setting leverage to {final_leverage}x on Pacifica...")

                pacifica_leverage_success = self.pacifica_client.set_leverage(symbol, final_leverage)
                if not pacifica_leverage_success:
                    logger.error(f"{Colors.RED}Failed to set leverage on Pacifica for {symbol}. Aborting.{Colors.RESET}")
                    self.state_mgr.set_state(BotState.WAITING)
                    return

                logger.info(f"{Colors.GREEN}Successfully set leverage to {final_leverage}x on Pacifica.{Colors.RESET}")

                lighter_leverage_success = await self._update_lighter_leverage(lighter_market_id, final_leverage)
                if not lighter_leverage_success:
                    logger.error(f"{Colors.RED}Failed to set leverage on Lighter for {symbol}. Aborting cycle.{Colors.RESET}")
                    self.state_mgr.set_state(BotState.WAITING)
                    await self.emergency_close_all("Lighter leverage update failed")
                    return

                # Brief pause to ensure leverage updates propagate
                await asyncio.sleep(2)

                # Calculate target position size
                safe_base_capital = self.config.base_capital_allocation * 0.98
                target_position_notional = safe_base_capital * final_leverage
                logger.info(f"Target position size: ${target_position_notional:.2f}")

                # Calculate max position size based on available margin
                max_pos_lighter = lighter_avail * final_leverage
                max_pos_pa = pa_avail * final_leverage
                max_available_notional = min(max_pos_lighter, max_pos_pa)

                if max_available_notional < 10.0:
                    logger.warning("Insufficient available capital for a minimum position.")
                    self.state_mgr.set_state(BotState.WAITING)
                    return

                actual_notional = min(target_position_notional, max_available_notional * 0.95)

                if actual_notional < target_position_notional:
                    logger.warning(f"Position size reduced to ${actual_notional:.2f} due to available margin.")

                # Open the position
                await self.open_position(best_opportunity, actual_notional, final_leverage)

            finally:
                await lighter_api_client.close()

        except BalanceFetchError as e:
            logger.error(f"Could not fetch balances to start cycle: {e}")
            self.state_mgr.set_state(BotState.WAITING)
            await self.emergency_close_all("balance fetch failure during analysis")
        except Exception as e:
            logger.exception(f"Error during analysis phase: {e}")
            self.state_mgr.set_state(BotState.ERROR)
            await self.emergency_close_all(f"analysis failure: {e}")

    async def open_position(self, opportunity: Dict, notional: float, leverage: int):
        """Open a delta-neutral position on both exchanges."""
        self.state_mgr.set_state(BotState.OPENING)
        symbol = opportunity["symbol"]
        lighter_market_id = opportunity["lighter_market_id"]

        try:
            account_index = int(self.env["ACCOUNT_INDEX"])
            lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.env["LIGHTER_BASE_URL"]))

            try:
                # Get market details
                order_api = lighter.OrderApi(lighter_api_client)
                market_id, price_tick, amount_tick = await get_lighter_market_details(order_api, symbol)

                # Get current prices
                lighter_bid, lighter_ask = await get_lighter_best_bid_ask(order_api, symbol, market_id)
                pacifica_price = await self.pacifica_client.get_mark_price(symbol)

                if not lighter_bid or not lighter_ask or pacifica_price <= 0:
                    logger.error(f"{Colors.RED}Cannot get price data for {symbol}!{Colors.RESET}")
                    self.state_mgr.set_state(BotState.WAITING)
                    return

                lighter_mid = (lighter_bid + lighter_ask) / 2
                pacifica_lot_size = Decimal(str(self.pacifica_client.get_lot_size(symbol)))

                # Calculate position size
                # Use average price for size calculation
                avg_price = (lighter_mid + pacifica_price) / 2
                unrounded_quantity = Decimal(str(notional)) / Decimal(str(avg_price))

                # Round to coarser tick size
                coarser_step_size = max(Decimal(str(amount_tick)), pacifica_lot_size)
                quantizer = Decimal('1')
                rounded_quantity_decimal = (unrounded_quantity / coarser_step_size).quantize(quantizer, rounding=ROUND_DOWN) * coarser_step_size
                final_quantity = float(rounded_quantity_decimal)

                if final_quantity <= 0:
                    raise ValueError(f"Calculated quantity is zero after rounding. Notional ${notional} is too small.")

                logger.info(f"Calculated final quantity: {final_quantity:.8f} {symbol}")

                # Display full funding table before opening position
                logger.info(f"\n{Colors.BOLD}{Colors.CYAN}{'='*60}{Colors.RESET}")
                logger.info(f"{Colors.BOLD}{Colors.YELLOW}Opening Position - Current Funding Rates{Colors.RESET}")
                logger.info(f"{Colors.BOLD}{Colors.CYAN}{'='*60}{Colors.RESET}")
                try:
                    current_opportunities = await fetch_funding_rates(lighter_api_client, self.pacifica_client, self.config.symbols_to_monitor, self.market_cache)
                    if current_opportunities:
                        self._log_funding_snapshot(current_opportunities, title=f"Pre-Entry Funding Snapshot (Opening: {symbol})")
                    else:
                        self._log_funding_snapshot([opportunity], title=f"Funding for {symbol}")
                except Exception as e:
                    logger.warning(f"Could not fetch current funding rates: {e}")
                    self._log_funding_snapshot([opportunity], title=f"Funding for {symbol}")

                logger.info(f"Opening delta-neutral position for {symbol}...")

                # Execute orders based on which exchange is long/short
                lighter_result = None
                pacifica_result = None

                if opportunity["long_exch"] == "Lighter":
                    logger.info(f"Opening LONG on Lighter and SHORT on Pacifica")
                    # Long on Lighter (buy at ask)
                    lighter_result = await lighter_place_aggressive_order(
                        self.lighter_signer, market_id, price_tick, amount_tick,
                        "buy", final_quantity, ref_price=lighter_ask
                    )
                    # Short on Pacifica
                    pacifica_result = self.pacifica_client.place_market_order(symbol, side='sell', quantity=final_quantity)
                else:
                    logger.info(f"Opening LONG on Pacifica and SHORT on Lighter")
                    # Long on Pacifica
                    pacifica_result = self.pacifica_client.place_market_order(symbol, side='buy', quantity=final_quantity)
                    # Short on Lighter (sell at bid)
                    lighter_result = await lighter_place_aggressive_order(
                        self.lighter_signer, market_id, price_tick, amount_tick,
                        "sell", final_quantity, ref_price=lighter_bid
                    )

                # Check if both orders succeeded
                if lighter_result is None or pacifica_result is None:
                    raise RuntimeError(f"Failed to open one or both legs: Lighter={lighter_result}, Pacifica={pacifica_result}")

                logger.info(f"{Colors.GREEN}Successfully opened delta-neutral position for {symbol}.{Colors.RESET}")

                # Record position state
                self.state_mgr.state["current_cycle_number"] += 1
                opened_at = datetime.now(UTC)

                # Get entry balances
                lighter_avail_before, lighter_total_before = await get_lighter_balance(self.env["LIGHTER_WS_URL"], account_index)
                pacifica_balance_before = get_pacifica_balance(self.pacifica_client)[0]

                self.state_mgr.state["current_position"] = {
                    "symbol": symbol,
                    "opened_at": opened_at.isoformat(),
                    "target_close_at": (opened_at + timedelta(hours=self.config.hold_duration_hours)).isoformat(),
                    "long_exchange": opportunity["long_exch"],
                    "short_exchange": opportunity["short_exch"],
                    "notional": notional,
                    "leverage": leverage,
                    "lighter_market_id": lighter_market_id,
                    "entry_balance_lighter": lighter_total_before,
                    "entry_balance_pacifica": pacifica_balance_before
                }
                self.state_mgr.set_state(BotState.HOLDING)

            finally:
                await lighter_api_client.close()

        except Exception as e:
            logger.exception(f"Failed to open position for {symbol}. Error: {e}")
            # Attempt emergency close of any partial positions
            await self.close_position(is_emergency=True)
            self.state_mgr.set_state(BotState.ERROR)
            await self.emergency_close_all(f"open_position failure for {symbol}")

    async def monitor_position(self):
        """Monitor current position for PnL and stop-loss."""
        pos = self.state_mgr.state["current_position"]
        if not pos:
            self.state_mgr.set_state(BotState.IDLE)
            return

        symbol = pos["symbol"]
        lighter_market_id = pos.get("lighter_market_id")

        if not lighter_market_id:
            logger.error("Missing lighter_market_id in position state")
            self.state_mgr.set_state(BotState.ERROR)
            return

        # Check if hold duration has ended
        target_close_str = pos['target_close_at']
        if target_close_str.endswith('Z'):
            target_close_str = target_close_str[:-1] + '+00:00'
        target_close_dt = datetime.fromisoformat(target_close_str)
        if target_close_dt.tzinfo is None:
            target_close_dt = target_close_dt.replace(tzinfo=UTC)

        if datetime.now(UTC) >= target_close_dt:
            logger.info(f"Hold duration for {symbol} has ended. Closing position.")
            await self.close_position()
            return

        # Get detailed position and account info
        try:
            account_index = int(self.env["ACCOUNT_INDEX"])
            lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.env["LIGHTER_BASE_URL"]))

            try:
                # Get positions from both exchanges
                account_api = lighter.AccountApi(lighter_api_client)
                lighter_size = await get_lighter_open_size(account_api, account_index, lighter_market_id, symbol=symbol)
                pacifica_pos = await self.pacifica_client.get_position(symbol)

                # Get balances
                lighter_avail, lighter_total = await get_lighter_balance(self.env["LIGHTER_WS_URL"], account_index)
                pa_total, pa_avail = get_pacifica_balance(self.pacifica_client)

                # Get funding rates
                try:
                    funding_api = lighter.FundingApi(lighter_api_client)
                    lighter_funding_raw = await get_lighter_funding_rate(funding_api, lighter_market_id)
                    lighter_funding = (lighter_funding_raw / 8.0 * 24 * 365 * 100) if lighter_funding_raw else 0.0
                except Exception:
                    lighter_funding = 0.0

                try:
                    pa_rate_hourly = self.pacifica_client.get_funding_rate(symbol)
                    pa_funding = pa_rate_hourly * 24 * 365 * 100
                except Exception:
                    pa_funding = 0.0

                # Calculate PnL
                pnl_data = await get_position_pnl(lighter_api_client, self.pacifica_client, account_index, symbol, lighter_market_id)
                lighter_pnl = pnl_data["lighter_unrealized_pnl"]
                pa_pnl = pnl_data["pacifica_unrealized_pnl"]
                total_pnl = pnl_data["total_unrealized_pnl"]

                # Get position sizes
                pa_size = pacifica_pos.get('qty', 0.0) if pacifica_pos else 0.0

                # Get leverage (use stored leverage from position state)
                lighter_leverage = pos.get('leverage', self.config.leverage)
                pa_leverage = pos.get('leverage', self.config.leverage)

                # Display status
                pnl_color = Colors.GREEN if total_pnl >= 0 else Colors.RED
                cycle_number = self.state_mgr.state.get("current_cycle_number", 0)

                # Parse opened_at time
                opened_at_str = pos.get('opened_at', '')
                if opened_at_str.endswith('Z'):
                    opened_at_str = opened_at_str[:-1] + '+00:00'
                opened_at_dt = datetime.fromisoformat(opened_at_str) if opened_at_str else None

                # Build status message as single string
                status_lines = []
                status_lines.append(f"\n{Colors.BOLD}{Colors.CYAN}{'='*70}{Colors.RESET}")
                status_lines.append(f"{Colors.BOLD}Position Status: {Colors.YELLOW}{symbol}{Colors.RESET} {Colors.GRAY}(Cycle #{cycle_number}){Colors.RESET}")
                status_lines.append(f"{Colors.CYAN}{'='*70}{Colors.RESET}")

                # Show timing information
                if opened_at_dt:
                    status_lines.append(f"\n{Colors.BOLD}{Colors.BLUE}Timing:{Colors.RESET}")
                    status_lines.append(f"  {Colors.GRAY}Opened:{Colors.RESET}      {Colors.CYAN}{opened_at_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}{Colors.RESET}")
                    status_lines.append(f"  {Colors.GRAY}Target Close:{Colors.RESET} {Colors.CYAN}{target_close_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}{Colors.RESET}")
                    time_remaining = target_close_dt - datetime.now(UTC)
                    hours_remaining = time_remaining.total_seconds() / 3600
                    time_color = Colors.GREEN if hours_remaining > 6 else (Colors.YELLOW if hours_remaining > 2 else Colors.RED)
                    status_lines.append(f"  {Colors.GRAY}Time Left:{Colors.RESET}    {time_color}{hours_remaining:.1f} hours{Colors.RESET}")

                status_lines.append(f"\n{Colors.BOLD}{Colors.MAGENTA}Position Sizes:{Colors.RESET}")
                lighter_size_color = Colors.GREEN if lighter_size > 0 else Colors.RED
                pa_size_color = Colors.GREEN if pa_size > 0 else Colors.RED
                status_lines.append(f"  {Colors.CYAN}Lighter:{Colors.RESET}   {lighter_size_color}{lighter_size:+.4f}{Colors.RESET} {Colors.YELLOW}{symbol}{Colors.RESET}")
                status_lines.append(f"  {Colors.CYAN}Pacifica:{Colors.RESET}  {pa_size_color}{pa_size:+.4f}{Colors.RESET} {Colors.YELLOW}{symbol}{Colors.RESET}")
                status_lines.append(f"  {Colors.BOLD}Notional:{Colors.RESET}   {Colors.YELLOW}${pos['notional']:.2f}{Colors.RESET} {Colors.GRAY}(per exchange){Colors.RESET}")

                status_lines.append(f"\n{Colors.BOLD}{Colors.BLUE}Account Balances:{Colors.RESET}")
                status_lines.append(f"  {Colors.CYAN}Lighter:{Colors.RESET}   {Colors.GREEN}${lighter_total:.2f}{Colors.RESET} {Colors.GRAY}(Available: ${lighter_avail:.2f}){Colors.RESET}")
                status_lines.append(f"  {Colors.CYAN}Pacifica:{Colors.RESET}  {Colors.GREEN}${pa_total:.2f}{Colors.RESET} {Colors.GRAY}(Available: ${pa_avail:.2f}){Colors.RESET}")
                current_equity = lighter_total + pa_total
                total_equity_color = Colors.GREEN if current_equity > 200 else Colors.YELLOW
                status_lines.append(f"  {Colors.BOLD}Total Equity:{Colors.RESET} {total_equity_color}${current_equity:.2f}{Colors.RESET}")

                # Display long-term PnL if initial capital is available
                initial_capital = self.state_mgr.state.get("initial_capital")
                if initial_capital is not None:
                    long_term_pnl = current_equity - initial_capital
                    long_term_pnl_pct = (long_term_pnl / initial_capital * 100) if initial_capital > 0 else 0
                    lt_pnl_color = Colors.GREEN if long_term_pnl >= 0 else Colors.RED
                    status_lines.append(f"  {Colors.BOLD}Total PnL:{Colors.RESET}    {lt_pnl_color}${long_term_pnl:+.2f} ({long_term_pnl_pct:+.2f}%){Colors.RESET} {Colors.GRAY}(since start){Colors.RESET}")

                status_lines.append(f"\n{Colors.BOLD}{Colors.YELLOW}Leverage:{Colors.RESET}")
                lev_color = Colors.GREEN if lighter_leverage <= 5 else (Colors.YELLOW if lighter_leverage <= 10 else Colors.RED)
                status_lines.append(f"  {Colors.CYAN}Lighter:{Colors.RESET}   {lev_color}{lighter_leverage:.1f}x{Colors.RESET}")
                status_lines.append(f"  {Colors.CYAN}Pacifica:{Colors.RESET}  {lev_color}{pa_leverage:.1f}x{Colors.RESET}")

                status_lines.append(f"\n{Colors.BOLD}{Colors.MAGENTA}Funding Rates (APR):{Colors.RESET}")
                lighter_color = Colors.GREEN if lighter_funding >= 0 else Colors.RED
                pa_color = Colors.GREEN if pa_funding >= 0 else Colors.RED
                status_lines.append(f"  {Colors.CYAN}Lighter:{Colors.RESET}   {lighter_color}{lighter_funding:+.2f}%{Colors.RESET}")
                status_lines.append(f"  {Colors.CYAN}Pacifica:{Colors.RESET}  {pa_color}{pa_funding:+.2f}%{Colors.RESET}")
                spread_color = Colors.GREEN if abs(lighter_funding - pa_funding) > 30 else Colors.YELLOW
                status_lines.append(f"  {Colors.BOLD}Net Spread:{Colors.RESET}  {spread_color}{abs(lighter_funding - pa_funding):.2f}%{Colors.RESET}")

                status_lines.append(f"\n{Colors.BOLD}{Colors.GREEN}Unrealized PnL:{Colors.RESET}")
                lighter_pnl_color = Colors.GREEN if lighter_pnl >= 0 else Colors.RED
                pa_pnl_color = Colors.GREEN if pa_pnl >= 0 else Colors.RED
                status_lines.append(f"  {Colors.CYAN}Lighter:{Colors.RESET}   {lighter_pnl_color}${lighter_pnl:+.2f}{Colors.RESET}")
                status_lines.append(f"  {Colors.CYAN}Pacifica:{Colors.RESET}  {pa_pnl_color}${pa_pnl:+.2f}{Colors.RESET}")
                status_lines.append(f"  {Colors.BOLD}Total PnL:{Colors.RESET}   {pnl_color}${total_pnl:+.2f}{Colors.RESET}")

                # Determine worst leg (for stop-loss calculation)
                worst_leg = "Lighter" if lighter_pnl < pa_pnl else "Pacifica"
                worst_pnl = min(lighter_pnl, pa_pnl)

                # Stop-loss check and display (based on worst leg)
                pnl_data = {"total_unrealized_pnl": worst_pnl}  # Use worst leg PnL for stop-loss
                stop_loss_percent = pos.get("stop_loss_percent")
                if stop_loss_percent is None:
                    stop_loss_percent = calculate_dynamic_stop_loss(pos.get("leverage", self.config.leverage))

                # Calculate stop-loss trigger level
                stop_loss_trigger = -1 * (pos["notional"] * stop_loss_percent / 100)
                pnl_percent = (total_pnl / pos["notional"]) * 100 if pos["notional"] > 0 else 0
                worst_leg_pnl_percent = (worst_pnl / pos["notional"]) * 100 if pos["notional"] > 0 else 0

                status_lines.append(f"\n{Colors.BOLD}{Colors.RED}Risk Management:{Colors.RESET}")
                status_lines.append(f"  {Colors.GRAY}Stop-Loss:{Colors.RESET}   {Colors.RED}-{stop_loss_percent:.1f}%{Colors.RESET} {Colors.GRAY}(${stop_loss_trigger:.2f}){Colors.RESET}")
                pnl_pct_color = Colors.GREEN if pnl_percent >= 0 else Colors.RED
                status_lines.append(f"  {Colors.GRAY}Total PnL:{Colors.RESET}    {pnl_pct_color}{pnl_percent:+.2f}%{Colors.RESET} {Colors.GRAY}(${total_pnl:+.2f}){Colors.RESET}")
                status_lines.append(f"  {Colors.GRAY}Lighter PnL:{Colors.RESET}  {lighter_pnl_color}${lighter_pnl:+.2f}{Colors.RESET}")
                status_lines.append(f"  {Colors.GRAY}PA PnL:{Colors.RESET}      {pa_pnl_color}${pa_pnl:+.2f}{Colors.RESET}")
                worst_leg_color = Colors.CYAN
                worst_pnl_color = Colors.GREEN if worst_pnl >= 0 else Colors.RED
                worst_leg_pct_color = Colors.GREEN if worst_leg_pnl_percent >= 0 else Colors.RED
                status_lines.append(f"  {Colors.GRAY}Worst Leg:{Colors.RESET}   {worst_leg_color}{worst_leg}{Colors.RESET} ({worst_pnl_color}${worst_pnl:+.2f}{Colors.RESET}, {worst_leg_pct_color}{worst_leg_pnl_percent:+.2f}%{Colors.RESET})")

                # Distance to stop-loss (based on worst leg)
                distance_to_sl = worst_pnl - stop_loss_trigger
                if distance_to_sl < 0:
                    sl_color = Colors.RED
                    status_lines.append(f"  {Colors.RED}{Colors.BOLD}STOP-LOSS BREACH: ${distance_to_sl:+.2f}{Colors.RESET}")
                else:
                    sl_pct = (distance_to_sl / abs(stop_loss_trigger)) * 100 if stop_loss_trigger != 0 else 100
                    if sl_pct < 20:
                        sl_color = Colors.RED
                        status_lines.append(f"  {Colors.GRAY}Distance to SL:{Colors.RESET} {sl_color}${distance_to_sl:.2f} ({sl_pct:.1f}%){Colors.RESET}")
                    elif sl_pct < 50:
                        sl_color = Colors.YELLOW
                        status_lines.append(f"  {Colors.GRAY}Distance to SL:{Colors.RESET} {sl_color}${distance_to_sl:.2f} ({sl_pct:.1f}%){Colors.RESET}")
                    else:
                        status_lines.append(f"  {Colors.GRAY}Distance to SL:{Colors.RESET} {Colors.GREEN}${distance_to_sl:.2f} ({sl_pct:.1f}%){Colors.RESET}")

                status_lines.append(f"{Colors.CYAN}{'='*70}{Colors.RESET}\n")

                # Log as single message
                logger.info("\n".join(status_lines))

                triggered, reason = check_stop_loss(pnl_data, pos["notional"], stop_loss_percent)
                if triggered:
                    logger.warning(f"{Colors.RED}Stop-loss triggered! {reason}{Colors.RESET}")
                    await self.close_position()
                    return

            finally:
                await lighter_api_client.close()

        except Exception as e:
            logger.error(f"Error gathering position info: {e}")
            await self.emergency_close_all(f"monitor_position failure for {pos.get('symbol', 'unknown')}")
            self.state_mgr.set_state(BotState.ERROR)

        logger.info(f"Next health check in {self.config.check_interval_seconds} seconds.")
        await self._responsive_sleep(self.config.check_interval_seconds)

    async def close_position(self, is_emergency: bool = False, max_retries: int = 3):
        """Close the current position."""
        if not is_emergency:
            self.state_mgr.set_state(BotState.CLOSING)

        pos = self.state_mgr.state["current_position"]
        if not pos:
            logger.warning("Close called but no position in state.")
            return

        symbol = pos["symbol"]
        lighter_market_id = pos.get("lighter_market_id")

        if not lighter_market_id:
            logger.error("Missing lighter_market_id in position state")
            self.state_mgr.set_state(BotState.ERROR)
            return

        try:
            account_index = int(self.env["ACCOUNT_INDEX"])
            lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.env["LIGHTER_BASE_URL"]))

            try:
                account_api = lighter.AccountApi(lighter_api_client)
                pacifica_pos = await self.pacifica_client.get_position(symbol)
                lighter_size = await get_lighter_open_size(account_api, account_index, lighter_market_id, symbol=symbol)

                # Close positions with retry logic
                lighter_closed = False
                pacifica_closed = False

                # Close Pacifica position
                if pacifica_pos and pacifica_pos['qty'] != 0:
                    close_qty = abs(pacifica_pos['qty'])
                    close_side = 'sell' if pacifica_pos['qty'] > 0 else 'buy'
                    for attempt in range(max_retries):
                        logger.info(f"Closing Pacifica position for {symbol}: {close_qty:.4f} {close_side} (attempt {attempt + 1}/{max_retries})...")
                        try:
                            pacifica_result = self.pacifica_client.place_market_order(symbol, side=close_side, quantity=close_qty, reduce_only=True)
                            if pacifica_result is not None:
                                pacifica_closed = True
                                logger.info(f"{Colors.GREEN}Pacifica position closed{Colors.RESET}")
                                break
                            else:
                                logger.warning(f"Pacifica close returned None on attempt {attempt + 1}")
                        except Exception as e:
                            logger.error(f"Pacifica close attempt {attempt + 1} failed: {e}")
                        if not pacifica_closed and attempt < max_retries - 1:
                            await asyncio.sleep(2)
                else:
                    pacifica_closed = True
                    logger.debug(f"No Pacifica position to close for {symbol}")

                # Close Lighter position
                if lighter_size != 0:
                    # Get market details and prices
                    order_api = lighter.OrderApi(lighter_api_client)
                    market_id, price_tick, amount_tick = await get_lighter_market_details(order_api, symbol)
                    lighter_bid, lighter_ask = await get_lighter_best_bid_ask(order_api, symbol, market_id)

                    # Determine close side and ref_price
                    if lighter_size > 0:
                        # Long position: sell to close (use bid as ref_price)
                        close_side = "sell"
                        ref_price = lighter_bid
                    else:
                        # Short position: buy to close (use ask as ref_price)
                        close_side = "buy"
                        ref_price = lighter_ask

                    close_size = abs(lighter_size)

                    for attempt in range(max_retries):
                        logger.info(f"Closing Lighter position for {symbol}: {lighter_size:+.4f} (attempt {attempt + 1}/{max_retries})...")
                        try:
                            lighter_result = await lighter_close_position(
                                self.lighter_signer, market_id, price_tick, amount_tick,
                                close_side, close_size, ref_price
                            )
                            if lighter_result:
                                lighter_closed = True
                                logger.info(f"{Colors.GREEN}Lighter position closed{Colors.RESET}")
                                break
                            else:
                                logger.warning(f"Lighter close failed on attempt {attempt + 1}")
                        except Exception as e:
                            logger.error(f"Lighter close attempt {attempt + 1} failed: {e}")
                        if not lighter_closed and attempt < max_retries - 1:
                            await asyncio.sleep(2)
                else:
                    lighter_closed = True
                    logger.debug(f"No Lighter position to close for {symbol}")

                # Report results
                if lighter_closed and pacifica_closed:
                    logger.info(f"{Colors.GREEN}Successfully closed all positions for {symbol}.{Colors.RESET}")
                elif pacifica_closed and not lighter_closed:
                    logger.warning(f"{Colors.YELLOW}Pacifica position closed, but Lighter position remains (manual close needed){Colors.RESET}")
                elif lighter_closed and not pacifica_closed:
                    logger.error(f"{Colors.RED}PARTIAL CLOSE: Lighter closed, but Pacifica position remains for {symbol}!{Colors.RESET}")
                    raise RuntimeError(f"Failed to close Pacifica position for {symbol} after {max_retries} attempts")
                else:
                    logger.error(f"{Colors.RED}Failed to close both positions for {symbol}{Colors.RESET}")

                if not is_emergency:
                    # PnL Calculation
                    try:
                        lighter_avail_after, lighter_total_after = await get_lighter_balance(self.env["LIGHTER_WS_URL"], account_index)
                        pacifica_balance_after = get_pacifica_balance(self.pacifica_client)[0]

                        lighter_balance_before = pos.get('entry_balance_lighter', lighter_total_after)
                        pacifica_balance_before = pos.get('entry_balance_pacifica', pacifica_balance_after)

                        pnl_lighter = lighter_total_after - lighter_balance_before
                        pnl_pacifica = pacifica_balance_after - pacifica_balance_before
                        total_pnl = pnl_lighter + pnl_pacifica

                        logger.info(f"Cycle PnL | Lighter: ${pnl_lighter:+.2f} | Pacifica: ${pnl_pacifica:+.2f} | Total: ${total_pnl:+.2f}")

                        stats = self.state_mgr.state["cumulative_stats"]
                        stats["total_cycles"] += 1
                        stats["successful_cycles"] += 1
                        stats["total_realized_pnl"] += total_pnl
                    except Exception as e:
                        logger.warning(f"Could not calculate PnL: {e}")

                self.state_mgr.state["current_position"] = None
                self.state_mgr.set_state(BotState.WAITING)

            finally:
                await lighter_api_client.close()

        except Exception as e:
            logger.exception(f"Failed to close position for {symbol}: {e}")
            self.state_mgr.set_state(BotState.ERROR)
            await self.emergency_close_all(f"close_position failure for {symbol}")

async def main():
    parser = argparse.ArgumentParser(description="Delta-Neutral Funding Rate Bot for Lighter and Pacifica")
    parser.add_argument("--state-file", type=str, default="bot_state_lighter_pacifica.json", help="Path to the state file.")
    parser.add_argument("--config-file", type=str, default="bot_config.json", help="Path to the configuration file.")
    args = parser.parse_args()

    bot = RotationBot(state_file=args.state_file, config_file=args.config_file)
    await bot.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
