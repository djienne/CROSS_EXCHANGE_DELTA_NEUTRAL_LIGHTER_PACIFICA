# Test Suite Documentation

This directory contains test scripts for both Pacifica and Lighter exchanges.

## Lighter Test Scripts

### 1. `test_lighter_balance.py`
Tests fetching account balance from Lighter.

**What it tests:**
- WebSocket connection to Lighter
- Fetching equity and available balance
- Balance data parsing

**Usage:**
```bash
python test/test_lighter_balance.py
```

**Expected output:**
```
Fetching Lighter balances...

Equity: $1234.56
Available Balance: $987.65
Explicit Equity: 1234.56
Explicit Available Balance: 987.65
```

---

### 2. `test_lighter_funding.py`
Tests fetching funding rates from Lighter for all configured symbols.

**What it tests:**
- Market details retrieval
- Funding rate fetching for multiple symbols
- APR calculation (8-hour rate scaled to hourly × 8760 periods/year)
- Symbol ranking by funding rate

**Usage:**
```bash
python test/test_lighter_funding.py
```

**Expected output:**
```
Fetching funding rates from Lighter...

--- APR Ranking (based on 8-hour funding) ---
PAXG: 27.16% APR (8h rate: 0.0248%)
FARTCOIN: 20.15% APR (8h rate: 0.0184%)
BTC: 10.51% APR (8h rate: 0.0096%)
```

---

### 3. `test_lighter_positions.py`
Tests fetching open positions from Lighter.

**What it tests:**
- Position size retrieval
- Unrealized PnL calculation
- Position side detection (long/short)
- Handling of zero positions

**Usage:**
```bash
python test/test_lighter_positions.py
```

**Expected output:**
```
=== Lighter Positions ===

BTC:
  Side: LONG
  Quantity: 0.0050
  Unrealized PnL: $+2.34

ETH: No open position

SOL: No open position
```

---

### 4. `test_lighter_leverage.py`
Tests leverage mechanics on Lighter.

**What it tests:**
- How leverage works on Lighter (implicit, not explicit)
- Calculating effective leverage from positions
- Position notional value calculation
- Overall account leverage
- Maximum theoretical leverage (20x cap)
- Dynamic stop-loss levels based on leverage

**Usage:**
```bash
python test/test_lighter_leverage.py
```

**Expected output:**
```
=== Important: How Leverage Works on Lighter ===
Lighter does NOT have a 'set leverage' API like Pacifica.
Instead, leverage is implicit and determined by:
  1. Position Notional Value (qty × price)
  2. Account Equity (total account value)
  3. Effective Leverage = Position Notional / Account Equity

Leverage is controlled by ORDER SIZE, not a separate setting.

=== Test 1: Account Information ===
Total Equity: $1234.56
Available Balance: $987.65
Used Margin: $246.91
Margin Usage: 20.00%

=== Test 2: Current Positions and Effective Leverage ===

BTC:
  Side: LONG
  Quantity: 0.0050
  Current Price: $65432.0000
  Notional: $327.16
  Position Leverage: 0.27x
  Unrealized PnL: $+2.34

ETH: No open position

=== Test 3: Overall Account Leverage ===
Total Notional Across All Positions: $327.16
Account Equity: $1234.56
Overall Account Leverage: 0.27x
```

---

### 5. `test_lighter_market_order.py`
Full lifecycle test: opens and closes market positions.

**What it tests:**
- Aggressive market order placement (with spread crossing)
- Position opening with calculated quantities
- Position monitoring
- Position closing with reduce-only orders
- Balance change tracking

**Usage:**
```bash
python test/test_lighter_market_order.py
```

