# 🤖 Lighter-Pacifica Cross-Exchange Funding Rate Delta Neutral Bot

Automated delta-neutral bot that captures funding rate spreads between Lighter and Pacifica perpetual futures exchanges.
Can also be used to farm trading volume while limiting risk (refreshes positions every `hold_duration_hours=12` hours by default, but can be changed).

**💰 Support this project**:
- **Lighter**: Affiliate link to Support this project : ⚡Trade on Lighter – Spot & Perpetuals, 100% decentralized, no KYC, and ZERO fees – [https://app.lighter.xyz/?referral=FREQTRADE](https://app.lighter.xyz/?referral=FREQTRADE) (I’ll give you 100% kickback with this link)
- **Pacifica**: Sign up at [app.pacifica.fi](https://app.pacifica.fi/) and use one of the following referral codes when registering (if one is already taken, try another):
  ```
  BYVJRCM791XFCF5K
  ENPVKJ1WAVYNV2Z0
  6HR2WM4C0JQ7D39Q
  411J9J7CYNFZN3SX
  2K7D40A9H53M2TJT
  S1G3A2063Q7410BV
  XH2V3VY9CQ7535CX
  EK8NXX12VDKJJWNK
  6NBS6TT7Y1SV2P53
  E5ZYTD2FVXJA123W
  ```

## 📊 Strategy

The bot maintains market-neutral positions by going **long on the exchange with lower funding rate** and **short on the exchange with higher funding rate**, collecting the funding rate differential while minimizing directional price risk.
Can also be used to farm trading volume while limiting risk (refreshes positions every `hold_duration_hours=12` hours by default, but can be changed).

**💡 Example**: If Lighter funding is +10% APR and Pacifica is +50% APR:
- Long position on Lighter (pay 10%)
- Short position on Pacifica (receive 50%)
- Net profit: ~40% APR

## 🚀 Quick Start

### 1. 📦 Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. 🔐 Configure Environment

Copy `.env.example` to `.env` and add your credentials:

```bash
cp .env.example .env
```

Edit `.env` with your API keys:
- **Lighter**: `API_KEY_PRIVATE_KEY`, `ACCOUNT_INDEX`, `API_KEY_INDEX`
- **Pacifica**: `SOL_WALLET`, `API_PUBLIC`, `API_PRIVATE`

### 3. ⚙️ Configure Bot

Edit `bot_config.json`:

```json
{
  "symbols_to_monitor": ["BTC", "ETH", "SOL", "ASTER", ...],
  "leverage": 1,
  "base_capital_allocation": 100.0,
  "hold_duration_hours": 12.0,
  "min_net_apr_threshold": 5.0
}
```

**Key Parameters**:
- `base_capital_allocation`: Base capital in USD (actual position = base × leverage × 0.98 buffer)
- `leverage`: **LOCKED AT 1X** (Hardcoded in bot logic for safety)
- `hold_duration_hours`: How long to hold each position
- `min_net_apr_threshold`: Minimum funding spread to open position

### 4. ▶️ Run

```bash
python lighter_pacifica_hedge.py
```

## 🐳 Docker Deployment

```bash
# Build the image
docker-compose build

# Build and start
docker-compose up -d

# View logs
docker-compose logs -f hedge-bot

# Stop
docker-compose down

# Rebuild after code changes
docker-compose build && docker-compose up -d
```

## 🔄 How It Works

1. **🔍 Analyze**: Fetches funding rates from both exchanges every cycle and prints a funding APR table (Lighter 8h rate converted to hourly, Pacifica hourly rate)
2. **🎯 Select**: Chooses symbol with highest net APR above threshold
3. **📈 Open**: Opens delta-neutral position (long/short) with synchronized leverage
4. **⏱️ Hold**: Monitors position health, PnL, and stop-loss for configured duration
5. **📉 Close**: Closes both positions simultaneously
6. **⏸️ Wait**: Brief cooldown before next cycle

## 🛡️ Safety Features

- ✅ **1X Leverage Lock**: Strictly enforces 1X leverage for maximum safety
- ✅ **Leverage Synchronization**: Both exchanges use identical leverage
- ✅ **Dynamic Stop-Loss**: Tighter stops at higher leverage (~60% capital loss trigger), **triggered by worst leg PnL** to protect against one-sided losses
- ✅ **2% Safety Buffer**: Automatic reduction of base capital allocation
- ✅ **Symbol Filtering**: Only trades symbols available on both exchanges with sufficient liquidity
- ✅ **Volume Filtering**: Automatically filters symbols with less than $50M 24h trading volume on Pacifica (checked at startup and before each cycle)
- ✅ **Spread Filtering**: Excludes symbols with spreads exceeding 0.15% between exchanges to minimize slippage (checked at startup and before each cycle)
- ✅ **State Recovery**: Automatically recovers position state after restart and triggers emergency close if mismatches are detected
- ✅ **Quantity Precision**: Uses coarser step size to ensure identical quantities
- ✅ **Long-term PnL Tracking**: Tracks initial capital and displays cumulative performance across all cycles
- ✅ **Emergency Closer**: `python emergency_close.py --force` will flatten all legs on both venues, sharing the same env defaults as the bot

## 📈 Funding Calculations

- Lighter API returns cumulative **8-hour** funding; the bot and test suite convert this to hourly by dividing by 8 before annualising (×8760).
- Pacifica API returns **hourly** funding; the bot multiplies directly by 24×365 to derive APR.
- Funding snapshots are logged at startup, before opening a position, and during position monitoring so you can compare both exchanges quickly.

## 💵 Position Sizing Examples

With `base_capital_allocation: 100`:

| Leverage | Safety Buffer | Position Size per Exchange |
|----------|---------------|----------------------------|
| 1x       | $98           | $98                        |
| 3x       | N/A           | (Locked at 1X)             |
| 5x       | N/A           | (Locked at 1X)             |
| 10x      | N/A           | (Locked at 1X)             |

Position size is further reduced if insufficient margin available (uses 95% of max available).

## ⛔ Stop-Loss Levels

Stop-loss is **triggered by the worst leg PnL** (not total PnL) to protect against one-sided losses that could lead to liquidation.

| Leverage | Stop-Loss % | Capital Loss at Trigger | Buffer Before Liquidation |
|----------|-------------|-------------------------|---------------------------|
| 1x       | -50%        | 50%                     | ~50%                      |
| 3x       | -20%        | 60%                     | ~40%                      |
| 5x       | -12%        | 60%                     | ~40%                      |
| 10x      | -6%         | 60%                     | ~40%                      |
| 20x      | -3%         | 60%                     | ~40%                      |

**When triggered**: Both positions are immediately closed via market orders, PnL is calculated, and the bot enters a waiting period (5 minutes by default) before starting a new cycle.

## 📊 Monitoring

The bot displays comprehensive color-coded status during position holding (every 60 seconds by default):

<img src="screen.png" alt="Bot Status Display" width="800">

**Status includes:**
- Position timing and time remaining
- Position sizes and notional value
- Account balances and total equity
- **Long-term PnL** since bot start (tracked via initial capital)
- Current leverage on both exchanges
- Real-time funding rates and spread
- Individual leg PnL (Lighter and Pacifica)
- Total unrealized PnL
- **Risk metrics**: Stop-loss level, current PnL %, worst leg performance, distance to stop-loss

## 🚨 Emergency Position Closer

Close all open positions on both exchanges:

```bash
# Interactive mode - shows positions and asks to press Enter to confirm
python emergency_close.py

# Close specific symbol only
python emergency_close.py --symbol BTC

# Close all without confirmation
python emergency_close.py --force

# Preview without executing
python emergency_close.py --dry-run
```

The script scans symbols from `bot_config.json` and displays all open positions with PnL. In interactive mode, simply press **Enter** to confirm closing (or Ctrl+C to cancel).

## 📊 Utility Scripts

Check market conditions before running the bot:

```bash
# Check 24h trading volume for all symbols
python check_24h_volume.py

# Check mid-price spreads between exchanges
python check_spreads.py

# Use custom config file
python check_24h_volume.py --config custom_config.json
python check_spreads.py --config custom_config.json
```

**check_24h_volume.py**: Displays 24h trading volume in both base currency and USD for all symbols on both exchanges. Helps identify liquid markets.

**check_spreads.py**: Shows bid/ask/mid prices and spread percentages between Lighter and Pacifica. Helps identify symbols with tight spreads for optimal trading.

## 🧪 Testing

Test your setup with the included test suite:

```bash
# Test Lighter connection and balance
python test/test_lighter_balance.py

# Test Pacifica connection and balance
python test/test_pacifica_balance.py

# Test funding rates
python test/test_lighter_funding.py
python test/test_pacifica_funding.py

# Test positions
python test/test_lighter_positions.py
python test/test_pacifica_positions.py

# Full lifecycle test (opens and closes positions - uses real funds!)
python test/test_lighter_market_order.py
python test/test_pacifica_market_order.py
```

See `test/README_TESTS.md` for detailed documentation on all available tests.

**Note**: Market order tests place real orders and use real funds. Start with small amounts.

## 📁 Files

- `lighter_pacifica_hedge.py` - Main bot
- `emergency_close.py` - Emergency position closer
- `check_24h_volume.py` - Volume checker utility
- `check_spreads.py` - Spread checker utility
- `lighter_client.py` - Lighter exchange wrapper
- `pacifica_client.py` - Pacifica exchange client
- `bot_config.json` - Configuration
- `bot_state_lighter_pacifica.json` - Persistent state (auto-created)
- `logs/lighter_pacifica_hedge.log` - Log file (resets on start)

## ⚠️ Important Notes

- **Single Position**: Bot manages one position at a time
- **Symbol Filtering**: Symbols not on both exchanges are automatically ignored
- **Volume & Spread Filtering**: Checked at startup and before each cycle to ensure liquid, efficient markets
  - Minimum $50M 24h volume on Pacifica
  - Maximum 0.15% spread between exchanges
- **Cycle Tracking**: Cycle number persists across restarts
- **Initial Capital**: Captured at first run and used for long-term PnL calculation
- **Monitoring Frequency**: Position checked every 60 seconds by default (`check_interval_seconds`)
- **Stop-Loss Protection**: Based on worst performing leg, not total PnL
- **Log Reset**: Log file resets on every bot start
- **UTC Timestamps**: All times displayed in UTC

## 📚 Documentation

- `CLAUDE.md` - Detailed architecture and development guide for Claude Code
- `test/README_TESTS.md` - Test suite documentation
- `.env.example` - Environment variable template

## 📋 Requirements

- Python 3.12+
- Active accounts on Lighter and Pacifica
- Sufficient balance on both exchanges (~$50+ recommended per exchange)
- API credentials with trading permissions

## 📜 License

This bot is for educational and research purposes. Use at your own risk. Always test with small amounts first.






