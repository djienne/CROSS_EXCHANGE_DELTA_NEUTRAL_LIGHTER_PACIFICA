# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a **delta-neutral funding rate arbitrage bot** that automatically captures funding rate spreads between Lighter and Pacifica exchanges. The bot opens simultaneous long/short positions on both exchanges to collect funding payments while remaining market-neutral.

## Architecture

### Core Components

**Main Bot (`lighter_pacifica_hedge.py`)**
- State machine with states: IDLE, ANALYZING, OPENING, HOLDING, CLOSING, WAITING, ERROR, SHUTDOWN
- Persistent state management via `bot_state_lighter_pacifica.json`
- Configuration loaded from `bot_config.json`
- Main loop: analyze funding rates → open best position → hold for duration → close → wait → repeat

**Exchange Connectors**
- `lighter_client.py`: Helper functions for Lighter SDK with WebSocket balance fetching, orderbook handling, position management, and order placement
- `pacifica_client.py`: Custom client for Pacifica DEX using Solana keypairs

**State Persistence**
- `StateManager` class handles JSON state file with atomic writes (temp file + os.replace)
- Tracks current position, cycle number (persistent across restarts), completed cycles, and cumulative stats
- Tracks `initial_capital` (total equity at first run) for long-term PnL calculation
- State recovery on startup by scanning both exchanges for existing positions

### Key Mechanisms

**Leverage System**
- Pacifica leverage is set before opening positions; Lighter leverage is per-order
- Final leverage = `min(config_leverage, lighter_max=20, pacifica_max, 20)` (20x hard cap enforced)
- Leverage is set on Pacifica before opening any positions (lines 792-801)
- Lighter assumes 20x max since it doesn't expose max leverage via API
- Warnings logged when leverage is reduced due to limits (lines 780-790)
- 20x hard cap at line 770: `MAX_ALLOWED_LEVERAGE = 20`

**Position Sizing**
- `base_capital_allocation` in config is BASE CAPITAL (not leveraged position size)
- 2% safety buffer applied automatically: `safe_base_capital = base_capital_allocation × 0.98`
- Position notional = `safe_base_capital × final_leverage` (lines 807-809)
- Further reduced if insufficient margin available (95% of max available)
- Example: $100 base at 3x leverage → $98 × 3 = $294 position on each exchange

**Stop-Loss Calculation** (lines 293-320)
- Dynamic based on leverage to trigger at ~60% capital loss, leaving 40% buffer before liquidation
- Formula: `max(2.0, 60.0 / leverage)` for leverage > 5
- Examples:
  - 1x: -50%, 3x: -20%, 5x: -12%, 10x: -6%, 15x: -4%, 20x: -3%
- **Trigger based on worst leg PnL** (not total PnL) for better risk protection (lines 1080-1131)
- When triggered: Both positions closed immediately via market orders, PnL calculated, bot enters WAITING state
- Uses same closing logic as `emergency_close.py` (tested and verified)
- Checked during monitoring phase every `check_interval_seconds` (default 60s)

**Symbol Filtering** (lines 785-891)
- Filters symbols in three stages: exchange availability, volume, and spread
- **Exchange Availability**: Only symbols available on both Lighter and Pacifica
- **Volume Filter**: Minimum $50M 24h trading volume on Pacifica (fetches via klines API)
- **Spread Filter**: Maximum 0.15% absolute spread between exchanges (fetches prices from both exchanges)
- Applied at startup during market cache initialization
- Re-applied before each cycle in `start_new_cycle()` to keep symbol list current
- Logs which symbols pass/fail each filter with detailed metrics
- Exits if no symbols meet all criteria
- Includes proper rate limiting (1s for Lighter, 0.1s for Pacifica)

**Quantity Synchronization** (lines 866-875)
- Uses coarser (larger) step size between both exchanges for quantity rounding
- Ensures identical quantity on both sides for true delta-neutral hedge
- Rounds DOWN using Decimal arithmetic to avoid rejection

**Funding Rate Decision** (lines 219-266)
- Fetches funding rates from both exchanges (hourly percentages)
- Converts to APR: `rate × 24 × 365 × 100`
- Goes LONG on exchange with lower funding rate, SHORT on higher
- Net APR = absolute difference between the two rates

**State Recovery** (lines 367-516)
- On startup, scans all configured symbols for existing positions
- Validates delta-neutral (sizes approximately opposite and equal within 5%)
- Recovers to HOLDING state if valid single position found
- Sets ERROR state if multiple positions or imbalanced positions detected
- Can automatically close orphan legs (positions on only one exchange) using `lighter_close_position()` function

**Cycle Tracking**
- `current_cycle_number` increments on each position open (line 913)
- Persistent across restarts (stored in state file)
- Displayed in status output during monitoring (line 1014)

