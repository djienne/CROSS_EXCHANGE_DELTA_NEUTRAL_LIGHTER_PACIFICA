"""
Fetch klines (candlestick data) directly from Pacifica API.

This script fetches historical kline data via the Pacifica REST API with multiple
async API calls to fetch up to 10,000 klines (default). Includes data integrity checks.

Example usage:
    python get_klines.py --symbol UNI --interval 5m
    python get_klines.py --symbol BTC --interval 1h --limit 5000
    python get_klines.py --symbol ETH --interval 15m --start 2025-10-04T00:00:00
"""

import aiohttp
import asyncio
import pandas as pd
import argparse
from datetime import datetime
import time


API_BASE_URL = "https://api.pacifica.fi/api/v1"
MAX_KLINES_PER_REQUEST = 3000  # Conservative limit per request
OVERLAP_KLINES = 10  # Overlap between batches to ensure no gaps


async def fetch_klines_batch(session, symbol, interval, start_time, end_time, batch_num):
    """
    Fetch a single batch of klines from Pacifica API (async).

    Args:
        session: aiohttp ClientSession
        symbol: Trading symbol
        interval: Kline interval
        start_time: Start time in milliseconds
        end_time: End time in milliseconds
        batch_num: Batch number for logging

    Returns:
        Tuple of (batch_num, list of kline dictionaries)
    """
    url = f"{API_BASE_URL}/kline"
    params = {
        'symbol': symbol,
        'interval': interval,
        'start_time': start_time,
        'end_time': end_time
    }

    print(f"[Batch {batch_num}] Fetching from {datetime.fromtimestamp(start_time/1000).strftime('%Y-%m-%d %H:%M')} to {datetime.fromtimestamp(end_time/1000).strftime('%Y-%m-%d %H:%M')}...")

    try:
        async with session.get(url, params=params, timeout=30) as response:
            response.raise_for_status()
            data = await response.json()

            if not data.get('success'):
                error_msg = data.get('error', 'Unknown error')
                raise Exception(f"API returned error: {error_msg}")

            klines = data.get('data', [])
            print(f"  [Batch {batch_num}] Received {len(klines):,} klines")
            return (batch_num, klines)

    except Exception as e:
        print(f"  [Batch {batch_num}] ERROR: {e}")
        return (batch_num, [])


