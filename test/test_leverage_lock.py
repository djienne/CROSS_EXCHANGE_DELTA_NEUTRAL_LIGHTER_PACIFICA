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
async def test_leverage_lock():
    """
    Verify that the bot enforces 1X leverage regardless of configuration.
    """
    # Mock environment variables
    with patch.dict(os.environ, {
        "API_KEY_PRIVATE_KEY": "test_key",
        "ACCOUNT_INDEX": "0",
        "API_KEY_INDEX": "0",
        "SOL_WALLET": "test_wallet",
        "API_PUBLIC": "test_public",
        "API_PRIVATE": "test_private",
        "LIGHTER_BASE_URL": "https://api.lighter.xyz"
    }):
        # Mock dependencies
        with patch('lighter_pacifica_hedge.PacificaClient') as MockPacificaClient, \
         patch('lighter_pacifica_hedge.lighter') as mock_lighter, \
         patch('lighter_pacifica_hedge.get_lighter_balance') as mock_get_balance, \
         patch('lighter_pacifica_hedge.get_lighter_open_size') as mock_get_size, \
         patch('lighter_pacifica_hedge.get_lighter_position_pnl') as mock_get_pnl, \
         patch('lighter_pacifica_hedge.get_lighter_funding_rate') as mock_get_funding, \
         patch('lighter_pacifica_hedge.get_pacifica_balance') as mock_get_pa_balance, \
         patch('lighter_pacifica_hedge.fetch_funding_rates', new_callable=AsyncMock) as mock_fetch_funding:

            # Setup mocks
            mock_get_pa_balance.return_value = (1000.0, 1000.0)
            mock_fetch_funding.return_value = [{
                "symbol": "SOL",
                "lighter_market_id": 1,
                "net_apr": 10.0,
                "lighter_funding": 0.0,
                "pacifica_funding": 0.0
            }]
            mock_pacifica = MockPacificaClient.return_value
            mock_pacifica.get_position = AsyncMock(return_value={'qty': 0.0})
            mock_pacifica.get_funding_rate = MagicMock(return_value=0.01)
            mock_pacifica.get_max_leverage.return_value = 20
            mock_pacifica.set_leverage = MagicMock(return_value=True)
            
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
            
            # Initialize bot with HIGH leverage config
            config_data = {
                "symbols_to_monitor": ["SOL"],
                "leverage": 10, # Requesting 10x
                "base_capital_allocation": 100.0,
                "hold_duration_hours": 12.0,
                "min_net_apr_threshold": 5.0,
                "liquidation_check_grace_period_seconds": 30
            }
            
            # Patch json.load to return our test config
            with patch('json.load', return_value=config_data), \
                 patch('builtins.open', MagicMock()):
                
                bot = RotationBot("test_state.json", "bot_config.json")
                
                # Manually inject config to be sure
                bot.config.leverage = 10 
                
                # Mock _update_lighter_leverage
                bot._update_lighter_leverage = AsyncMock(return_value=True)
                
                # Mock market cache
                bot.market_cache = {"SOL": 1}
                
                # Mock opportunity finding to return a valid opportunity
                bot.find_best_opportunity = AsyncMock(return_value={
                    "symbol": "SOL",
                    "lighter_market_id": 1,
                    "net_apr": 10.0,
                    "lighter_funding": 0.0,
                    "pacifica_funding": 0.0
                })
                
                # Mock state manager save
                bot.state_mgr.save_state = MagicMock()
                
                # Mock symbol filtering to avoid volume/spread checks
                bot._filter_tradable_symbols = AsyncMock()
                
                # Mock log funding snapshot
                bot._log_funding_snapshot = MagicMock()
                
                # Enable debug logging
                logging.getLogger('lighter_pacifica_hedge').setLevel(logging.DEBUG)
                
                # Mock open_position to avoid actual trading logic
                bot.open_position = AsyncMock()
                
                # Run start_new_cycle
                # We need to mock state manager to allow transition
                bot.state_mgr.get_state = MagicMock(return_value=BotState.IDLE)
                
                # We only want to test the leverage calculation part, which happens in start_new_cycle
                # But start_new_cycle is complex. Let's inspect the code again.
                # The leverage logic is inside start_new_cycle.
                
                # Let's run start_new_cycle and intercept the calls
                await bot.start_new_cycle()
                
                # Verification
                
                # 1. Check Pacifica leverage set to 1
                mock_pacifica.set_leverage.assert_called_with("SOL", 1)
                print(f"Pacifica set_leverage called with: {mock_pacifica.set_leverage.call_args}")
                
                # 2. Check Lighter leverage set to 1
                bot._update_lighter_leverage.assert_called_with(1, 1)
                print(f"Lighter update_leverage called with: {bot._update_lighter_leverage.call_args}")
                
                # 3. Verify that despite config saying 10, we used 1
                assert bot.config.leverage == 10, "Config should still have the original value"

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(test_leverage_lock())
