"""
BTC-ETH Hedged Long/Short Bot for Pacifica DEX

Minimal configuration, data-driven hedge ratio, periodic refresh,
per-leg stop-loss protection.

Usage:
    python main.py
"""
import os
import sys
import json
import time
import signal
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple, Optional
from dotenv import load_dotenv
from colorama import init, Fore, Style

# Initialize colorama
init(autoreset=True)

# Add parent to path for SDK imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from pacifica_client import PacificaClient
from ewma_hedge_ratio import calculate_hedge_ratio_auto


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(Path(__file__).parent / "hedge_bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class HedgeBot:
    """BTC-ETH hedged long/short bot."""

    # Symbols (fixed)
    BTC_SYMBOL = "BTC"
    ETH_SYMBOL = "ETH"

    def __init__(self, config_path: Path):
        """Initialize bot with config file."""
        # Load config
        with open(config_path, 'r') as f:
            self.config = json.load(f)

        logger.info(f"{Fore.GREEN}Loaded config: {self.config}")

        # Load environment
        load_dotenv(Path(__file__).parent / ".env")

        sol_wallet = os.getenv("SOL_WALLET")
        api_public = os.getenv("API_PUBLIC")
        api_private = os.getenv("API_PRIVATE")

        if not all([sol_wallet, api_public, api_private]):
            raise ValueError("Missing env vars: SOL_WALLET, API_PUBLIC, API_PRIVATE")

        # Initialize client
        self.client = PacificaClient(
            sol_wallet,
            api_public,
            api_private,
            slippage_bps=self.get_config("slippage_bps", 50),
            allow_fallback=False
        )
        logger.info(f"{Fore.GREEN}Pacifica client initialized")

        # Cached hedge ratio
        self._cached_h: Optional[float] = None
        self._h_last_update: float = 0
        self._hedge_ratio_calculator = calculate_hedge_ratio_auto
        logger.info(f"{Fore.GREEN}EWMA hedge ratio calculator initialized")

        # State
        self.running = True
        self.next_refresh: Optional[datetime] = None
        self.last_deploy_at: Optional[datetime] = None
        self.stoploss_triggered = False

        # State management
        self.state_file = Path(__file__).parent / "state" / "state.json"
        self.saved_hedge_ratio: Optional[float] = None
        self.saved_btc_qty: Optional[float] = None
        self.saved_eth_qty: Optional[float] = None
        self.reference_account_value: Optional[float] = None
        self._load_state()

        # Restore hedge ratio from state if available
        if self.saved_hedge_ratio is not None:
            self._cached_h = self.saved_hedge_ratio
            self._h_last_update = time.time()
            logger.info(f"{Fore.GREEN}Hedge ratio restored from state: {self._cached_h:.4f}")

        # Register signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _load_state(self):
        """Load bot state from state.json."""
        if not self.state_file.exists():
            logger.info(f"{Fore.YELLOW}No state file found.")
            return

        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)

            self.last_deploy_at = datetime.fromisoformat(state.get("last_deploy_at"))
            self.next_refresh = datetime.fromisoformat(state.get("next_refresh"))
            self.saved_hedge_ratio = state.get("hedge_ratio")
            self.saved_btc_qty = state.get("btc_qty_target")
            self.saved_eth_qty = state.get("eth_qty_target")
            self.stoploss_triggered = state.get("stoploss_triggered", False)

            # Get reference equity for long-term PnL tracking
            self.reference_account_value = state.get("reference_account_value")
            if self.reference_account_value is None:
                logger.info("Reference account value not found in state, setting it to current equity.")
                self.reference_account_value = self.client.get_equity()
                # Re-save state immediately with the new reference value
                self._save_state(
                    self.saved_btc_qty, 
                    self.saved_eth_qty, 
                    self.saved_hedge_ratio
                )

            logger.info(f"{Fore.GREEN}State loaded: {state}")

        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            # In case of corrupted state, clear it
            self._clear_state()

    def _save_state(self, btc_qty: float, eth_qty: float, hedge_ratio: float):
        """Save bot state to state.json."""
        state = {
            "last_deploy_at": self.last_deploy_at.isoformat() if self.last_deploy_at else None,
            "next_refresh": self.next_refresh.isoformat() if self.next_refresh else None,
            "hedge_ratio": hedge_ratio,
            "btc_qty_target": btc_qty,
            "eth_qty_target": eth_qty,
            "stoploss_triggered": self.stoploss_triggered,
            "reference_account_value": self.reference_account_value,
        }

        try:
            # Ensure state directory exists
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=4)
            logger.info(f"State saved: {state}")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _clear_state(self):
        """Clear bot state by deleting state.json."""
        try:
            if self.state_file.exists():
                self.state_file.unlink()
                logger.info("State file deleted.")
        except Exception as e:
            logger.error(f"Failed to clear state: {e}")


    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get config value with default."""
        return self.config.get(key, default)

    def _get_hedge_ratio(self, force_refresh: bool = False) -> float:
        """
        Get hedge ratio with caching.

        Recalculates if:
        - Never calculated before
        - force_refresh is True
        - Cache is older than 1 hour

        Returns:
            Hedge ratio h
        """
        import time

        # Check if we need to recalculate
        cache_age = time.time() - self._h_last_update
        need_calc = (
            self._cached_h is None or
            force_refresh or
            cache_age > 3600  # 1 hour
        )

        if need_calc:
            method = self.get_config("hedge_ratio_method", "ewma")
            logger.info(f"{Fore.CYAN}Calculating hedge ratio using '{method}' method...")

            try:
                # Try EWMA first (most reactive)
                h, stats = calculate_hedge_ratio_auto(
                    window_hours=24,
                    method=method,
                    fallback_h=0.85,
                    verbose=False
                )

                logger.info(f"{Fore.CYAN}Hedge ratio ({method}): h = {Style.BRIGHT}{h:.4f}")
                if "raw_h" in stats:
                    logger.info(f"{Fore.WHITE}  Raw h (unclamped): {stats['raw_h']:.4f}")
                if "status" in stats:
                    logger.info(f"{Fore.WHITE}  Status: {stats['status']}")
                if "samples" in stats:
                    logger.info(f"{Fore.WHITE}  Samples: {stats['samples']}")

                # Calculate split
                btc_pct = 100 / (1 + h)
                eth_pct = 100 * h / (1 + h)
                logger.info(f"{Fore.YELLOW}  Portfolio split: {btc_pct:.1f}% BTC / {eth_pct:.1f}% ETH")

                self._cached_h = h
                self._h_last_update = time.time()

            except Exception as e:
                logger.error(f"Hedge ratio calculation failed: {e}", exc_info=True)

                # Use cached or fallback
                if self._cached_h is not None:
                    logger.info(f"{Fore.YELLOW}Using cached hedge ratio: {self._cached_h:.4f}")
                else:
                    self._cached_h = 0.85
                    logger.info(f"{Fore.YELLOW}Using fallback hedge ratio: {self._cached_h:.4f}")

        return self._cached_h

    def compute_targets(self) -> Tuple[float, float]:
        """
        Compute target quantities for BTC and ETH.

        Returns:
            (btc_qty, eth_qty) - positive for long, negative for short
        """
        # Get equity
        equity = self.client.get_equity()
        logger.info(f"{Fore.GREEN}Account equity: ${equity:.2f}")

        # Calculate deployed capital
        capital_pct = self.get_config("capital_pct", 95)
        capital = equity * (capital_pct / 100.0)
        logger.info(f"{Fore.CYAN}Deploying {capital_pct}% of equity: ${capital:.2f}")

        # Apply leverage
        leverage = self.get_config("leverage", 4)
        gross_notional = capital * leverage
        logger.info(f"{Fore.CYAN}Gross notional (with {leverage}x leverage): ${gross_notional:.2f}")

        # Get hedge ratio (use cache if recent, otherwise recalculate)
        h = self._get_hedge_ratio()
        logger.info(f"Hedge ratio: {h:.4f}")

        # Get mark prices
        p_btc = self.client.get_mark_price(self.BTC_SYMBOL)
        p_eth = self.client.get_mark_price(self.ETH_SYMBOL)
        logger.info(f"{Fore.WHITE}Mark prices - BTC: ${p_btc:.2f}, ETH: ${p_eth:.2f}")

        # Calculate notionals
        # N_btc = gross / (1 + h)
        # N_eth = h * N_btc
        n_btc_usd = gross_notional / (1.0 + h)
        n_eth_usd = h * n_btc_usd

        logger.info(f"{Fore.YELLOW}Target notionals - BTC: ${n_btc_usd:.2f}, ETH: ${n_eth_usd:.2f}")

        # Convert to quantities
        q_btc = n_btc_usd / p_btc
        q_eth = n_eth_usd / p_eth

        # Round to lot sizes
        q_btc = self.client.round_quantity(q_btc, self.BTC_SYMBOL)
        q_eth = self.client.round_quantity(q_eth, self.ETH_SYMBOL)

        logger.info(f"{Fore.YELLOW}Target quantities - BTC: {q_btc:.4f}, ETH: {q_eth:.4f}")

        # Check margin health (simple check: ensure notionals don't exceed max)
        max_leverage_btc = self.client.get_max_leverage(self.BTC_SYMBOL)
        max_leverage_eth = self.client.get_max_leverage(self.ETH_SYMBOL)

        required_margin_btc = (q_btc * p_btc) / max_leverage_btc
        required_margin_eth = (q_eth * p_eth) / max_leverage_eth
        total_required_margin = required_margin_btc + required_margin_eth

        if total_required_margin > equity:
            # Scale down proportionally
            scale = equity / total_required_margin * 0.95  # 5% safety margin
            q_btc *= scale
            q_eth *= scale

            q_btc = self.client.round_quantity(q_btc, self.BTC_SYMBOL)
            q_eth = self.client.round_quantity(q_eth, self.ETH_SYMBOL)

            logger.warning(f"Scaled down positions for margin health: BTC={q_btc:.4f}, ETH={q_eth:.4f}")

        return q_btc, q_eth

    def _positions_match_targets(
        self,
        pos_btc: Dict[str, Any],
        pos_eth: Dict[str, Any],
        target_btc_qty: float,
        target_eth_qty: float,
        tolerance_pct: float = 0.05
    ) -> bool:
        """
        Determine whether current positions already match desired targets.

        Args:
            pos_btc: Current BTC position dict.
            pos_eth: Current ETH position dict.
            target_btc_qty: Target BTC quantity (positive for long).
            target_eth_qty: Target ETH quantity (positive notion for short leg).
            tolerance_pct: Relative tolerance allowed between current and target quantities.

        Returns:
            True if positions align with targets (within tolerance), False otherwise.
        """
        logger.info("Checking if positions match targets...")
        expected_btc_qty = float(target_btc_qty)
        expected_eth_qty = -float(target_eth_qty)
        logger.info(f"{Fore.CYAN}Target quantities: BTC={expected_btc_qty:.4f}, ETH={expected_eth_qty:.4f}")
        logger.info(f"{Fore.WHITE}Actual positions:  BTC={pos_btc.get('qty', 0.0):.4f}, ETH={pos_eth.get('qty', 0.0):.4f}")


        if pos_btc.get("qty", 0.0) == 0 and pos_eth.get("qty", 0.0) == 0:
            return False # Not aligned if targets are non-zero

        # Require proper directionality (long BTC, short ETH)
        if expected_btc_qty <= 0 or expected_eth_qty >= 0:
            logger.warning("Target directionality is incorrect.")
            return False
        if pos_btc.get("qty", 0.0) < 0 or pos_eth.get("qty", 0.0) > 0:
            logger.warning("Actual position directionality is incorrect.")
            return False

        try:
            lot_btc = abs(float(self.client.get_lot_size(self.BTC_SYMBOL)))
        except (TypeError, ValueError):
            lot_btc = 0.0
        try:
            lot_eth = abs(float(self.client.get_lot_size(self.ETH_SYMBOL)))
        except (TypeError, ValueError):
            lot_eth = 0.0

        lot_btc = lot_btc if lot_btc > 0 else 0.0001
        lot_eth = lot_eth if lot_eth > 0 else 0.001

        def matches(actual: float, expected: float, lot_size: float, symbol: str) -> bool:
            if expected == 0:
                match = abs(actual) <= lot_size
                logger.info(f"[{symbol}] Match check (expected is 0): actual={actual:.4f}, lot_size={lot_size:.4f} -> {match}")
                return match

            diff = abs(actual - expected)
            threshold = max(lot_size, abs(expected) * tolerance_pct)
            match = diff <= threshold
            logger.info(f"[{symbol}] Match check: actual={actual:.4f}, expected={expected:.4f}, diff={diff:.4f}, threshold={threshold:.4f} -> {match}")
            return match

        btc_ok = matches(float(pos_btc.get("qty", 0.0)), expected_btc_qty, lot_btc, self.BTC_SYMBOL)
        eth_ok = matches(float(pos_eth.get("qty", 0.0)), expected_eth_qty, lot_eth, self.ETH_SYMBOL)

        result = btc_ok and eth_ok
        logger.info(f"Positions match targets: {result} (BTC: {btc_ok}, ETH: {eth_ok})")
        return result

    def _infer_position_open_time(self, *positions: Dict[str, Any]) -> Optional[datetime]:
        """
        Infer the earliest open timestamp from provided positions.

        Args:
            positions: Position dicts that may contain 'opened_at' (seconds since epoch).

        Returns:
            datetime of earliest open if available, otherwise None.
        """
        timestamps = []
        for pos in positions:
            raw = pos.get("opened_at")
            if raw is None:
                continue
            try:
                ts = float(raw)
                if ts > 1e12:  # protect against ms
                    ts = ts / 1000.0
                timestamps.append(datetime.fromtimestamp(ts))
            except Exception:
                continue

        if timestamps:
            return min(timestamps)
        return None

    def market_simple(
        self,
        symbol: str,
        side: str,  # "buy" or "sell"
        quantity: float,
        reduce_only: bool = False
    ):
        """
        Execute a market order.
        Args:
            symbol: Trading symbol
            side: "buy" or "sell"
            quantity: Quantity to trade
            reduce_only: Reduce-only flag
        """
        if quantity <= 0:
            logger.warning(f"Skipping {side} {symbol}: quantity={quantity}")
            return

        logger.info(f"Placing {side} market order for {quantity:.4f} {symbol} (reduce_only={reduce_only})")

        try:
            # Place market order
            order_id = self.client.place_market_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                reduce_only=reduce_only
            )

            logger.info(f"Market order placed: {order_id}")

        except Exception as e:
            logger.error(f"Failed to execute market order: {e}")

    def open_pair(self, targets: Optional[Tuple[float, float]] = None):
        """Open BTC long and ETH short positions."""
        logger.info(f"{Fore.CYAN}{'=' * 60}")
        logger.info(f"{Fore.CYAN}OPENING PAIR POSITIONS")
        logger.info(f"{Fore.CYAN}{'=' * 60}")

        try:
            # Compute targets
            if targets is None:
                if self.saved_btc_qty is not None and self.saved_eth_qty is not None:
                    q_btc, q_eth = self.saved_btc_qty, self.saved_eth_qty
                else:
                    q_btc, q_eth = self.compute_targets()
            else:
                q_btc, q_eth = targets

            q_btc = float(q_btc)
            q_eth = float(q_eth)

            # Open BTC long
            logger.info(f"{Fore.GREEN}Opening BTC long: {q_btc:.4f} {self.BTC_SYMBOL}")
            btc_order_id = self.client.place_market_order(self.BTC_SYMBOL, "buy", q_btc, reduce_only=False)
            logger.info(f"BTC long order placed: {btc_order_id}")

            # Small delay between orders
            time.sleep(0.5)

            # Open ETH short
            logger.info(f"{Fore.RED}Opening ETH short: {q_eth:.4f} {self.ETH_SYMBOL}")
            eth_order_id = self.client.place_market_order(self.ETH_SYMBOL, "sell", q_eth, reduce_only=False)
            logger.info(f"ETH short order placed: {eth_order_id}")


            logger.info("Pair positions opened")
            self.last_deploy_at = datetime.now()
            if self.reference_account_value is None:
                self.reference_account_value = self.client.get_equity()
            refresh_hours = self.get_config("refresh_hours", 8)
            self.next_refresh = self.last_deploy_at + timedelta(hours=refresh_hours)
            self._save_state(q_btc, q_eth, self._get_hedge_ratio())

        except Exception as e:
            logger.error(f"Failed to open pair: {e}")

    def close_pair(self):
        """Close BTC and ETH positions."""
        logger.info(f"{Fore.CYAN}{'=' * 60}")
        logger.info(f"{Fore.CYAN}CLOSING PAIR POSITIONS")
        logger.info(f"{Fore.CYAN}{'=' * 60}")

        try:
            # Get current positions
            pos_btc = self.client.get_position(self.BTC_SYMBOL)
            pos_eth = self.client.get_position(self.ETH_SYMBOL)

            logger.info(f"Current positions - BTC: {pos_btc['qty']:.4f}, ETH: {pos_eth['qty']:.4f}")

            # Close BTC position (if long)
            if pos_btc['qty'] > 0:
                logger.info(f"{Fore.RED}Closing BTC long: {pos_btc['qty']:.4f} {self.BTC_SYMBOL}")
                btc_order_id = self.client.place_market_order(self.BTC_SYMBOL, "sell", pos_btc['qty'], reduce_only=True)
                logger.info(f"BTC close order placed: {btc_order_id}")


            # Small delay
            time.sleep(0.5)

            # Close ETH position (if short)
            if pos_eth['qty'] < 0:
                logger.info(f"{Fore.GREEN}Closing ETH short: {abs(pos_eth['qty']):.4f} {self.ETH_SYMBOL}")
                eth_order_id = self.client.place_market_order(self.ETH_SYMBOL, "buy", abs(pos_eth['qty']), reduce_only=True)
                logger.info(f"ETH close order placed: {eth_order_id}")

            logger.info("Pair positions closed")
            self._clear_state()

        except Exception as e:
            logger.error(f"Failed to close pair: {e}")

    def check_stoploss(self) -> bool:
        """
        Check if stop-loss is triggered on either leg.

        Returns:
            True if stop-loss triggered, False otherwise
        """
        stoploss_pct = self.get_config("stoploss_pct", 5) / 100.0

        try:
            # Check BTC position
            pos_btc = self.client.get_position(self.BTC_SYMBOL)
            if pos_btc['notional'] > 0:
                pnl_pct_btc = pos_btc['unrealized_pnl'] / pos_btc['notional']
                if pnl_pct_btc <= -stoploss_pct:
                    logger.warning(f"{Fore.RED}⚠️  STOP-LOSS TRIGGERED on BTC: PnL {pnl_pct_btc*100:.2f}%")
                    return True

            # Check ETH position
            pos_eth = self.client.get_position(self.ETH_SYMBOL)
            if pos_eth['notional'] > 0:
                pnl_pct_eth = pos_eth['unrealized_pnl'] / pos_eth['notional']
                if pnl_pct_eth <= -stoploss_pct:
                    logger.warning(f"{Fore.RED}⚠️  STOP-LOSS TRIGGERED on ETH: PnL {pnl_pct_eth*100:.2f}%")
                    return True

            return False

        except Exception as e:
            logger.error(f"Failed to check stop-loss: {e}")
            return False

    def reconcile_and_update_state(self):
        """
        Reconcile current positions to new targets by closing all and reopening.
        """
        logger.info("Reconciling positions: closing all and reopening with new targets.")
        try:
            # Close all existing positions to ensure a clean slate
            self.close_pair()
            time.sleep(2)  # Give time for orders to process

            # Open new positions with freshly computed targets
            self.open_pair()

            logger.info("Reconciliation complete. New positions opened.")

        except Exception as e:
            logger.error(f"Failed to reconcile positions: {e}", exc_info=True)

    def print_status(self):
        """Print current status."""
        try:
            equity = self.client.get_equity()
            pos_btc = self.client.get_position(self.BTC_SYMBOL)
            pos_eth = self.client.get_position(self.ETH_SYMBOL)

            total_pnl = pos_btc['unrealized_pnl'] + pos_eth['unrealized_pnl']
            pnl_color = Fore.GREEN if total_pnl >= 0 else Fore.RED
            btc_pnl_color = Fore.GREEN if pos_btc['unrealized_pnl'] >= 0 else Fore.RED
            eth_pnl_color = Fore.GREEN if pos_eth['unrealized_pnl'] >= 0 else Fore.RED

            logger.info(f"{Fore.MAGENTA}{'-' * 60}")
            logger.info(f"{Fore.YELLOW}Equity: ${equity:.2f} | Total PnL for current position: {pnl_color}${total_pnl:.2f}")
            
            if self.reference_account_value is not None and self.reference_account_value > 0:
                long_term_pnl = equity - self.reference_account_value
                long_term_pnl_pct = (long_term_pnl / self.reference_account_value) * 100
                long_term_pnl_color = Fore.GREEN if long_term_pnl >= 0 else Fore.RED
                logger.info(f"Long-term PnL: {long_term_pnl_color}${long_term_pnl:.2f} ({long_term_pnl_pct:+.2f}%)" f"{Style.RESET_ALL} | Reference Equity: ${self.reference_account_value:.2f}")

            logger.info(f"BTC: {pos_btc['qty']:.4f} @ ${pos_btc['entry_price']:.2f} | PnL: {btc_pnl_color}${pos_btc['unrealized_pnl']:.2f}")
            logger.info(f"ETH: {pos_eth['qty']:.4f} @ ${pos_eth['entry_price']:.2f} | PnL: {eth_pnl_color}${pos_eth['unrealized_pnl']:.2f}")

            if self.next_refresh:
                time_to_refresh = (self.next_refresh - datetime.now()).total_seconds() / 60
                logger.info(f"{Fore.CYAN}Next refresh in: {time_to_refresh:.1f} minutes")

            if self.stoploss_triggered:
                logger.info(f"{Fore.RED}⚠️  STOP-LOSS ACTIVE - Waiting for next refresh")

            logger.info(f"{Fore.MAGENTA}{'-' * 60}")

        except Exception as e:
            logger.error(f"Failed to print status: {e}")

    def run(self):
        """Main bot loop."""
        logger.info(f"{Fore.BLUE}{'=' * 60}")
        logger.info(f"{Fore.BLUE}BTC-ETH HEDGE BOT STARTING")
        logger.info(f"{Fore.BLUE}{'=' * 60}")

        # Cancel all orders on startup
        logger.info("Cancelling all open orders...")
        self.client.cancel_all_orders()
        time.sleep(1)

        # Check for existing positions and state
        pos_btc = self.client.get_position(self.BTC_SYMBOL)
        pos_eth = self.client.get_position(self.ETH_SYMBOL)

        # Scenario 1: State file exists
        if self.saved_btc_qty is not None and self.saved_eth_qty is not None:
            logger.info("Saved state found. Checking alignment against saved targets.")
            if self._positions_match_targets(pos_btc, pos_eth, self.saved_btc_qty, self.saved_eth_qty):
                logger.info(f"{Fore.GREEN}Positions align with saved state. Resuming operation.")
            else:
                logger.warning(f"{Fore.YELLOW}Positions do NOT align with saved state. Reconciling.")
                self.reconcile_and_update_state()
        
        # Scenario 2: No state file
        else:
            logger.info("No state file found.")
            if pos_btc['qty'] != 0 or pos_eth['qty'] != 0:
                logger.warning(f"{Fore.YELLOW}Orphan positions found without a state file. Reconciling.")
                self.reconcile_and_update_state()
            else:
                logger.info("No existing positions or state. Opening initial pair.")
                self.open_pair()

        # Print initial status
        self.print_status()

        # Main loop
        last_status_print = time.time()
        status_interval = self.get_config("status_interval_seconds", 60)

        while self.running:

            try:
                # Check stop-loss
                if not self.stoploss_triggered and self.check_stoploss():
                    logger.warning(f"{Fore.RED}🛑 STOP-LOSS TRIGGERED - Closing all positions")
                    self.close_pair()
                    self.stoploss_triggered = True
                    logger.info(f"Waiting until next refresh at {self.next_refresh}")

                # Check refresh time
                if self.next_refresh and datetime.now() >= self.next_refresh:
                    logger.info(f"{Fore.CYAN}⏰ Refresh time reached")

                    # Close existing positions
                    self.close_pair()
                    time.sleep(2)

                    # Reset stop-loss flag
                    self.stoploss_triggered = False

                    # Refresh hedge ratio (force recalculation)
                    self._get_hedge_ratio(force_refresh=True)

                    # Open new positions
                    target_btc_qty, target_eth_qty = self.compute_targets()
                    self.open_pair(targets=(target_btc_qty, target_eth_qty))

                    # Schedule next refresh
                    refresh_hours = self.get_config("refresh_hours", 8)
                    if self.last_deploy_at is not None:
                        self.next_refresh = self.last_deploy_at + timedelta(hours=refresh_hours)
                    else:
                        self.next_refresh = datetime.now() + timedelta(hours=refresh_hours)
                    logger.info(f"Next refresh scheduled for: {self.next_refresh}")

                # Print status periodically
                if time.time() - last_status_print >= status_interval:
                    self.print_status()
                    last_status_print = time.time()

                # Sleep
                sleep_duration = self.get_config("loop_sleep_seconds", 5)
                time.sleep(sleep_duration)

            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received")
                self.running = False
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(10)

        # Shutdown
        logger.info("Shutting down...")
        # logger.info("Closing all positions...")
        # self.close_pair()
        # time.sleep(2)

        logger.info("Cancelling all orders...")
        self.client.cancel_all_orders()

        logger.info(f"{Fore.BLUE}{'=' * 60}")
        logger.info(f"{Fore.BLUE}BOT STOPPED")
        logger.info(f"{Fore.BLUE}{'=' * 60}")


def main():
    """Entry point."""
    config_path = Path(__file__).parent / "config.json"

    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        logger.error("Please create config.json with required parameters")
        sys.exit(1)

    try:
        bot = HedgeBot(config_path)
        bot.run()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
