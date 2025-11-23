"""
Cancel all orders on Pacifica DEX using agent wallet authentication.
"""
import os
import time
import requests
from dotenv import load_dotenv
from solders.keypair import Keypair

from pacifica_sdk.common.constants import REST_URL
from pacifica_sdk.common.utils import sign_message

# Load environment variables
load_dotenv()

API_URL = f"{REST_URL}/orders/cancel_all"

def cancel_all_orders(all_symbols=True, exclude_reduce_only=False):
    """
    Cancel all orders using agent wallet authentication.

    Args:
        all_symbols: Cancel orders across all symbols (default True)
        exclude_reduce_only: Exclude reduce-only orders from cancellation (default False)
    """
    # Get credentials from environment
    sol_wallet = os.getenv("SOL_WALLET")
    api_private = os.getenv("API_PRIVATE")
    api_public = os.getenv("API_PUBLIC")

    if not all([sol_wallet, api_private, api_public]):
        raise ValueError("Missing required environment variables: SOL_WALLET, API_PRIVATE, API_PUBLIC")

    # Generate agent keypair from private key
    agent_keypair = Keypair.from_base58_string(api_private)

    # Scaffold the signature header
    timestamp = int(time.time() * 1_000)

    signature_header = {
        "timestamp": timestamp,
        "expiry_window": 5_000,
        "type": "cancel_all_orders",
    }

    # Construct the signature payload
    signature_payload = {
        "all_symbols": all_symbols,
        "exclude_reduce_only": exclude_reduce_only,
    }

    # Sign the message with agent wallet
    message, signature = sign_message(signature_header, signature_payload, agent_keypair)

    # Construct the request with agent wallet authentication
    request_header = {
        "account": sol_wallet,  # Main wallet
        "agent_wallet": api_public,  # Agent wallet public key
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

    response = requests.post(API_URL, json=request, headers=headers)

    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")

    if response.status_code == 200:
        print("\n[SUCCESS] Successfully cancelled all orders")
    else:
        print(f"\n[ERROR] Failed to cancel orders")
        print(f"Request: {request}")
        print(f"\nDebug Info:")
        print(f"Main Wallet: {sol_wallet}")
        print(f"Agent Wallet: {api_public}")
        print(f"Message: {message}")
        print(f"Signature: {signature}")

    return response


if __name__ == "__main__":
    cancel_all_orders()
