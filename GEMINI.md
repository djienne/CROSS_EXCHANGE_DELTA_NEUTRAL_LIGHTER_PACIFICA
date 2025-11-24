# Lighter-Pacifica Cross-Exchange Hedge Bot

## Project Overview

This project is an automated delta-neutral trading bot designed to capture funding rate spreads between **Lighter** and **Pacifica** perpetual futures exchanges. It maintains market-neutral positions by going long on the exchange with the lower funding rate and short on the exchange with the higher rate.

### Key Features
*   **Delta Neutral Strategy:** Minimizes directional price risk by balancing long and short positions.
*   **Funding Rate Arbitrage:** Profits from the difference in funding rates between venues.
*   **Robust Safety Mechanisms:**
    *   **1X Leverage Lock:** Strictly enforces 1X leverage for maximum safety.
    *   **Dynamic Stop-Loss:** Triggers based on the *worst-performing leg* to protect against one-sided liquidation.
    *   **State Recovery:** Automatically restores position state after restarts using `bot_state_lighter_pacifica.json`.
    *   **Liquidity Filtering:** Checks 24h volume and spread tightness before entering trades.
*   **Real-time Monitoring:** Console dashboard displaying PnL, leverage, spread, and time remaining.

## Architecture & Logic

The bot operates in a continuous cycle managed by `lighter_pacifica_hedge.py`:

1.  **Analyze:** Fetches funding rates from both exchanges. Converts Lighter's 8h rate to an annualized APR and compares it with Pacifica's hourly-derived APR.
2.  **Select:** Identifies the symbol with the highest net APR spread that meets the `min_net_apr_threshold` and liquidity requirements.
3.  **Open:** Executes a delta-neutral position (Long on one, Short on the other) using synchronized leverage.
4.  **Hold:** Monitors the position for `hold_duration_hours`. continuously checking:
    *   **Health:** Connection status and API responsiveness.
    *   **PnL:** Real-time profit/loss of individual legs and the total position.
    *   **Stop-Loss:** Triggers an immediate close if the worst leg hits the defined threshold.
5.  **Close:** Flattens both positions simultaneously via market orders.
6.  **Wait:** Pauses for `wait_between_cycles_minutes` before restarting the analysis.

**State Management:**
The bot persists its state to `bot_state_lighter_pacifica.json`. On startup, it reads this file to determine if it should resume monitoring an existing position or start a new search.

## Setup & Configuration

### 1. Environment Variables (`.env`)
Required credentials for exchange access:
*   **Lighter:** `API_KEY_PRIVATE_KEY`, `ACCOUNT_INDEX`, `API_KEY_INDEX`
*   **Pacifica:** `SOL_WALLET`, `API_PUBLIC`, `API_PRIVATE`

### 2. Strategy Configuration (`bot_config.json`)
Controls the trading behavior:
*   `symbols_to_monitor`: List of assets to trade (e.g., `["BTC", "ETH", "SOL"]`).
*   `leverage`: **LOCKED AT 1X** (Hardcoded in bot logic for safety).
*   `base_capital_allocation`: Capital in USD allocated per trade *before* leverage.
*   `hold_duration_hours`: How long to keep the position open.
*   `min_net_apr_threshold`: Minimum spread % required to enter a trade.
*   `check_interval_seconds`: Frequency of health checks during the holding phase.

## Operational Guide

### Primary Commands

| Action | Command | Description |
| :--- | :--- | :--- |
| **Start Bot** | `python lighter_pacifica_hedge.py` | Launches the main hedging logic. |
| **Emergency Close** | `python emergency_close.py` | **CRITICAL:** Immediately closes all open positions on both exchanges. Use `--force` to skip confirmation. |
| **Check Liquidity** | `python check_24h_volume.py` | Lists 24h volume for configured symbols to ensure market activity. |
| **Check Spreads** | `python check_spreads.py` | Displays current bid/ask spreads to avoid high slippage. |

### Docker Deployment
```bash
docker-compose build
docker-compose up -d
docker-compose logs -f hedge-bot
```

## Testing Protocol

The `test/` directory contains specific scripts to verify every component. **Always run connection tests before starting the bot.**

### Connection & Balance (Safe)
*   `python test/test_lighter_balance.py`
*   `python test/test_pacifica_balance.py`

### Market Data (Safe)
*   `python test/test_lighter_funding.py` (Check Lighter funding rates)
*   `python test/test_pacifica_funding.py` (Check Pacifica funding rates)

### Trading Lifecycle (REAL FUNDS)
*   `python test/test_lighter_market_order.py`
*   `python test/test_pacifica_market_order.py`
*   *Note: These tests open and close small real positions.*

## Development Conventions

*   **Logs:** Execution logs are stored in `logs/lighter_pacifica_hedge.log` and reset on every startup.
*   **Error Handling:** The bot is designed to fail safe. If an API error occurs during the "Open" phase, it attempts to unwind any partial positions.
*   **Formatting:** Code should adhere to standard Python PEP 8 guidelines.
*   **Dependencies:** Managed via `requirements.txt`.

## Key Files
*   `lighter_pacifica_hedge.py`: Main entry point.
*   `emergency_close.py`: Safety tool for immediate liquidation.
*   `lighter_client.py` / `pacifica_client.py`: Exchange API wrappers.
*   `bot_config.json`: Strategy configuration.