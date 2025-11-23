import asyncio
import logging
import sys
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta, timezone

# Mock external dependencies
sys.modules['lighter'] = MagicMock()
sys.modules['pacifica_sdk'] = MagicMock()
sys.modules['pacifica_sdk.common.constants'] = MagicMock()
sys.modules['pacifica_sdk.common.utils'] = MagicMock()
sys.modules['dotenv'] = MagicMock()
sys.modules['solders'] = MagicMock()
sys.modules['solders.keypair'] = MagicMock()

# Import bot classes
from lighter_pacifica_hedge import StateManager, BotState, RotationBot, BotConfig
import lighter_pacifica_hedge

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_liquidation_detection():
    print("\n=== Testing Liquidation Detection ===\n")
    
    # Setup mocks
    state_mgr = MagicMock(spec=StateManager)
    state_mgr.state = {
        "current_position": {
            "symbol": "BTC",
            "opened_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(), # Opened 5 mins ago
            "target_close_at": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
            "lighter_market_id": 1,
            "notional": 100.0,
            "leverage": 3
        },
        "current_cycle_number": 1
    }
    state_mgr.get_state.return_value = BotState.HOLDING
    
    # Mock os.getenv and BotConfig.load_from_file to pass RotationBot init checks
    with patch('os.getenv') as mock_getenv, \
         patch('lighter_pacifica_hedge.BotConfig.load_from_file') as mock_load_config, \
         patch('os.path.exists') as mock_exists:
        
        def getenv_side_effect(key, default=None):
            if key in ["ACCOUNT_INDEX", "API_KEY_INDEX", "LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX"]:
                return "0"
            if key == "LIGHTER_WS_URL":
                return "wss://dummy"
            if key == "LIGHTER_BASE_URL":
                return "https://dummy"
            if key in ["LIGHTER_PRIVATE_KEY", "SOL_WALLET", "API_PUBLIC", "API_PRIVATE"]:
                return "dummy_key"
            return default if default is not None else "dummy_value"
            
        mock_getenv.side_effect = getenv_side_effect
        mock_exists.return_value = True
        mock_load_config.return_value = BotConfig(symbols_to_monitor=["BTC"])
        
        # Mock SignerClient check_client to return None (success)
        sys.modules['lighter'].SignerClient.return_value.check_client.return_value = None
        
        # Mock ApiClient close to be awaitable
        sys.modules['lighter'].ApiClient.return_value.close = AsyncMock()

        # Instantiate bot
        bot = RotationBot("dummy_state.json", "dummy_config.json")
    
    # Inject mocks into bot instance
    bot.state_mgr = state_mgr
    bot.env = {"ACCOUNT_INDEX": "0", "LIGHTER_WS_URL": "wss://dummy", "LIGHTER_BASE_URL": "https://dummy"}
    bot.pacifica_client = MagicMock()
    bot.lighter_signer = MagicMock()
    
    # Mock config with short grace period for the test
    bot.config = BotConfig(symbols_to_monitor=["BTC"], liquidation_check_grace_period_seconds=1)
    
    # Mock close_position to track calls
    bot.close_position = AsyncMock()
    
    # Mock helper functions in the module
    lighter_pacifica_hedge.get_lighter_balance = AsyncMock(return_value=(100.0, 100.0))
    lighter_pacifica_hedge.get_pacifica_balance = MagicMock(return_value=(100.0, 100.0))
    lighter_pacifica_hedge.get_lighter_funding_rate = AsyncMock(return_value=0.01)
    lighter_pacifica_hedge.get_position_pnl = AsyncMock(return_value={
        "lighter_unrealized_pnl": 0.0,
        "pacifica_unrealized_pnl": 0.0,
        "total_unrealized_pnl": 0.0
    })
    
    # --- Test Case: Liquidation on Lighter ---
    print("Test Case: Liquidation on Lighter (Lighter size = 0, Pacifica size != 0)")
    
    # Mock positions: Lighter = 0 (Liquidated), Pacifica = 0.1
    lighter_pacifica_hedge.get_lighter_open_size = AsyncMock(return_value=0.0)
    bot.pacifica_client.get_position = AsyncMock(return_value={'qty': 0.1})
    
    # Run monitor_position
    await bot.monitor_position()
    
    # Verify emergency close was triggered
    if bot.close_position.called and bot.close_position.call_args[1].get('is_emergency') is True:
        print("✅ PASS: Emergency close triggered")
    else:
        print(f"❌ FAIL: close_position not called correctly: {bot.close_position.call_args}")
        
    # Verify state transition to ERROR
    if state_mgr.set_state.called and state_mgr.set_state.call_args[0][0] == BotState.ERROR:
        print("✅ PASS: Bot entered ERROR state")
    else:
        print(f"❌ FAIL: State not set to ERROR: {state_mgr.set_state.call_args}")

if __name__ == "__main__":
    asyncio.run(test_liquidation_detection())
