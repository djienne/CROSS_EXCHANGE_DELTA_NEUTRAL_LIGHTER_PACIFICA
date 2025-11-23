import sys
import os
# Add parent directory to path to allow imports from parent folder
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

"""
Test script to get the current positions from Lighter.
"""
import asyncio
from test_lighter_market_order import run_trade_flow

"""
Legacy entry point retained for compatibility; delegates to the combined
market order + position verification flow.
"""

async def main() -> None:
    await run_trade_flow()


if __name__ == "__main__":
    asyncio.run(main())
