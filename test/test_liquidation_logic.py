import asyncio
import logging
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lighter_pacifica_hedge import RotationBot, BotState, BotConfig

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@pytest.mark.asyncio
async def test_liquidation_prevention_on_api_error():
    """
    Verify that the bot does NOT trigger emergency close when an API error occurs
    during position monitoring.
    """
    # Mock dependencies
    with patch('lighter_pacifica_hedge.PacificaClient') as MockPacificaClient, \
         patch('lighter_pacifica_hedge.lighter') as mock_lighter, \
         patch('lighter_pacifica_hedge.get_lighter_balance') as mock_get_balance, \
         patch('lighter_pacifica_hedge.get_lighter_open_size') as mock_get_size, \
         patch('lighter_pacifica_hedge.get_lighter_position_pnl') as mock_get_pnl, \
         patch('lighter_pacifica_hedge.get_lighter_funding_rate') as mock_get_funding:

        # Setup mocks
        mock_pacifica = MockPacificaClient.return_value
        mock_pacifica.get_position = AsyncMock(return_value={'qty': 1.0})
        mock_pacifica.get_funding_rate = MagicMock(return_value=0.01)
        
        # Mock Lighter API client
        mock_lighter_client = MagicMock()
        mock_lighter_client.close = AsyncMock()
        mock_lighter.ApiClient.return_value = mock_lighter_client
        
        # Mock SignerClient
        mock_signer = MagicMock()
        mock_lighter.SignerClient.return_value = mock_signer
        mock_signer.check_client.return_value = None  # Success
        
        # Mock balance to be healthy
        mock_get_balance.return_value = (1000.0, 1000.0)
        
        # CRITICAL: Mock get_lighter_open_size to RAISE an exception (simulating API failure)
        mock_get_size.side_effect = RuntimeError("Simulated Lighter API Error")
        
        # Initialize bot
        bot = RotationBot("test_state.json", "bot_config.json")
        bot.state_mgr.state["current_position"] = {
            "symbol": "SOL",
            "lighter_market_id": 1,
            "opened_at": "2025-01-01T00:00:00Z",
            "target_close_at": "2026-01-01T00:00:00Z", # Future date
            "notional": 100.0,
            "leverage": 1
        }
        bot.state_mgr.set_state(BotState.HOLDING)
        
        # Mock emergency close to verify it's NOT called
        bot.emergency_close_all = AsyncMock()
        
        # Run monitor_position
        await bot.monitor_position()
        
        # Verification
        # 1. Emergency close should NOT be called
        if bot.emergency_close_all.called:
            print("FAILURE: emergency_close_all was called!")
        else:
            print("SUCCESS: emergency_close_all was NOT called.")
            
        assert not bot.emergency_close_all.called, "Emergency close should not be triggered on API error"
        
        # 2. State should remain HOLDING (not ERROR)
        assert bot.state_mgr.get_state() == BotState.HOLDING, "Bot state should remain HOLDING"
        print(f"Bot state: {bot.state_mgr.get_state()}")

@pytest.mark.asyncio
async def test_true_liquidation_detection():
    """
    Verify that the bot DOES trigger emergency close when position size is actually 0
    (simulating liquidation).
    """
    # Mock dependencies
    with patch('lighter_pacifica_hedge.PacificaClient') as MockPacificaClient, \
         patch('lighter_pacifica_hedge.lighter') as mock_lighter, \
         patch('lighter_pacifica_hedge.get_lighter_balance') as mock_get_balance, \
         patch('lighter_pacifica_hedge.get_lighter_open_size') as mock_get_size, \
         patch('lighter_pacifica_hedge.get_lighter_position_pnl') as mock_get_pnl, \
         patch('lighter_pacifica_hedge.get_lighter_funding_rate') as mock_get_funding:

        # Setup mocks
        mock_pacifica = MockPacificaClient.return_value
        mock_pacifica.get_position = AsyncMock(return_value={'qty': 1.0}) # Pacifica still has position
        
        # Mock Lighter API client
        mock_lighter_client = MagicMock()
        mock_lighter_client.close = AsyncMock()
        mock_lighter.ApiClient.return_value = mock_lighter_client

        # Mock SignerClient
        mock_signer = MagicMock()
        mock_lighter.SignerClient.return_value = mock_signer
        mock_signer.check_client.return_value = None  # Success
        
        # Mock balance
        mock_get_balance.return_value = (1000.0, 1000.0)
        
        # CRITICAL: Mock get_lighter_open_size to return 0.0 (simulating liquidation/closure)
        mock_get_size.return_value = 0.0
        mock_get_pnl.return_value = 0.0
        
        # Initialize bot
        bot = RotationBot("test_state.json", "bot_config.json")
        # Ensure grace period is passed
        bot.config.liquidation_check_grace_period_seconds = 0 
        
        bot.state_mgr.state["current_position"] = {
            "symbol": "SOL",
            "lighter_market_id": 1,
            "opened_at": "2025-01-01T00:00:00Z",
            "target_close_at": "2026-01-01T00:00:00Z", # Future date
            "notional": 100.0,
            "leverage": 1
        }
        bot.state_mgr.set_state(BotState.HOLDING)
        
        # Mock close_position (which is called on liquidation)
        bot.close_position = AsyncMock()
        
        # Run monitor_position
        await bot.monitor_position()
        
        # Verification
        # 1. close_position should be called with is_emergency=True
        if bot.close_position.called:
            print("SUCCESS: close_position was called.")
        else:
            print("FAILURE: close_position was NOT called!")
            
        assert bot.close_position.called, "Close position should be triggered on liquidation"
        assert bot.close_position.call_args[1]['is_emergency'] == True

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    print("\n--- Test 1: API Error Prevention ---")
    loop.run_until_complete(test_liquidation_prevention_on_api_error())
    print("\n--- Test 2: True Liquidation Detection ---")
    loop.run_until_complete(test_true_liquidation_detection())