**Market Cache**
- Lighter requires pre-fetching market IDs for all symbols (lines 585-604)
- Maps symbol names to market IDs (e.g., "BTC" → market_id)
- Built during startup before symbol filtering

### Lighter SDK Integration

**Order Placement** (lighter_client.py:342-412)
- Uses scaled integers: `base_amount = int(round(size / amount_tick))`, `price = int(price_value / price_tick)`
- Aggressive crossing: default `cross_ticks=100` to cross spread by 100 ticks for guaranteed fills
- Order signature: `SignerClient.create_order(market_index, client_order_index, base_amount, price, is_ask, order_type, time_in_force, reduce_only, trigger_price)`

**Position Closing** (lighter_client.py:415-492)
- Uses `reduce_only=1` (integer, not boolean)
- Same aggressive crossing logic as opening
- Determines close side based on position direction (long → sell, short → buy)

**Position Size Fetching** (lighter_client.py:234-282)
- Uses `pos.position * pos.sign` where sign is 1 for long, -1 for short
- Returns signed size (positive for long, negative for short)

**Balance Fetching** (lighter_client.py:114-151)
- WebSocket-based: subscribes to `user_stats/{account_index}`
- Checks for message types: `"update/user_stats"` and `"subscribed/user_stats"`
- Nested structure: `data.get("stats", {}).get("available_balance")`

**OrderBook Fetching** (lighter_client.py:75-111)
- Uses `lighter.WsClient` with callback pattern via `LighterOrderBookFetcher` class
- Callback receives market_id and order_book data
- Extracts best bid/ask from bids[0] and asks[0]

## Configuration

**bot_config.json**
- `symbols_to_monitor`: List of symbols (automatically filtered to those on both exchanges)
- `leverage`: Target leverage (auto-reduced if exceeds exchange limits or 20x hard cap)
- `base_capital_allocation`: Base capital in USD (actual position = base × leverage × 0.98 safety buffer)
- `hold_duration_hours`: How long to hold position (default 8h)
- `min_net_apr_threshold`: Minimum net APR % to open position (default 5%)
- `check_interval_seconds`: Health check frequency during HOLDING (default 60s)
- `wait_between_cycles_minutes`: Wait after closing before next cycle (default 5min)

**Parameter Name Migration**
- Old configs with `notional_per_position` automatically migrate to `base_capital_allocation` (lines 134-135)

**Environment Variables** (`.env`)
- `LIGHTER_WS_URL`: Lighter WebSocket URL
- `LIGHTER_BASE_URL`: Lighter REST API URL
- `LIGHTER_PRIVATE_KEY`: Lighter private key for signing orders
- `ACCOUNT_INDEX` or `LIGHTER_ACCOUNT_INDEX`: Account index (default 0)
- `SOL_WALLET`: Solana wallet address for Pacifica
- `API_PUBLIC`: Pacifica API public key
- `API_PRIVATE`: Pacifica API private key (base58 encoded)

## Commands

### Running the Bot

```bash
# Install dependencies
pip install -r requirements.txt

# Run the bot (standard)
python lighter_pacifica_hedge.py

# With custom config/state files
python lighter_pacifica_hedge.py --config-file custom_config.json --state-file custom_state.json
```

Default state file: `bot_state_lighter_pacifica.json`

### Emergency Position Closer

```bash
# Interactive mode - scans symbols from bot_config.json, shows positions, asks confirmation
python emergency_close.py

# Close specific symbol only
python emergency_close.py --symbol BTC

# Close all without confirmation
python emergency_close.py --force

# Preview without executing
python emergency_close.py --dry-run

# Use custom config file
python emergency_close.py --config custom_config.json
```

The `emergency_close.py` script:
- Scans only symbols listed in `bot_config.json` (not all available symbols)
- Displays all open positions with side, quantity, and unrealized PnL
- Requires pressing Enter to confirm before closing (unless `--force`)
- Provides colored output for easy readability
- Reports success/failure for each position closed
- Supports closing both Lighter and Pacifica positions

### Utility Scripts

```bash
# Check 24h trading volume for all symbols
python check_24h_volume.py

# Check mid-price spreads between exchanges
python check_spreads.py

# Use custom config file
python check_24h_volume.py --config custom_config.json
python check_spreads.py --config custom_config.json
```

**check_24h_volume.py**:
- Fetches 24h trading volume from both exchanges using candlestick/klines APIs
- Displays volume in base currency and USD for each symbol
- Color-coded output based on volume magnitude
- Shows which symbols meet the $50M volume threshold
- Includes rate limiting (1s for Lighter, 0.1s for Pacifica)

