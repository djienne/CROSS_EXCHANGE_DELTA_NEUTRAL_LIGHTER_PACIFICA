#!/usr/bin/env python3
"""
Check mid-price spreads between Lighter and Pacifica exchanges.
Displays bid/ask/mid prices and spread percentages in a formatted table.

Usage:
    python check_spreads.py
    python check_spreads.py --config custom_config.json
"""

import asyncio
import json
import sys
import argparse
import logging
from pathlib import Path
from typing import Optional, Tuple
from dotenv import load_dotenv
import os

# Silence noisy loggers
logging.getLogger('websockets').setLevel(logging.WARNING)
logging.getLogger('websockets.client').setLevel(logging.WARNING)
logging.getLogger('websockets.server').setLevel(logging.WARNING)
logging.getLogger('websockets.protocol').setLevel(logging.WARNING)
logging.getLogger('asyncio').setLevel(logging.WARNING)
logging.getLogger('lighter').setLevel(logging.WARNING)
logging.getLogger('lighter_client').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('aiohttp').setLevel(logging.WARNING)

# Import Lighter SDK
import lighter

# Add parent directory for SDK imports
sys.path.insert(0, str(Path(__file__).parent))
from pacifica_client import PacificaClient
from lighter_client import get_lighter_market_details, get_lighter_best_bid_ask

# ANSI color codes for terminal output
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    GRAY = '\033[90m'
    RESET = '\033[0m'


def load_config(config_file: str = "bot_config.json") -> dict:
    """Load configuration from file."""
    try:
        with open(config_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"{Colors.RED}Error loading config: {e}{Colors.RESET}")
        sys.exit(1)


