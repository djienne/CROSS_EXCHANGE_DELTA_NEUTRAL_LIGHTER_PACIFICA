#!/usr/bin/env python3
"""
Check 24-hour trading volume for all symbols in bot_config.json.
Displays volumes from both Lighter and Pacifica in a formatted table.

Usage:
    python check_24h_volume.py
    python check_24h_volume.py --config custom_config.json
"""

import asyncio
import json
import sys
import time
import argparse
import logging
from pathlib import Path
from typing import Dict, Optional
import requests
import lighter

# Silence noisy loggers
logging.getLogger('websockets').setLevel(logging.WARNING)
logging.getLogger('websockets.client').setLevel(logging.WARNING)
logging.getLogger('websockets.server').setLevel(logging.WARNING)
logging.getLogger('websockets.protocol').setLevel(logging.WARNING)
logging.getLogger('asyncio').setLevel(logging.WARNING)
logging.getLogger('lighter').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('aiohttp').setLevel(logging.WARNING)

# Add parent directory for SDK imports
sys.path.insert(0, str(Path(__file__).parent))
from pacifica_sdk.common.constants import REST_URL

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
    RESET = '\033[0m'


def load_config(config_file: str = "bot_config.json") -> Dict:
    """Load configuration from file."""
    try:
        with open(config_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"{Colors.RED}Error loading config: {e}{Colors.RESET}")
        sys.exit(1)


async def get_lighter_24h_volume(symbol: str, market_id: int, api_client: lighter.ApiClient) -> Optional[float]:
    """
    Get 24-hour volume for a symbol on Lighter using candlestick data.

    Args:
        symbol: Trading symbol
        market_id: Market ID on Lighter
        api_client: Lighter API client

    Returns:
        24h volume in base currency (e.g., BTC, ETH) or None if failed
    """
    try:
        # Use candlestick API to get last 24 hours of 1h candles
        candlestick_api = lighter.CandlestickApi(api_client)

        # Get current timestamp and 24h ago
        end_timestamp = int(time.time())
        start_timestamp = end_timestamp - (24 * 60 * 60)  # 24 hours ago

        # Fetch candlesticks (1h resolution)
        # Note: resolution is a string like "1h", count_back is required
        response = await candlestick_api.candlesticks(
            market_id=market_id,
            resolution="1h",  # 1 hour candles
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            count_back=24,  # 24 hours
            set_timestamp_to_end=False
        )

        if not response or not hasattr(response, 'candlesticks'):
            return None

        # Sum volume0 (base token volume) from all candles
        total_volume = 0.0
        for candle in response.candlesticks:
            # volume0 is base token volume (e.g., BTC, ETH)
            total_volume += float(getattr(candle, 'volume0', 0))

        return total_volume

    except Exception as e:
        print(f"{Colors.YELLOW}Lighter {symbol}: Error fetching volume - {e}{Colors.RESET}")
        return None


