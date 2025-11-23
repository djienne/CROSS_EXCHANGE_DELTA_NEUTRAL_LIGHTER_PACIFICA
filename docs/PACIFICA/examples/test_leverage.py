#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test script to check and change leverage for a symbol.
Reads symbol from params/trading_config.json
"""

import os
import sys
import json
import time
import base58
import requests
from solders.keypair import Keypair
from dotenv import load_dotenv

# Set UTF-8 encoding for Windows console
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

# Add pacifica_sdk to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'pacifica_sdk'))
from common.utils import sign_message
from common.constants import REST_URL

# Load environment variables
load_dotenv()

SOL_WALLET = os.getenv("SOL_WALLET")
API_PRIVATE = os.getenv("API_PRIVATE")
API_PUBLIC = os.getenv("API_PUBLIC")

if not all([SOL_WALLET, API_PRIVATE, API_PUBLIC]):
    print("Error: Missing environment variables (SOL_WALLET, API_PRIVATE, API_PUBLIC)")
    sys.exit(1)

# Load agent wallet keypair
try:
    agent_keypair = Keypair.from_base58_string(API_PRIVATE)
except Exception as e:
    print(f"Error loading agent keypair: {e}")
    sys.exit(1)


def load_trading_config():
    """Load trading configuration from params/trading_config.json"""
    config_path = os.path.join(os.path.dirname(__file__), 'params', 'trading_config.json')
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        return config
    except FileNotFoundError:
        print(f"Error: Config file not found at {config_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in config file: {e}")
        sys.exit(1)


def get_account_settings():
    """Get current account leverage settings for all markets"""
    url = f"{REST_URL}/account/settings"
    params = {"account": SOL_WALLET}

    print(f"\n{'='*60}")
    print(f"Fetching account settings for {SOL_WALLET}")
    print(f"{'='*60}")

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        if data.get("success"):
            settings = data.get("data", [])

            if not settings:
                print("\n⚠️  No custom leverage settings found (using default max leverage for all markets)")
                return None

            print(f"\nFound {len(settings)} market(s) with custom settings:\n")
            for setting in settings:
                symbol = setting.get("symbol")
                leverage = setting.get("leverage")
                isolated = setting.get("isolated")
                margin_mode = "Isolated" if isolated else "Cross"
                print(f"  • {symbol:8s} | Leverage: {leverage:2d}x | Margin: {margin_mode}")

            return settings
        else:
            error = data.get("error", "Unknown error")
            print(f"Error: {error}")
            return None

    except requests.exceptions.RequestException as e:
        print(f"Request error: {e}")
        return None


def get_market_info(symbol):
    """Get market information including max leverage"""
    url = f"{REST_URL}/info"

    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        if data.get("success"):
            markets = data.get("data", [])

            # Find the specific symbol
            market_data = None
            for market in markets:
                if market.get("symbol") == symbol:
                    market_data = market
                    break

            if not market_data:
                print(f"\n❌ Error: Symbol '{symbol}' not found in market data")
                print(f"\nAvailable symbols: {', '.join([m.get('symbol') for m in markets[:10]])}...")
                return None

            max_leverage = market_data.get("max_leverage")
            min_order_size = market_data.get("min_order_size")
            max_order_size = market_data.get("max_order_size")
            isolated_only = market_data.get("isolated_only", False)

            print(f"\n{'='*60}")
            print(f"Market Information: {symbol}")
            print(f"{'='*60}")
            print(f"  Max Leverage: {max_leverage}x")
            print(f"  Min Order Size: ${min_order_size}")
            print(f"  Max Order Size: ${max_order_size}")
            print(f"  Isolated Only: {isolated_only}")

            return market_data
        else:
            error = data.get("error", "Unknown error")
            print(f"Error fetching market info: {error}")
            return None

    except requests.exceptions.RequestException as e:
        print(f"Request error: {e}")
        return None


def update_leverage(symbol, leverage, debug=False):
    """Update leverage for a specific symbol"""
    timestamp = int(time.time() * 1000)

    # Create signature header
    signature_header = {
        "type": "update_leverage",
        "timestamp": timestamp,
        "expiry_window": 5000
    }

    # Create signature payload (only the data fields, NOT account/agent_wallet)
    signature_payload = {
        "symbol": symbol,
        "leverage": leverage
    }

    # Sign the message
    try:
        message, signature = sign_message(signature_header, signature_payload, agent_keypair)
        if debug:
            print(f"\n[DEBUG] Signature message: {message}")
            print(f"[DEBUG] Signature: {signature[:50]}...")
    except Exception as e:
        print(f"Error signing message: {e}")
        return False

    # Build request header (authentication fields)
    request_header = {
        "account": SOL_WALLET,
        "agent_wallet": API_PUBLIC,
        "signature": signature,
        "timestamp": signature_header["timestamp"],
        "expiry_window": signature_header["expiry_window"]
    }

    # Build full request body
    request_body = {
        **request_header,
        **signature_payload
    }

    if debug:
        print(f"[DEBUG] Request body: {json.dumps(request_body, indent=2)}")

    # Send request
    url = f"{REST_URL}/account/leverage"

    print(f"\n{'='*60}")
    print(f"Updating Leverage: {symbol} → {leverage}x")
    print(f"{'='*60}")

    try:
        response = requests.post(url, json=request_body)
        if debug:
            print(f"[DEBUG] Response status: {response.status_code}")
            print(f"[DEBUG] Response body: {response.text}")

        response.raise_for_status()
        data = response.json()

        if data.get("success"):
            print(f"✅ Success! Leverage updated to {leverage}x for {symbol}")
            return True
        else:
            error = data.get("error", "Unknown error")
            code = data.get("code", "N/A")
            print(f"❌ Error: {error} (Code: {code})")
            return False

    except requests.exceptions.RequestException as e:
        print(f"❌ Request error: {e}")
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_data = e.response.json()
                print(f"   Error details: {error_data}")
            except:
                print(f"   Response text: {e.response.text}")
        return False


def main():
    print("\n" + "="*60)
    print("PACIFICA LEVERAGE TEST SCRIPT")
    print("="*60)

    # Load trading config
    config = load_trading_config()
    symbol = config.get("symbol")

    if not symbol:
        print("Error: No symbol specified in trading config")
        sys.exit(1)

    print(f"\nSymbol from config: {symbol}")

    # Get market info
    market_info = get_market_info(symbol)
    if not market_info:
        print("Failed to get market information")
        sys.exit(1)

    max_leverage = market_info.get("max_leverage")

    # Get current account settings
    current_settings = get_account_settings()

    # Find current leverage for this symbol
    current_leverage = None
    if current_settings:
        for setting in current_settings:
            if setting.get("symbol") == symbol:
                current_leverage = setting.get("leverage")
                break

    if current_leverage is None:
        print(f"\n⚠️  No custom leverage set for {symbol} (using default max leverage: {max_leverage}x)")
        current_leverage = max_leverage
    else:
        print(f"\n📊 Current leverage for {symbol}: {current_leverage}x")

    # Interactive prompt to change leverage
    print(f"\n{'='*60}")
    print("CHANGE LEVERAGE")
    print(f"{'='*60}")
    print(f"Current: {current_leverage}x")
    print(f"Max allowed: {max_leverage}x")
    print(f"\nNote: For open positions, you can only INCREASE leverage")

    try:
        new_leverage_input = input(f"\nEnter new leverage (1-{max_leverage}) or press Enter to skip: ").strip()

        if not new_leverage_input:
            print("\nSkipping leverage change.")
            return

        new_leverage = int(new_leverage_input)

        if new_leverage < 1 or new_leverage > max_leverage:
            print(f"❌ Error: Leverage must be between 1 and {max_leverage}")
            sys.exit(1)

        if new_leverage == current_leverage:
            print(f"⚠️  New leverage ({new_leverage}x) is the same as current leverage")
            return

        # Confirm
        confirm = input(f"\nConfirm: Change {symbol} leverage from {current_leverage}x to {new_leverage}x? (y/N): ").strip().lower()

        if confirm != 'y':
            print("\nCancelled.")
            return

        # Update leverage
        success = update_leverage(symbol, new_leverage)

        if success:
            # Re-fetch account settings to confirm
            print("\nVerifying update...")
            time.sleep(1)
            get_account_settings()

    except ValueError:
        print("❌ Error: Invalid input. Please enter a number.")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nCancelled by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