async def get_pacifica_bid_ask(client: PacificaClient, symbol: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Get best bid, ask, and mark price from Pacifica.

    Returns:
        Tuple of (bid, ask, mark_price) or (None, None, None) if failed
    """
    try:
        # Try to get mark price
        mark_price = await client.get_mark_price(symbol)

        # For Pacifica, we'll estimate bid/ask from mark price with a small spread
        # This is an approximation since Pacifica API doesn't directly expose orderbook
        if mark_price and mark_price > 0:
            # Estimate 0.05% spread on each side
            spread_pct = 0.0005
            estimated_bid = mark_price * (1 - spread_pct)
            estimated_ask = mark_price * (1 + spread_pct)
            return estimated_bid, estimated_ask, mark_price

        return None, None, None

    except Exception as e:
        print(f"{Colors.YELLOW}Pacifica {symbol}: Error fetching prices - {e}{Colors.RESET}")
        return None, None, None


def format_price(price: Optional[float]) -> str:
    """Format price with appropriate precision."""
    if price is None:
        return f"{Colors.RED}N/A{Colors.RESET}"

    if price >= 1000:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:.4f}"
    else:
        return f"${price:.8f}"


def format_spread(spread_pct: Optional[float]) -> str:
    """Format spread percentage with color coding."""
    if spread_pct is None:
        return f"{Colors.RED}N/A{Colors.RESET}"

    abs_spread = abs(spread_pct)

    # Color code based on spread magnitude
    if abs_spread < 0.1:
        color = Colors.GREEN
    elif abs_spread < 0.5:
        color = Colors.YELLOW
    else:
        color = Colors.RED

    # Show direction
    direction = "↑" if spread_pct > 0 else "↓" if spread_pct < 0 else "="

    return f"{color}{spread_pct:+.3f}% {direction}{Colors.RESET}"


async def main():
    parser = argparse.ArgumentParser(description='Check mid-price spreads between exchanges')
    parser.add_argument('--config', type=str, default='bot_config.json',
                       help='Path to bot config file (default: bot_config.json)')
    args = parser.parse_args()

    # Print header
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*120}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}MID-PRICE SPREAD ANALYSIS: LIGHTER vs PACIFICA{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'='*120}{Colors.RESET}\n")

    # Load config
    config = load_config(args.config)
    symbols = config.get('symbols_to_monitor', [])

    if not symbols:
        print(f"{Colors.RED}No symbols found in config file{Colors.RESET}")
        return

    print(f"Loaded {len(symbols)} symbols from {args.config}\n")
    print(f"{Colors.YELLOW}Fetching prices from Lighter and Pacifica...{Colors.RESET}\n")

    # Load environment variables
    load_dotenv()
    lighter_base_url = os.getenv("LIGHTER_BASE_URL") or os.getenv("BASE_URL") or "https://mainnet.zklighter.elliot.ai"
    sol_wallet = os.getenv("SOL_WALLET")
    api_public = os.getenv("API_PUBLIC")
    api_private = os.getenv("API_PRIVATE")

    if not all([sol_wallet, api_public, api_private]):
        print(f"{Colors.RED}Missing required environment variables (SOL_WALLET, API_PUBLIC, API_PRIVATE){Colors.RESET}")
        return

    # Initialize clients
    try:
        lighter_api_client = lighter.ApiClient(configuration=lighter.Configuration(host=lighter_base_url))
        pacifica_client = PacificaClient(sol_wallet, api_public, api_private)
        order_api = lighter.OrderApi(lighter_api_client)
    except Exception as e:
        print(f"{Colors.RED}Error initializing clients: {e}{Colors.RESET}")
        return

    # Collect price data for all symbols
    results = []

    try:
        for i, symbol in enumerate(symbols):
            print(f"Processing {symbol}... ({i+1}/{len(symbols)})", end='\r', flush=True)

            result = {
                'symbol': symbol,
                'lighter_bid': None,
                'lighter_ask': None,
                'lighter_mid': None,
                'pacifica_bid': None,
                'pacifica_ask': None,
                'pacifica_mid': None,
                'spread_pct': None,
                'lighter_available': False,
                'pacifica_available': False
            }

            # Get Lighter prices with timeout
            try:
                lighter_task = asyncio.create_task(get_lighter_market_details(order_api, symbol))
                market_id, price_tick, amount_tick = await asyncio.wait_for(lighter_task, timeout=10.0)

                bid_ask_task = asyncio.create_task(get_lighter_best_bid_ask(order_api, symbol, market_id))
                lighter_bid, lighter_ask = await asyncio.wait_for(bid_ask_task, timeout=10.0)

                if lighter_bid and lighter_ask:
                    result['lighter_bid'] = lighter_bid
                    result['lighter_ask'] = lighter_ask
                    result['lighter_mid'] = (lighter_bid + lighter_ask) / 2
                    result['lighter_available'] = True

                # Rate limit
                await asyncio.sleep(1.0)

            except asyncio.TimeoutError:
                print(f"{Colors.YELLOW}Lighter {symbol}: Timeout{Colors.RESET}                         ")
            except Exception as e:
                print(f"{Colors.YELLOW}Lighter {symbol}: {str(e)[:50]}{Colors.RESET}                    ")

            # Get Pacifica prices with timeout
            try:
                pac_task = asyncio.create_task(get_pacifica_bid_ask(pacifica_client, symbol))
                pac_bid, pac_ask, pac_mark = await asyncio.wait_for(pac_task, timeout=10.0)

                if pac_mark:
                    result['pacifica_bid'] = pac_bid
                    result['pacifica_ask'] = pac_ask
                    result['pacifica_mid'] = pac_mark  # Use mark price as mid
                    result['pacifica_available'] = True

                # Rate limit
                await asyncio.sleep(0.1)

            except asyncio.TimeoutError:
                print(f"{Colors.YELLOW}Pacifica {symbol}: Timeout{Colors.RESET}                         ")
            except Exception as e:
                print(f"{Colors.YELLOW}Pacifica {symbol}: {str(e)[:50]}{Colors.RESET}                    ")

            # Calculate spread
            if result['lighter_mid'] and result['pacifica_mid']:
                # Spread = (Lighter - Pacifica) / Pacifica * 100
                # Positive = Lighter is more expensive
                result['spread_pct'] = ((result['lighter_mid'] - result['pacifica_mid']) / result['pacifica_mid']) * 100

            results.append(result)

        print(" " * 100, end='\r')  # Clear the "Processing..." line

    finally:
        await lighter_api_client.close()

    # Print table header
    print(f"\n{Colors.BOLD}{'Symbol':<10} {'Lighter Bid':<15} {'Lighter Ask':<15} {'Lighter Mid':<15} "
          f"{'Pacifica Bid':<15} {'Pacifica Ask':<15} {'Pacifica Mid':<15} {'Spread':<15} {'Available On':<25}{Colors.RESET}")
    print(f"{Colors.BOLD}{'-'*135}{Colors.RESET}")

    # Print results
    available_on_both = 0
    total_abs_spread = 0.0
    count_spreads = 0

    for result in results:
        symbol = result['symbol']

        # Format prices
        lighter_bid_str = format_price(result['lighter_bid'])
        lighter_ask_str = format_price(result['lighter_ask'])
        lighter_mid_str = format_price(result['lighter_mid'])

        pacifica_bid_str = format_price(result['pacifica_bid'])
        pacifica_ask_str = format_price(result['pacifica_ask'])
        pacifica_mid_str = format_price(result['pacifica_mid'])

        spread_str = format_spread(result['spread_pct'])

        # Format availability
        availability = []
        if result['lighter_available']:
            availability.append(f"{Colors.GREEN}Lighter{Colors.RESET}")
        if result['pacifica_available']:
            availability.append(f"{Colors.GREEN}Pacifica{Colors.RESET}")

        if not availability:
            availability_str = f"{Colors.RED}None{Colors.RESET}"
        else:
            availability_str = ", ".join(availability)

        if result['lighter_available'] and result['pacifica_available']:
            available_on_both += 1
            if result['spread_pct'] is not None:
                total_abs_spread += abs(result['spread_pct'])
                count_spreads += 1

        # Print row (accounting for color codes in width)
        print(f"{symbol:<10} {lighter_bid_str:<25} {lighter_ask_str:<25} {lighter_mid_str:<25} "
              f"{pacifica_bid_str:<25} {pacifica_ask_str:<25} {pacifica_mid_str:<25} {spread_str:<25} "
              f"{availability_str}")

    # Print summary
    print(f"{Colors.BOLD}{'-'*135}{Colors.RESET}")
    print(f"\n{Colors.BOLD}SUMMARY:{Colors.RESET}")
    print(f"  Total Symbols:               {Colors.CYAN}{len(symbols)}{Colors.RESET}")
    print(f"  Available on Both:           {Colors.GREEN}{available_on_both}{Colors.RESET}")
    print(f"  Symbols on Lighter:          {Colors.CYAN}{sum(1 for r in results if r['lighter_available'])}{Colors.RESET}")
    print(f"  Symbols on Pacifica:         {Colors.CYAN}{sum(1 for r in results if r['pacifica_available'])}{Colors.RESET}")

    if count_spreads > 0:
        avg_abs_spread = total_abs_spread / count_spreads
        spread_color = Colors.GREEN if avg_abs_spread < 0.2 else (Colors.YELLOW if avg_abs_spread < 0.5 else Colors.RED)
        print(f"  Average Absolute Spread:     {spread_color}{avg_abs_spread:.3f}%{Colors.RESET}")

    print(f"\n{Colors.GRAY}Note: Positive spread = Lighter more expensive | Negative spread = Pacifica more expensive{Colors.RESET}")
    print(f"{Colors.GRAY}Pacifica bid/ask are estimated from mark price (±0.05% spread){Colors.RESET}")
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*120}{Colors.RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