**check_spreads.py**:
- Fetches real-time prices from both exchanges
- Calculates mid-price spread: `|(lighter_mid - pacifica_mid) / pacifica_mid| * 100`
- Color-coded spreads: green < 0.1%, yellow 0.1-0.5%, red > 0.5%
- Shows direction arrows (↑ = Lighter more expensive, ↓ = Pacifica more expensive)
- Displays average absolute spread across all symbols
- Includes timeout handling and rate limiting

### Testing

Tests are in `test/` folder and work when run from either project root or test directory:

```bash
# Run all tests
pytest test/

# Run specific test file
python test/test_lighter_balance.py
python test/test_pacifica_leverage.py

# Run from test directory
cd test && python test_lighter_positions.py
```

Test files include `sys.path.insert()` to import from parent directory.

### Docker Deployment

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

Docker configuration:
- Container name: `lighter-pacifica-bot`
- Runs: `lighter_pacifica_hedge.py`
- Mounts: `./` to `/app/` for persistent state and logs
- Environment: Loads from `.env` file

### Logs

- Main bot log: `logs/lighter_pacifica_hedge.log` (resets on each script start, mode='w' at line 68)
- View state: `cat bot_state_lighter_pacifica.json`
- Docker logs: `docker-compose logs -f hedge-bot`

## Critical Safety Constraints

1. **Leverage Synchronization**: Pacifica leverage is set before opening; Lighter leverage is per-order. Bot enforces consistent leverage (lines ~1052-1091).
2. **20x Hard Cap**: Never exceeds 20x leverage regardless of config or exchange limits (line ~1053: `MAX_ALLOWED_LEVERAGE = 20`)
3. **2% Safety Buffer**: Base capital automatically reduced by 2% before leverage multiplication (line ~1097)
4. **Position Size Limits**: Auto-reduces if insufficient margin, uses 95% of available (lines ~1102-1115)
5. **Delta-Neutral Validation**: State recovery checks positions are opposite and equal within 5% (line ~508)
6. **Single Position Limit**: Bot only manages one position at a time. Multiple positions trigger ERROR state (lines ~421-424)
7. **Stop-Loss Buffer**: Dynamic stop-loss leaves ~40% buffer before liquidation (lines 316-329). **Triggered by worst leg PnL** to protect against one-sided losses
8. **Volume Filter**: Only trades symbols with $50M+ 24h volume on Pacifica (lines 802-830). Re-checked before each cycle.
9. **Spread Filter**: Excludes symbols with >0.15% spread between exchanges to minimize slippage (lines 831-891). Re-checked before each cycle.
10. **Aggressive Order Crossing**: Uses 100 tick crossing by default for guaranteed fills (lighter_client.py:51-72)

## Status Display

When in HOLDING state, comprehensive color-coded status shown every check interval (single log message, not multiple lines with timestamps):

```
Position Status: BTC (Cycle #1)

Timing:
  Opened:       2025-10-09 13:41:53 UTC
  Target Close: 2025-10-09 21:41:53 UTC
  Time Left:    8.0 hours

Position Sizes:
  Lighter:   +0.5000 BTC
  Pacifica:  -0.5000 BTC
  Notional:   $294.00 (per exchange)

Account Balances:
  Lighter:   $153.20 (Available: $120.00)
  Pacifica:  $65.18 (Available: $31.92)
  Total Equity: $218.38
  Total PnL:    $+18.38 (+9.18%) (since start)

Leverage:
  Lighter:   3.0x
  Pacifica:  3.0x

Funding Rates (APR):
  Lighter:   +10.95%
  Pacifica:  +66.74%
  Net Spread:  55.79%

Unrealized PnL:
  Lighter:   $+0.33
  Pacifica:  $-0.24
  Total PnL:   $+0.09

Risk Management:
  Stop-Loss:   -20.0% ($-19.62)
  Total PnL:    +0.09% ($+0.09)
  Lighter PnL:  $+0.33
  PA PnL:      $-0.24
  Worst Leg:   Pacifica ($-0.24, -2.45%)
  Distance to SL: $19.38 (98.8%)
```

## Common Development Patterns

**Adding New Exchange Methods**
- Add to `lighter_client.py` as standalone async functions or to `PacificaClient` class
- Use Lighter SDK components: `SignerClient` for orders, `ApiClient` for REST, `WsClient` for WebSocket
- Handle errors gracefully and log with appropriate level

**Modifying State Machine**
- State transitions use `state_mgr.set_state()` which auto-saves (line 197-200)
- Always update state BEFORE async operations that might fail
- Use `state_mgr.save()` after modifying nested state data (line 186-195)

**Config Changes**
- Update `BotConfig` dataclass (lines 100-108)
- Update defaults dict (lines 121-129)
- Add migration logic if renaming fields (lines 134-135 show example)
- Update `bot_config.json` with comment explaining new field