async def get_pacifica_24h_volume(symbol: str) -> tuple[Optional[float], Optional[float]]:
    """
    Get 24-hour volume and latest price for a symbol on Pacifica using klines.

    Args:
        symbol: Trading symbol (e.g., 'BTC', 'ETH')

    Returns:
        Tuple of (24h volume in base currency, latest close price) or (None, None) if failed
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
            return None, None

        klines = data.get('data', [])

        if not klines:
            return None, None

        # Sum volume from all klines
        total_volume = sum(float(kline.get('v', 0)) for kline in klines)

        # Get latest close price from most recent kline
        latest_price = float(klines[-1].get('c', 0)) if klines else 0.0

        return total_volume, latest_price

    except Exception as e:
        print(f"{Colors.YELLOW}Pacifica {symbol}: Error fetching volume - {e}{Colors.RESET}")
        return None, None


async def get_lighter_market_cache(api_client: lighter.ApiClient) -> Dict[str, int]:
    """Build cache of symbol -> market_id mappings for Lighter."""
    try:
        order_api = lighter.OrderApi(api_client)
        resp = await order_api.order_books()

        market_cache = {}
        for ob in resp.order_books:
            symbol = ob.symbol.upper()
            market_cache[symbol] = ob.market_id

        return market_cache

    except Exception as e:
        print(f"{Colors.RED}Error building Lighter market cache: {e}{Colors.RESET}")
        return {}


def format_volume(volume: Optional[float]) -> str:
    """Format volume with color coding."""
    if volume is None:
        return f"{Colors.RED}N/A{Colors.RESET}"

    # Color code based on magnitude
    if volume >= 1000:
        color = Colors.GREEN
    elif volume >= 100:
        color = Colors.CYAN
    elif volume >= 10:
        color = Colors.YELLOW
    else:
        color = Colors.RED

    return f"{color}{volume:,.2f}{Colors.RESET}"


def format_usd_volume(volume: Optional[float], price: float) -> str:
    """Format USD volume with color coding."""
    if volume is None or price <= 0:
        return f"{Colors.RED}N/A{Colors.RESET}"

    usd_volume = volume * price

    # Color code based on magnitude
    if usd_volume >= 10_000_000:  # 10M+
        color = Colors.GREEN
    elif usd_volume >= 1_000_000:  # 1M+
        color = Colors.CYAN
    elif usd_volume >= 100_000:  # 100K+
        color = Colors.YELLOW
    else:
        color = Colors.RED

    return f"{color}${usd_volume:,.0f}{Colors.RESET}"




async def main():
    parser = argparse.ArgumentParser(description='Check 24h trading volume for all symbols')
    parser.add_argument('--config', type=str, default='bot_config.json',
                       help='Path to bot config file (default: bot_config.json)')
    args = parser.parse_args()

    # Print header
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*100}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}24-HOUR TRADING VOLUME REPORT{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'='*100}{Colors.RESET}\n")

    # Load config
    config = load_config(args.config)
    symbols = config.get('symbols_to_monitor', [])

    if not symbols:
        print(f"{Colors.RED}No symbols found in config file{Colors.RESET}")
        return

    print(f"Loaded {len(symbols)} symbols from {args.config}\n")
    print(f"{Colors.YELLOW}Fetching volume data from Lighter and Pacifica...{Colors.RESET}\n")

    # Initialize Lighter client
    try:
        api_client = lighter.ApiClient()
        market_cache = await get_lighter_market_cache(api_client)
    except Exception as e:
        print(f"{Colors.RED}Error initializing Lighter client: {e}{Colors.RESET}")
        market_cache = {}

    # Collect volume data for all symbols
    results = []

    for symbol in symbols:
        print(f"Processing {symbol}...", end='\r')

        # Get Lighter volume
        lighter_volume = None
        if symbol in market_cache:
            lighter_volume = await get_lighter_24h_volume(symbol, market_cache[symbol], api_client)

        # Get Pacifica volume and price
        pacifica_volume, price = await get_pacifica_24h_volume(symbol)

        results.append({
            'symbol': symbol,
            'lighter_volume': lighter_volume,
            'pacifica_volume': pacifica_volume,
            'price': price,
            'on_lighter': symbol in market_cache,
            'on_pacifica': pacifica_volume is not None
        })

    print(" " * 50, end='\r')  # Clear the "Processing..." line

    # Print table header
    print(f"{Colors.BOLD}{'Symbol':<10} {'Lighter (24h)':<20} {'Pacifica (24h)':<20} {'Lighter USD':<20} {'Pacifica USD':<20} {'Available On':<20}{Colors.RESET}")
    print(f"{Colors.BOLD}{'-'*110}{Colors.RESET}")

    # Print results
    total_lighter_usd = 0.0
    total_pacifica_usd = 0.0

    for result in results:
        symbol = result['symbol']
        lighter_vol = result['lighter_volume']
        pacifica_vol = result['pacifica_volume']
        price = result['price']

        # Calculate USD volumes (volume already in base tokens, multiply by price)
        lighter_usd = (lighter_vol * price) if (lighter_vol is not None and price is not None and price > 0) else None
        pacifica_usd = (pacifica_vol * price) if (pacifica_vol is not None and price is not None and price > 0) else None

        if lighter_usd:
            total_lighter_usd += lighter_usd
        if pacifica_usd:
            total_pacifica_usd += pacifica_usd

        # Format availability
        availability = []
        if result['on_lighter']:
            availability.append(f"{Colors.GREEN}Lighter{Colors.RESET}")
        if result['on_pacifica']:
            availability.append(f"{Colors.GREEN}Pacifica{Colors.RESET}")

        if not availability:
            availability_str = f"{Colors.RED}None{Colors.RESET}"
        else:
            availability_str = ", ".join(availability)

        print(f"{symbol:<10} {format_volume(lighter_vol):<30} {format_volume(pacifica_vol):<30} "
              f"{format_usd_volume(lighter_usd, 1):<30} {format_usd_volume(pacifica_usd, 1):<30} "
              f"{availability_str}")

    # Print summary
    print(f"{Colors.BOLD}{'-'*110}{Colors.RESET}")
    print(f"\n{Colors.BOLD}SUMMARY:{Colors.RESET}")
    print(f"  Total Lighter 24h Volume:  {Colors.GREEN}${total_lighter_usd:,.0f}{Colors.RESET}")
    print(f"  Total Pacifica 24h Volume: {Colors.GREEN}${total_pacifica_usd:,.0f}{Colors.RESET}")
    print(f"  Symbols on Lighter:        {Colors.CYAN}{sum(1 for r in results if r['on_lighter'])}/{len(symbols)}{Colors.RESET}")
    print(f"  Symbols on Pacifica:       {Colors.CYAN}{sum(1 for r in results if r['on_pacifica'])}/{len(symbols)}{Colors.RESET}")
    print(f"  Symbols on Both:           {Colors.GREEN}{sum(1 for r in results if r['on_lighter'] and r['on_pacifica'])}/{len(symbols)}{Colors.RESET}")

    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*100}{Colors.RESET}\n")

    # Close API client
    try:
        await api_client.close()
    except:
        pass


if __name__ == "__main__":
    asyncio.run(main())
