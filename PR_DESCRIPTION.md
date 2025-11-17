# Fix Pacifica funding rate timing mismatch - use next_funding_rate instead of historical

## Summary

**Critical fix for Pacifica funding rate retrieval** - changes ONLY the Pacifica client, Lighter was already correct.

This PR fixes a timing mismatch where the bot was comparing:
- ✅ **Lighter**: Forward-looking funding rate (next/upcoming) - **CORRECT, NOT CHANGED**
- ❌ **Pacifica**: Backward-looking funding rate (historical/last applied) - **FIXED IN THIS PR**

## Problem

### What Was Wrong
The Pacifica client (`pacifica_client.py`) was using the **`funding_rate`** field from the `/api/v1/info` endpoint, which represents the **historical/last applied** funding rate.

Pacifica's API returns TWO funding rate fields:
```json
{
  "symbol": "ETH",
  "funding_rate": "0.00000716",        // ← Was using this (HISTORICAL)
  "next_funding_rate": "0.0000125"     // ← Should use this (FORWARD-LOOKING)
}
```

### Impact
This caused the bot to make arbitrage decisions by comparing:
- Lighter's **next** funding payment (what you'll receive/pay)
- Pacifica's **last** funding payment (what already happened)

Real-world example showing the magnitude of error:
- **ETH**: Historical `0.00000716` vs Next `0.0000125` (74% difference!)
- **SOL**: Historical `-0.00000029` vs Next `0.00000267` (sign flip!)

## Solution

### Changes Made
**File**: `pacifica_client.py:80-91`

Changed the Pacifica client to:
1. Use `next_funding_rate` (forward-looking TWAP estimate) as primary source
2. Fall back to `funding_rate` (historical) if `next_funding_rate` is not available
3. Added clear comments explaining why we use the forward-looking rate

```python
# Use next_funding_rate (forward-looking TWAP estimate) instead of funding_rate (historical)
# This ensures we're comparing forward-looking rates from both exchanges
next_rate = market.get("next_funding_rate")
historical_rate = market.get("funding_rate", 0.0)
funding_rate = float(next_rate if next_rate is not None else historical_rate)
```

### What Was NOT Changed
- ✅ **Lighter client** (`lighter_client.py`) - Already correct, uses forward-looking rates
- ✅ All other bot logic remains unchanged
- ✅ API structure and field names remain the same (internal mapping only)

## Technical Details

### Pacifica Funding Rate Mechanism
According to Pacifica's documentation:
- Funding payments occur **every hour** (not 8-hour like most exchanges)
- System samples funding rates **every 5 seconds**
- Computes **TWAP (time-weighted average price)** estimate of next 1h funding
- `next_funding_rate` = This TWAP estimate (forward-looking)
- `funding_rate` = Last applied rate (historical, already settled)

### Lighter Funding Rate Mechanism (NOT CHANGED)
According to Lighter's documentation:
- Funding payments occur **every hour**
- Rates represent **8-hour cumulative** values (bot divides by 8 to get hourly)
- `/api/v1/funding-rates` endpoint returns **current/next** funding rates (forward-looking)
- Lighter was already correct - no changes made

### Verification
Tested against live Pacifica API showing real differences:
```
Symbol     Historical      Next (Used)     Difference
------------------------------------------------------------
ETH        0.0000071600    0.0000125000    0.0000053400
SOL        -0.0000002900   0.0000026700    0.0000029600
BTC        0.0000125000    0.0000125000    0.0000000000
```

## Testing
- [x] Python syntax validation (`py_compile`)
- [x] Live API comparison showing correct field usage
- [x] Verified both exchanges now use forward-looking rates

## Files Changed
- `pacifica_client.py` - Updated funding rate field selection (7 lines added)
- `.gitignore` - Added to prevent `__pycache__/` commits

## Risk Assessment
**Low Risk**:
- Isolated change to single field selection
- Only affects Pacifica funding rate retrieval
- Fallback logic ensures compatibility
- No API contract changes
- **Lighter client completely untouched - it was already correct**