**Precision Handling**
- Use Decimal for all quantity/price calculations to avoid floating-point errors
- Round quantities DOWN with `ROUND_DOWN` (line 874)
- Get step sizes from both exchanges and use coarser one (lines 872-875)
- Lighter uses scaled integers: convert to int after dividing by tick size

**Status Display Modifications**
- Status output is consolidated into a single log message (lines 1022-1125) to avoid timestamp on every line
- Use color codes from `Colors` class (lines 54-63) for visual clarity
- Dynamic coloring based on values (e.g., green for profit, red for loss, time remaining colors)

## Key Code Locations

### lighter_pacifica_hedge.py
- **20x leverage hard cap**: Line ~1053
- **Initial capital tracking**: Lines ~927-940 (fetched at startup if missing)
- **Long-term PnL display**: Lines ~1362-1367
- **Leverage setting**: Lines ~1052-1091 (Pacifica only; Lighter is per-order)
- **2% safety buffer application**: Lines ~1097-1099
- **Position sizing calculation**: Lines ~1097-1115
- **Stop-loss formula**: Lines 316-329
- **Stop-loss check and trigger**: Lines ~1436-1440 (calls close_position if triggered)
- **Worst leg PnL calculation**: Lines ~1389-1413
- **State recovery**: Lines 398-561
- **Symbol filtering (3-stage)**: Lines 785-891
  - Exchange availability filter: Lines 786-800
  - Volume filter ($50M threshold): Lines 802-830
  - Spread filter (0.15% max): Lines 831-891
- **Periodic filtering in cycles**: Lines 1014-1028 (re-runs filtering before each cycle)
- **Market cache initialization**: Lines 897-920
- **Volume fetching method**: Lines 731-783
- **Quantity synchronization**: Lines ~1164-1172
- **Position opening**: Lines ~1131-1249
- **Position monitoring**: Lines ~1251-1451
- **Position closing**: Lines ~1453-1588
- **Status display (consolidated)**: Lines ~1331-1434
- **Risk management display**: Lines ~1404-1429
- **Config parameter migration**: Lines 146-148

### lighter_client.py
- **Tick rounding helpers**: Lines 24-72
- **Cross price calculation**: Lines 47-72 (100 tick default crossing)
- **OrderBook fetcher class**: Lines 75-111
- **Balance WebSocket**: Lines 114-151
- **Market details fetching**: Lines 154-193
- **Best bid/ask fetching**: Lines 196-231
- **Position size fetching**: Lines 234-282
- **Position PnL fetching**: Lines 285-339
- **Funding rate fetching**: Lines 196-231 (via MarketApi)
- **Aggressive order placement**: Lines 342-412
- **Position closing**: Lines 415-492

## Emergency Procedures

**Use emergency_close.py to close positions**:
```bash
python emergency_close.py  # Interactive with confirmation
python emergency_close.py --force  # No confirmation
```

**If bot crashes during OPENING/CLOSING**:
1. Run `python emergency_close.py --dry-run` to see open positions
2. Run `python emergency_close.py` to close them (press Enter to confirm)
3. Reset state: Edit `bot_state_lighter_pacifica.json` to set `"state": "IDLE"` and `"current_position": null`
4. Restart bot

**If ERROR state persists**:
- Bot retries recovery every 5 minutes
- Check logs for specific error
- Use `python emergency_close.py` to safely close positions
- May need manual position cleanup if delta-neutral constraint violated (>5% imbalance)

## Important Notes

- Log file resets on every script start (mode='w' at line 68)
- Cycle counter persists across restarts via state file
- Bot exits if no symbols meet all filtering criteria (exchange availability, volume, spread)
- **Volume & Spread Filtering**: Applied at startup and re-checked before each cycle (lines 1014-1028)
  - Volume threshold: $50M 24h on Pacifica
  - Spread threshold: 0.15% absolute difference
  - Symbols dynamically added/removed based on current market conditions
- All timestamps use UTC with proper timezone awareness
- PnL calculation compares entry balance to current balance after closing (lines ~1558-1575)
- Pacifica DOES support leverage setting via API (`/api/v1/account/leverage` endpoint)
- Lighter leverage is set per-order, not account-wide
- Old config files with `notional_per_position` automatically upgrade to `base_capital_allocation`
- Market cache is built at startup and required for all Lighter operations
- Default state file name: `bot_state_lighter_pacifica.json`
- Aggressive order crossing (100 ticks) ensures fills but may have higher slippage
- Utility scripts (`check_24h_volume.py`, `check_spreads.py`) help preview market conditions before running bot
- All logging from websockets, lighter_client, and third-party libraries set to WARNING level to reduce noise
