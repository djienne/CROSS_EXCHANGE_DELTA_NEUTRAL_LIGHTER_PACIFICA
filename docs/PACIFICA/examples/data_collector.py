import websocket
import json
import threading
import time
import os
import csv
import argparse
import requests
from datetime import datetime
from collections import deque
from decimal import Decimal, ROUND_HALF_UP

LIST_MARKETS = ['BTC', 'ETH', 'UNI', 'PENGU']

class WebSocketDataCollector:
    def __init__(self, symbols, flush_interval=5, order_book_levels=10):
        self.symbols = [symbol.upper() for symbol in symbols]
        self.flush_interval = flush_interval
        self.order_book_levels = order_book_levels
        self.base_url = "wss://ws.pacifica.fi/ws"
        self.api_base_url = "https://api.pacifica.fi/api/v1"
        self.api_info_base_url = "https://api.pacifica.fi/api/v1/info"

        # WebSocket connections
        self.combined_ws = None

        # Connection management
        self.is_connected = False
        self.should_reconnect = True
        self.reconnect_interval = 5
        self.ping_timeout = 15
        self.ping_interval = 30

        # Data buffers
        self.prices_buffer = {}  # symbol -> deque of price records (bid/ask/mid)
        self.orderbook_buffer = {}  # symbol -> deque of full order book records
        self.trades_buffer = {}  # symbol -> deque of trade records
        self.seen_trade_ids = {}  # symbol -> set of seen trade IDs

        # Thread management
        self.flush_thread = None
        self.lock = threading.Lock()

        # Initialize buffers and load existing trade IDs
        for symbol in self.symbols:
            self.prices_buffer[symbol] = deque()
            self.orderbook_buffer[symbol] = deque()
            self.trades_buffer[symbol] = deque()
            self.seen_trade_ids[symbol] = self.load_seen_trade_ids(symbol)

        print(f"Initialized WebSocket Data Collector for: {', '.join(self.symbols)}")

    def create_data_directory(self):
        """Creates the PACIFICA_data directory if it doesn't exist."""
        if not os.path.exists('PACIFICA_data'):
            os.makedirs('PACIFICA_data')

    def load_seen_trade_ids(self, symbol):
        """Loads the last 1000 existing trade IDs from the CSV file to prevent duplicates."""
        seen_ids = set()
        file_path = os.path.join('PACIFICA_data', f'trades_{symbol}.csv')
        if not os.path.isfile(file_path):
            return seen_ids

        try:
            with open(file_path, 'r', newline='') as csvfile:
                lines = csvfile.readlines()
                last_1000_lines = lines[-1000:]

                if lines and last_1000_lines[0] == lines[0] and lines[0].startswith('id,'):
                    start_index = 1
                else:
                    start_index = 0

                reader = csv.reader(last_1000_lines[start_index:])
                for row in reader:
                    if row:
                        try:
                            seen_ids.add(str(row[0]))  # Keep as string for Pacifica
                        except (ValueError, IndexError):
                            pass
        except Exception as e:
            print(f"Warning: Error loading trade IDs for {symbol}: {e}")

        return seen_ids

    def get_initial_prices_api(self, symbol):
        """Get initial price data via API to establish baseline."""
        # Skip API calls - Pacifica API requires authentication even for public endpoints
        # WebSocket will provide initial data instead
        print(f"  Skipping initial price API call (will use WebSocket data)")
        return None

    def get_initial_orderbook_api(self, symbol):
        """Get initial order book data via API to establish baseline."""
        # Skip API calls - Pacifica API requires authentication even for public endpoints
        # WebSocket will provide initial data instead
        print(f"  Skipping initial orderbook API call (will use WebSocket data)")
        return None

    def get_initial_trades_api(self, symbol):
        """Get initial trade data via API to establish baseline."""
        # Skip API calls - Pacifica API requires authentication even for public endpoints
        # WebSocket will provide initial data instead
        print(f"  Skipping initial trades API call (will use WebSocket data)")
        return []

    def on_combined_message(self, ws, message):
        """Handle combined stream messages from Pacifica WebSocket."""
        try:
            data = json.loads(message)
            channel = data.get('channel', '')

            # Handle subscription confirmation (if Pacifica sends one)
            if 'subscribed' in message.lower():
                print(f"Successfully subscribed: {message[:100]}")
                return

            # Handle price updates (channel: "prices")
            if channel == 'prices':
                self.process_price_update(data)

            # Handle orderbook updates (channel: "book")
            elif channel == 'book':
                self.process_orderbook_update(data)

            # Handle trade updates (channel: "trades")
            elif channel == 'trades':
                self.process_trade_update(data)

        except json.JSONDecodeError:
            pass
        except Exception as e:
            print(f"Error processing combined message: {e}")

    def process_price_update(self, data):
        """Process price update messages.
        Format: {"channel": "prices", "data": [{symbol info}, ...]}
        """
        try:
            prices_list = data.get('data', [])

            for price_data in prices_list:
                symbol = price_data.get('symbol', '')
                if symbol in self.symbols:
                    timestamp = price_data.get('timestamp', time.time() * 1000) / 1000

                    # Pacifica doesn't provide bid/ask in WebSocket, calculate from mid +/- spread
                    # Use Decimal for precise calculations
                    mid = Decimal(str(price_data.get('mid', 0)))
                    mark = Decimal(str(price_data.get('mark', mid)))

                    # Estimate bid/ask from mid (small spread for storage)
                    spread = abs(mid - mark) if mark else mid * Decimal('0.0001')
                    bid = mid - spread
                    ask = mid + spread

                    if mid > 0:
                        price_record = {
                            'timestamp': timestamp,
                            'bid': float(bid),
                            'ask': float(ask),
                            'mid': float(mid)
                        }

                        with self.lock:
                            self.prices_buffer[symbol].append(price_record)

        except Exception as e:
            print(f"Error processing price update: {e}")

    def process_orderbook_update(self, data):
        """Process orderbook update messages.
        Format: {"channel": "book", "data": {"l": [[bids], [asks]], "s": "SOL", "t": timestamp}}
        """
        try:
            book_data = data.get('data', {})
            symbol = book_data.get('s', '')

            if symbol in self.symbols:
                timestamp = book_data.get('t', time.time() * 1000) / 1000
                levels = book_data.get('l', [[], []])

                if len(levels) >= 2:
                    bid_list = levels[0][:self.order_book_levels]
                    ask_list = levels[1][:self.order_book_levels]

                    # Convert format: {p: price, a: amount, n: num_orders} to [price, amount]
                    processed_bids = [[float(b['p']), float(b['a'])] for b in bid_list if 'p' in b and 'a' in b]
                    processed_asks = [[float(a['p']), float(a['a'])] for a in ask_list if 'p' in a and 'a' in a]

                    if processed_bids and processed_asks:
                        orderbook_record = {
                            'timestamp': timestamp,
                            'bids': processed_bids,
                            'asks': processed_asks,
                            'lastUpdateId': str(int(timestamp * 1000))
                        }

                        # Also update price data from orderbook using Decimal for precision
                        best_bid = Decimal(str(processed_bids[0][0]))
                        best_ask = Decimal(str(processed_asks[0][0]))
                        mid = (best_bid + best_ask) / Decimal('2')

                        price_record = {
                            'timestamp': timestamp,
                            'bid': float(best_bid),
                            'ask': float(best_ask),
                            'mid': float(mid)
                        }

                        with self.lock:
                            self.orderbook_buffer[symbol].append(orderbook_record)
                            self.prices_buffer[symbol].append(price_record)

        except Exception as e:
            print(f"Error processing orderbook update: {e}")

    def process_trade_update(self, data):
        """Process trade update messages.
        Format: {"channel": "trades", "data": [{"a": amount, "p": price, "s": symbol, "t": timestamp, "d": side, ...}]}
        """
        try:
            trades = data.get('data', [])
            if not isinstance(trades, list):
                trades = [trades]

            for trade_data in trades:
                symbol = trade_data.get('s', '')

                if symbol in self.symbols:
                    # Create unique ID from timestamp + price + amount
                    timestamp = trade_data.get('t', int(time.time() * 1000))
                    price = float(trade_data.get('p', 0))
                    amount = float(trade_data.get('a', 0))

                    trade_id = f"{timestamp}_{price}_{amount}"

                    if trade_id and trade_id not in self.seen_trade_ids[symbol]:
                        raw_side = trade_data.get('d', 'buy')  # d: direction (open_long/open_short/close_long/close_short)

                        # Convert Pacifica direction to buy/sell for Avellaneda calculations
                        # open_long and close_short are buys, open_short and close_long are sells
                        if raw_side in ('open_long', 'close_short'):
                            side = 'buy'
                        elif raw_side in ('open_short', 'close_long'):
                            side = 'sell'
                        else:
                            side = raw_side  # fallback to original value

                        trade_record = {
                            'id': trade_id,
                            'timestamp': timestamp,
                            'side': side,
                            'price': price,
                            'quantity': amount
                        }

                        with self.lock:
                            self.trades_buffer[symbol].append(trade_record)
                            self.seen_trade_ids[symbol].add(trade_id)

        except Exception as e:
            print(f"Error processing trade update: {e}")

    def on_error(self, ws, error):
        """Handle WebSocket errors."""
        print(f"WebSocket error: {error}")
        self.is_connected = False

    def on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket close."""
        print(f"WebSocket connection closed. Status: {close_status_code}")
        self.is_connected = False

    def on_combined_open(self, ws):
        """Handle combined WebSocket open."""
        print("Combined WebSocket connected, subscribing to streams...")
        self.is_connected = True

        try:
            # Subscribe to prices stream
            prices_sub = {
                "method": "subscribe",
                "params": {
                    "source": "prices"
                }
            }
            ws.send(json.dumps(prices_sub))
            print("Sent subscription request for prices")

            # Subscribe to orderbook stream for each symbol
            for symbol in self.symbols:
                orderbook_sub = {
                    "method": "subscribe",
                    "params": {
                        "source": "book",
                        "symbol": symbol,
                        "agg_level": 1
                    }
                }
                ws.send(json.dumps(orderbook_sub))
                print(f"Sent subscription request for {symbol} orderbook")

            # Subscribe to trades stream for each symbol
            for symbol in self.symbols:
                trades_sub = {
                    "method": "subscribe",
                    "params": {
                        "source": "trades",
                        "symbol": symbol
                    }
                }
                ws.send(json.dumps(trades_sub))
                print(f"Sent subscription request for {symbol} trades")

        except Exception as e:
            print(f"Error sending subscription requests: {e}")

    def start_websockets(self):
        """Start WebSocket connections."""
        websocket.enableTrace(False)

        # Connect to Pacifica WebSocket
        ws_url = self.base_url
        print(f"Connecting to: {ws_url}")

        self.combined_ws = websocket.WebSocketApp(
            ws_url,
            on_open=self.on_combined_open,
            on_message=self.on_combined_message,
            on_error=self.on_error,
            on_close=self.on_close
        )

        # Start WebSocket in a separate thread
        def run_websocket():
            while self.should_reconnect:
                try:
                    self.combined_ws.run_forever(
                        ping_interval=self.ping_interval,
                        ping_timeout=10
                    )

                    if self.should_reconnect:
                        print(f"WebSocket disconnected. Reconnecting in {self.reconnect_interval}s...")
                        time.sleep(self.reconnect_interval)

                except Exception as e:
                    print(f"WebSocket error: {e}")
                    if self.should_reconnect:
                        time.sleep(self.reconnect_interval)

        ws_thread = threading.Thread(target=run_websocket, daemon=True)
        ws_thread.start()

    def flush_buffers(self):
        """Flush all buffers to CSV files."""
        with self.lock:
            # Flush price data
            for symbol in self.symbols:
                if self.prices_buffer[symbol]:
                    self.flush_prices_buffer(symbol)

                if self.orderbook_buffer[symbol]:
                    self.flush_orderbook_buffer(symbol)

                if self.trades_buffer[symbol]:
                    self.flush_trades_buffer(symbol)

    def flush_prices_buffer(self, symbol):
        """Flush price buffer for a specific symbol."""
        file_path = os.path.join('PACIFICA_data', f'prices_{symbol}.csv')
        file_exists = os.path.isfile(file_path)

        try:
            with open(file_path, 'a', newline='') as csvfile:
                writer = csv.writer(csvfile)
                if not file_exists:
                    writer.writerow(['unix_timestamp', 'bid', 'ask', 'mid'])

                count = 0
                while self.prices_buffer[symbol]:
                    record = self.prices_buffer[symbol].popleft()
                    writer.writerow([
                        record['timestamp'],
                        record['bid'],
                        record['ask'],
                        f"{record['mid']:.6f}"
                    ])
                    count += 1

                if count > 0:
                    print(f"Flushed {count} price records for {symbol}")

        except Exception as e:
            print(f"Error flushing prices for {symbol}: {e}")

    def flush_orderbook_buffer(self, symbol):
        """Flush order book buffer for a specific symbol."""
        file_path = os.path.join('PACIFICA_data', f'orderbook_{symbol}.csv')
        file_exists = os.path.isfile(file_path)

        try:
            with open(file_path, 'a', newline='') as csvfile:
                writer = csv.writer(csvfile)
                if not file_exists:
                    # Create header with bid/ask levels
                    header = ['unix_timestamp', 'lastUpdateId']
                    for i in range(self.order_book_levels):
                        header.extend([f'bid_price_{i}', f'bid_qty_{i}'])
                    for i in range(self.order_book_levels):
                        header.extend([f'ask_price_{i}', f'ask_qty_{i}'])
                    writer.writerow(header)

                count = 0
                while self.orderbook_buffer[symbol]:
                    record = self.orderbook_buffer[symbol].popleft()

                    # Prepare row data
                    row = [record['timestamp'], record.get('lastUpdateId', '')]

                    # Add bid data
                    bids = record.get('bids', [])
                    for i in range(self.order_book_levels):
                        if i < len(bids):
                            row.extend([f"{bids[i][0]:.6f}", f"{bids[i][1]:.6f}"])
                        else:
                            row.extend(['', ''])  # Empty if no data at this level

                    # Add ask data
                    asks = record.get('asks', [])
                    for i in range(self.order_book_levels):
                        if i < len(asks):
                            row.extend([f"{asks[i][0]:.6f}", f"{asks[i][1]:.6f}"])
                        else:
                            row.extend(['', ''])  # Empty if no data at this level

                    writer.writerow(row)
                    count += 1

                if count > 0:
                    print(f"Flushed {count} order book records for {symbol}")

        except Exception as e:
            print(f"Error flushing order book for {symbol}: {e}")

    def flush_trades_buffer(self, symbol):
        """Flush trades buffer for a specific symbol."""
        file_path = os.path.join('PACIFICA_data', f'trades_{symbol}.csv')
        file_exists = os.path.isfile(file_path)

        try:
            with open(file_path, 'a', newline='') as csvfile:
                writer = csv.writer(csvfile)
                if not file_exists:
                    writer.writerow(['id', 'unix_timestamp_ms', 'side', 'price', 'quantity'])

                count = 0
                while self.trades_buffer[symbol]:
                    record = self.trades_buffer[symbol].popleft()
                    writer.writerow([
                        record['id'],
                        record['timestamp'],
                        record['side'],
                        f"{record['price']:.6f}",
                        f"{record['quantity']:.6f}"
                    ])
                    count += 1

                if count > 0:
                    print(f"Flushed {count} trade records for {symbol}")

        except Exception as e:
            print(f"Error flushing trades for {symbol}: {e}")

    def start_flush_thread(self):
        """Start the buffer flush thread."""
        def flush_worker():
            while self.should_reconnect:
                time.sleep(self.flush_interval)
                self.flush_buffers()

        self.flush_thread = threading.Thread(target=flush_worker, daemon=True)
        self.flush_thread.start()
        print(f"Started buffer flush thread (interval: {self.flush_interval}s)")

    def collect_initial_data(self):
        """Collect initial data via API calls."""
        print("Collecting initial data via API...")

        for symbol in self.symbols:
            print(f"Getting initial data for {symbol}...")

            # Get initial price data
            initial_price = self.get_initial_prices_api(symbol)
            if initial_price:
                with self.lock:
                    self.prices_buffer[symbol].append(initial_price)
                print(f"  Initial price: ${initial_price['mid']:.2f}")

            # Get initial order book data
            initial_orderbook = self.get_initial_orderbook_api(symbol)
            if initial_orderbook:
                with self.lock:
                    self.orderbook_buffer[symbol].append(initial_orderbook)
                print(f"  Initial order book: {len(initial_orderbook['bids'])} bids, {len(initial_orderbook['asks'])} asks")

            # Get initial trade data
            initial_trades = self.get_initial_trades_api(symbol)
            if initial_trades:
                with self.lock:
                    self.trades_buffer[symbol].extend(initial_trades)
                print(f"  Loaded {len(initial_trades)} initial trades")

            print(f"  Existing trade IDs loaded: {len(self.seen_trade_ids[symbol])}")

    def start(self):
        """Start the data collector."""
        self.create_data_directory()

        # Collect initial data via API
        self.collect_initial_data()

        # Start buffer flush thread
        self.start_flush_thread()

        # Start WebSocket connections
        self.start_websockets()

        # Wait for connection
        max_wait = 10
        waited = 0
        while not self.is_connected and waited < max_wait:
            time.sleep(1)
            waited += 1

        if self.is_connected:
            print("WebSocket data collector started successfully!")
        else:
            print("Warning: WebSocket connection not established within timeout")

    def stop(self):
        """Stop the data collector."""
        print("Stopping data collector...")
        self.should_reconnect = False

        if hasattr(self, 'combined_ws') and self.combined_ws:
            self.combined_ws.close()

        # Final flush
        self.flush_buffers()
        print("Data collector stopped.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='WebSocket-based data collector for Pacifica Finance market data.')
    parser.add_argument('symbols', type=str, nargs='*', default=LIST_MARKETS,
                        help='The trading symbols to collect data for (e.g., BTC ETH SOL).')
    parser.add_argument('--flush-interval', type=int, default=5,
                        help='The interval in seconds to flush buffers to CSV files. Defaults to 5.')
    parser.add_argument('--order-book-levels', type=int, default=10,
                        help='Number of order book levels to collect. Defaults to 10.')
    args = parser.parse_args()

    collector = WebSocketDataCollector(args.symbols, args.flush_interval, args.order_book_levels)

    try:
        collector.start()

        # Keep running
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nShutting down...")
        collector.stop()