async def get_klines(symbol, interval, start_time=None, end_time=None, limit=10000):
    """
    Fetch klines from Pacifica API with multiple async requests if needed.

    Args:
        symbol: Trading symbol (e.g., 'UNI', 'BTC', 'ETH')
        interval: Kline interval ('1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '8h', '12h', '1d')
        start_time: Start time in milliseconds (or datetime string)
        end_time: End time in milliseconds (or datetime string, defaults to now)
        limit: Maximum number of klines to fetch (default 10000)

    Returns:
        DataFrame with kline data
    """

    # Convert interval to milliseconds
    interval_ms = {
        '1m': 60 * 1000,
        '3m': 3 * 60 * 1000,
        '5m': 5 * 60 * 1000,
        '15m': 15 * 60 * 1000,
        '30m': 30 * 60 * 1000,
        '1h': 60 * 60 * 1000,
        '2h': 2 * 60 * 60 * 1000,
        '4h': 4 * 60 * 60 * 1000,
        '8h': 8 * 60 * 60 * 1000,
        '12h': 12 * 60 * 60 * 1000,
        '1d': 24 * 60 * 60 * 1000,
    }

    if interval not in interval_ms:
        raise ValueError(f"Invalid interval: {interval}. Valid: {list(interval_ms.keys())}")

    interval_duration_ms = interval_ms[interval]

    # Calculate start_time and end_time
    if end_time is None:
        end_time = int(time.time() * 1000)
    elif isinstance(end_time, str):
        end_time = int(datetime.fromisoformat(end_time).timestamp() * 1000)

    if start_time is None:
        # Calculate start_time based on limit and interval
        start_time = end_time - (limit * interval_duration_ms)
    elif isinstance(start_time, str):
        start_time = int(datetime.fromisoformat(start_time).timestamp() * 1000)

    print(f"Fetching klines from Pacifica API...")
    print(f"  Symbol: {symbol}")
    print(f"  Interval: {interval}")
    print(f"  Start: {datetime.fromtimestamp(start_time/1000).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  End: {datetime.fromtimestamp(end_time/1000).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Requested: {limit:,} klines")
    print()

    # Calculate number of batches needed
    total_requested_ms = end_time - start_time
    total_klines_requested = int(total_requested_ms / interval_duration_ms)
    num_batches = (total_klines_requested + MAX_KLINES_PER_REQUEST - 1) // MAX_KLINES_PER_REQUEST

    if num_batches > 1:
        print(f"[INFO] Fetching {num_batches} batches in parallel (API limit: {MAX_KLINES_PER_REQUEST} klines/request)")
        print()

    # Prepare batch time ranges with overlap
    batch_ranges = []
    current_start = start_time

    for batch_num in range(num_batches):
        batch_end = min(current_start + (MAX_KLINES_PER_REQUEST * interval_duration_ms), end_time)
        batch_ranges.append((batch_num + 1, current_start, batch_end))

        # Update start for next batch with overlap (go back OVERLAP_KLINES intervals)
        overlap_ms = OVERLAP_KLINES * interval_duration_ms
        current_start = batch_end - overlap_ms

    # Fetch all batches in parallel using asyncio.gather
    async with aiohttp.ClientSession() as session:
        tasks = [
            fetch_klines_batch(session, symbol, interval, start, end, batch_num)
            for batch_num, start, end in batch_ranges
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

    print()

    # Process results
    all_klines = []
    for result in results:
        if isinstance(result, Exception):
            print(f"[ERROR] Batch failed with exception: {result}")
            continue

        batch_num, klines = result
        if klines:
            all_klines.extend(klines)

    if all_klines:
        print(f"[OK] Total fetched: {len(all_klines):,} klines from {num_batches} batches")
        print()

    if not all_klines:
        print("[ERROR] No klines fetched")
        return pd.DataFrame()

    # Convert to DataFrame
    df = pd.DataFrame(all_klines)

    # Rename columns to standard names
    df = df.rename(columns={
        't': 'timestamp',
        's': 'symbol',
        'i': 'interval',
        'o': 'open',
        'h': 'high',
        'l': 'low',
        'c': 'close',
        'v': 'volume',
        'n': 'trades_count',
        'T': 'close_time'
    })

    # Convert numeric columns
    df['open'] = pd.to_numeric(df['open'])
    df['high'] = pd.to_numeric(df['high'])
    df['low'] = pd.to_numeric(df['low'])
    df['close'] = pd.to_numeric(df['close'])
    df['volume'] = pd.to_numeric(df['volume'])
    df['trades_count'] = pd.to_numeric(df['trades_count'])

    # Add datetime column
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')

    # Remove duplicates from overlapping batches
    initial_count = len(df)
    df = df.drop_duplicates(subset=['timestamp'], keep='first')
    duplicates_removed = initial_count - len(df)

    if duplicates_removed > 0:
        print(f"[INFO] Removed {duplicates_removed} duplicate klines from overlapping batches")

    # Sort by timestamp
    df = df.sort_values('timestamp').reset_index(drop=True)

    # Limit to requested number of klines
    if len(df) > limit:
        df = df.tail(limit).reset_index(drop=True)

    # Data integrity check
    print("=" * 80)
    print("DATA INTEGRITY CHECK")
    print("=" * 80)

    # Check for gaps in timestamps
    expected_diff = interval_duration_ms
    df['time_diff'] = df['timestamp'].diff()
    gaps = df[df['time_diff'] > expected_diff * 1.5]  # Allow 50% tolerance

    if len(gaps) > 0:
        print(f"[WARNING] Found {len(gaps)} gaps in kline data:")
        for idx, row in gaps.head(10).iterrows():
            prev_time = df.loc[idx-1, 'datetime']
            curr_time = row['datetime']
            missing_klines = int((row['time_diff'] / expected_diff) - 1)
            print(f"  Gap at {curr_time} (after {prev_time}): ~{missing_klines} missing klines")
        if len(gaps) > 10:
            print(f"  ... and {len(gaps) - 10} more gaps")
    else:
        print(f"[OK] No gaps detected - continuous data")

    # Expected number of klines
    actual_count = len(df)
    time_range_ms = df['timestamp'].iloc[-1] - df['timestamp'].iloc[0]
    expected_count = int(time_range_ms / interval_duration_ms) + 1

    print(f"[OK] Fetched {actual_count:,} klines")
    print(f"[INFO] Expected ~{expected_count:,} klines for time range")

    if actual_count < expected_count * 0.95:  # More than 5% missing
        print(f"[WARNING] Data appears incomplete ({actual_count}/{expected_count} = {100*actual_count/expected_count:.1f}%)")
    else:
        print(f"[OK] Data completeness: {100*actual_count/expected_count:.1f}%")

    print()

    # Clean up temporary column
    df = df.drop(columns=['time_diff'])

    return df[['timestamp', 'datetime', 'open', 'high', 'low', 'close', 'volume', 'trades_count']]


async def main_async():
    parser = argparse.ArgumentParser(description='Fetch klines from Pacifica API')
    parser.add_argument('--symbol', type=str, default='UNI', help='Trading symbol (e.g., UNI, BTC, ETH)')
    parser.add_argument('--interval', type=str, default='5m',
                       help='Kline interval: 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 8h, 12h, 1d')
    parser.add_argument('--limit', type=int, default=10000,
                       help='Number of most recent klines to fetch (default: 10000)')
    parser.add_argument('--start', type=str, default=None,
                       help='Start time (ISO format: 2025-10-04T00:00:00 or milliseconds)')
    parser.add_argument('--end', type=str, default=None,
                       help='End time (ISO format: 2025-10-04T12:00:00 or milliseconds)')

    args = parser.parse_args()

    print("=" * 80)
    print(f"PACIFICA KLINE FETCHER - {args.symbol} {args.interval}")
    print("=" * 80)
    print()

    # Parse start/end times if provided as milliseconds
    start_time = None
    end_time = None

    if args.start:
        try:
            start_time = int(args.start)
        except ValueError:
            start_time = args.start

    if args.end:
        try:
            end_time = int(args.end)
        except ValueError:
            end_time = args.end

    # Fetch klines
    try:
        klines = await get_klines(
            symbol=args.symbol,
            interval=args.interval,
            start_time=start_time,
            end_time=end_time,
            limit=args.limit
        )
    except Exception as e:
        print(f"[ERROR] Failed to fetch klines: {e}")
        return

    if len(klines) == 0:
        print("[ERROR] No klines fetched")
        return

    # Display summary statistics
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Symbol:       {args.symbol}")
    print(f"Interval:     {args.interval}")
    print(f"Total klines: {len(klines):,}")
    print(f"Time range:   {klines['datetime'].iloc[0]} to {klines['datetime'].iloc[-1]}")
    print(f"Price range:  ${klines['low'].min():.4f} - ${klines['high'].max():.4f}")
    print(f"Total volume: {klines['volume'].sum():.4f}")
    print(f"Total trades: {int(klines['trades_count'].sum()):,}")
    print()

    # Display first 5 klines
    print("=" * 80)
    print("FIRST 5 KLINES")
    print("=" * 80)
    print(klines.head().to_string(index=False))
    print()

    # Display last 5 klines
    print("=" * 80)
    print("LAST 5 KLINES")
    print("=" * 80)
    print(klines.tail().to_string(index=False))
    print()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