**Expected output:**
```
=== Initial Account State ===
Total Equity: $1234.56
Available Balance: $987.65

=== Opening Market Positions ===

BTC:
  Ask Price: $65432.10
  Target Notional: $20.00
  Quantity: 0.0003
  [OK] Order placed successfully

=== Waiting for 5 seconds ===

=== Checking Open Positions ===

BTC:
  Side: LONG
  Quantity: 0.0003
  Unrealized PnL: $+0.05

=== Closing Market Positions ===

BTC:
  Closing 0.0003 BTC (sell)
  [OK] Close order placed successfully

=== Final Account State ===
Total Equity: $1234.61
Available Balance: $987.70
Change in Equity: $+0.05

[SUCCESS] All positions successfully closed
```

---

### 6. `test_lighter_orderbook.py`
Tests orderbook data retrieval from Lighter.

**What it tests:**
- Best bid/ask fetching
- Market metadata (market ID, ticks)
- Spread calculation
- Mid-price calculation

**Usage:**
```bash
python test/test_lighter_orderbook.py
```

**Expected output:**
```
=== Fetching Orderbook Data ===

BTC:
  Market ID: 123
  Best Bid: $65430.0000
  Best Ask: $65432.0000
  Mid Price: $65431.0000
  Spread: $2.0000 (0.003%)
  Price Tick: 0.01
  Amount Tick: 0.0001
```

---

## Pacifica Test Scripts

### 1. `test_pacifica_balance.py`
Tests fetching account balance from Pacifica.

**Usage:**
```bash
python test/test_pacifica_balance.py
```

---

### 2. `test_pacifica_leverage.py`
Tests leverage management on Pacifica (getting max leverage, setting leverage).

**Usage:**
```bash
python test/test_pacifica_leverage.py
```

---

### 3. `test_pacifica_funding.py`
Tests fetching funding rates from Pacifica and ranking by APR.

**What it tests:**
- Funding fee retrieval for configured symbols
- APR calculation from hourly funding rates (×8760)
- Ranking symbols by funding attractiveness

**Usage:**
```bash
python test/test_pacifica_funding.py
```

**Expected output:**
```
Fetching funding fees...

--- APR Ranking (based on hourly funding) ---
ASTER: 12.45% APR (hourly rate: 0.0014%)
BTC:   1.71% APR (hourly rate: 0.0002%)
```

---

### 4. `test_pacifica_market_order.py`
Full lifecycle test: opens and closes market positions on Pacifica.

**Usage:**
```bash
python test/test_pacifica_market_order.py
```

---

### 5. `test_pacifica_positions.py`
Tests fetching open positions from Pacifica.

**Usage:**
```bash
python test/test_pacifica_positions.py
```

---

## Environment Setup

All test scripts require environment variables to be set in `.env`:

### For Lighter Tests:
```
API_KEY_PRIVATE_KEY=your_lighter_api_key
ACCOUNT_INDEX=0
```

### For Pacifica Tests:
```
SOL_WALLET=your_solana_wallet
API_PUBLIC=your_pacifica_public_key
API_PRIVATE=your_pacifica_private_key
```

## Running Tests

You can run tests from either the project root or the test directory:

```bash
# From project root
python test/test_lighter_balance.py

# From test directory
cd test
python test_lighter_balance.py
```

## Notes

- **Market Order Tests**: These tests actually place real orders and will use real funds. Start with small `notional_per_trade` values.
- **Balance Tests**: Read-only, safe to run anytime.
- **Position Tests**: Read-only, safe to run anytime.
- **Funding Tests**: Read-only, safe to run anytime.
- **Leverage Tests**: Read-only, safe to run anytime.
- **Orderbook Tests**: Read-only, safe to run anytime.

## Troubleshooting

### Connection Errors
- Check that environment variables are set correctly in `.env`
- Verify API keys have the necessary permissions
- Ensure network connectivity to exchange APIs

### Order Placement Failures
- Check account balance is sufficient
- Verify symbol is supported on the exchange
- Check if exchange is in maintenance mode

### Position Retrieval Issues
- Some symbols may not be available on both exchanges
- Position might have been liquidated
- API might be rate-limited
