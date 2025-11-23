import asyncio
import logging
import sys
from unittest.mock import MagicMock, AsyncMock

# Mock external dependencies before importing bot
sys.modules['lighter'] = MagicMock()
sys.modules['pacifica_sdk'] = MagicMock()
sys.modules['pacifica_sdk.common.constants'] = MagicMock()
sys.modules['pacifica_sdk.common.utils'] = MagicMock()
sys.modules['dotenv'] = MagicMock()
sys.modules['solders'] = MagicMock()
sys.modules['solders.keypair'] = MagicMock()

# Import bot classes
from lighter_pacifica_hedge import StateManager, BotState, recover_state, BotConfig

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_resilience():
    print("\n=== Testing Bot Resilience to API Failures ===\n")
    
    # Setup mocks
    state_mgr = MagicMock(spec=StateManager)
    state_mgr.state = {"current_position": None}
    state_mgr.get_state.return_value = BotState.IDLE
    
    lighter_client = AsyncMock()
    pacifica_client = MagicMock()
    
    # Mock config and env
    config = BotConfig(symbols_to_monitor=["BTC"])
    env = {"ACCOUNT_INDEX": "0"}
    market_cache = {"BTC": 1}
    
    # --- Test Case 1: Lighter API Failure ---
    print("Test Case 1: Lighter API Failure")
    
    # Mock Lighter to raise exception
    # We need to mock get_lighter_open_size which is imported in the bot
    # Since we can't easily mock the imported function directly in the script without patching,
    # we will rely on the fact that recover_state calls scan_symbols_for_positions
    
    # But wait, scan_symbols_for_positions is in the same file as recover_state.
    # We need to mock the CLIENT calls inside scan_symbols_for_positions.
    
    # Mock pacifica to return no position (success)
    pacifica_client.get_position = AsyncMock(return_value=None)
    
    # Mock Lighter account api to raise exception
    # The bot creates a new AccountApi instance. We need to mock what it does.
    # Actually, lighter_pacifica_hedge imports get_lighter_open_size.
    # We can patch it in the module.
    
    import lighter_pacifica_hedge
    
    original_get_lighter = lighter_pacifica_hedge.get_lighter_open_size
    
    try:
        # Simulate Lighter failure
        async def mock_fail(*args, **kwargs):
            raise RuntimeError("Simulated Lighter API Failure")
        
        lighter_pacifica_hedge.get_lighter_open_size = mock_fail
        
        # Run recover_state
        result = await recover_state(state_mgr, lighter_client, pacifica_client, config, env, market_cache)
        
        if result is False and state_mgr.set_state.call_args[0][0] == BotState.ERROR:
            print("✅ PASS: Bot entered ERROR state on Lighter failure")
        else:
            print(f"❌ FAIL: Result={result}, State={state_mgr.set_state.call_args}")
            
    finally:
        lighter_pacifica_hedge.get_lighter_open_size = original_get_lighter

    # --- Test Case 2: Pacifica API Failure ---
    print("\nTest Case 2: Pacifica API Failure")
    
    # Reset state mock
    state_mgr.set_state.reset_mock()
    
    # Mock Pacifica to raise exception
    pacifica_client.get_position = AsyncMock(side_effect=RuntimeError("Simulated Pacifica API Failure"))
    
    # Run recover_state
    result = await recover_state(state_mgr, lighter_client, pacifica_client, config, env, market_cache)
    
    if result is False and state_mgr.set_state.call_args[0][0] == BotState.ERROR:
        print("✅ PASS: Bot entered ERROR state on Pacifica failure")
    else:
        print(f"❌ FAIL: Result={result}, State={state_mgr.set_state.call_args}")

    # --- Test Case 3: Success (No Positions) ---
    print("\nTest Case 3: Success (No Positions)")
    
    # Reset state mock
    state_mgr.set_state.reset_mock()
    
    # Mock success
    pacifica_client.get_position = AsyncMock(return_value=None)
    
    async def mock_success(*args, **kwargs):
        return 0.0
    lighter_pacifica_hedge.get_lighter_open_size = mock_success
    
    # Run recover_state
    result = await recover_state(state_mgr, lighter_client, pacifica_client, config, env, market_cache)
    
    if result is True and state_mgr.set_state.call_args[0][0] == BotState.IDLE:
        print("✅ PASS: Bot entered IDLE state when no positions found")
    else:
        print(f"❌ FAIL: Result={result}, State={state_mgr.set_state.call_args}")

if __name__ == "__main__":
    asyncio.run(test_resilience())
