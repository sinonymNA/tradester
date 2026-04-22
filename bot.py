import logging
import math
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional
from uuid import uuid4

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    TrailingStopOrderRequest,
)


# ==============================
# Configuration
# ==============================
load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_SECRET_KEY")
PAPER_TRADING = os.getenv("ALPACA_PAPER", "true").lower() == "true"
SYMBOL = "QQQ"
TIMEZONE = ZoneInfo("America/New_York")

ENTRY_START = dtime(9, 45)
ENTRY_END = dtime(15, 30)
REGULAR_CLOSE = dtime(16, 0)

VWAP_STD_LOOKBACK = 20
VOLUME_LOOKBACK = 20
RSI_LENGTH = 14
VOLUME_MULTIPLIER = 1.5
RSI_THRESHOLD = 30
HARD_STOP_PCT = 0.005          # 0.5%
TRAIL_STOP_PCT = 0.2           # Alpaca trailing stop percent uses whole percent, so 0.2 = 0.2%
MAX_DAILY_LOSS = -500.0
MAX_DAILY_PROFIT = 1000.0
POLL_DELAY_AFTER_MINUTE_SEC = 3
POSITION_RISK_BUDGET = float(os.getenv("POSITION_RISK_BUDGET", "100"))
MIN_CASH_BUFFER = float(os.getenv("MIN_CASH_BUFFER", "1000"))
DATA_FEED_RAW = os.getenv("ALPACA_DATA_FEED", "iex").lower()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
)
logger = logging.getLogger("mean_reversion_bot")

if not API_KEY or not API_SECRET:
    raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the environment or .env file")

DATA_FEED = {
    "iex": DataFeed.IEX,
    "sip": DataFeed.SIP,
    "delayed_sip": DataFeed.DELAYED_SIP,
}.get(DATA_FEED_RAW, DataFeed.IEX)


@dataclass
class PositionState:
    has_position: bool = False
    entry_price: Optional[float] = None
    entry_qty: float = 0.0
    partial_exit_done: bool = False
    trailing_order_id: Optional[str] = None
    last_signal_bar_ts: Optional[str] = None


@dataclass
class RuntimeState:
    is_running: bool = True
    trading_enabled: bool = True
    current_balance: float = 0.0
    unrealized_pnl: float = 0.0
    start_of_day_equity: Optional[float] = None
    trading_day: Optional[date] = None
    stop_reason: Optional[str] = None
    position: PositionState = field(default_factory=PositionState)


class MeanReversionBot:
    def __init__(self) -> None:
        self.trading_client = TradingClient(API_KEY, API_SECRET, paper=PAPER_TRADING)
        self.data_client = StockHistoricalDataClient(API_KEY, API_SECRET)
        self.state = RuntimeState()
        self.lock = threading.RLock()
        self.shutdown_event = threading.Event()
        self.thread = threading.Thread(target=self.run_loop, name="trading-loop", daemon=True)

    # ------------------------------
    # Lifecycle
    # ------------------------------
    def start(self) -> None:
        logger.info("Starting Mean Reversion 3.0 bot")
        self.sync_account_state(reset_day=True)
        self.thread.start()

    def stop(self) -> None:
        with self.lock:
            self.state.is_running = False
        self.shutdown_event.set()
        logger.info("Stop requested")

    # ------------------------------
    # Time helpers
    # ------------------------------
    @staticmethod
    def now_et() -> datetime:
        return datetime.now(TIMEZONE)

    def in_entry_window(self, ts: datetime) -> bool:
        return ENTRY_START <= ts.time() <= ENTRY_END

    def in_regular_session(self, ts: datetime) -> bool:
        return dtime(9, 30) <= ts.time() <= REGULAR_CLOSE

    def seconds_until_next_poll(self) -> float:
        now = self.now_et()
        next_minute = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
        target = next_minute + timedelta(seconds=POLL_DELAY_AFTER_MINUTE_SEC)
        return max((target - now).total_seconds(), 0.5)

    # ------------------------------
    # Broker/account helpers
    # ------------------------------
    def sync_account_state(self, reset_day: bool = False) -> None:
        account = self.trading_client.get_account()
        now = self.now_et()

        with self.lock:
            self.state.current_balance = float(account.equity)

            try:
                position = self.trading_client.get_open_position(SYMBOL)
                self.state.unrealized_pnl = float(position.unrealized_pl)
                self.state.position.has_position = True
                self.state.position.entry_price = float(position.avg_entry_price)
                self.state.position.entry_qty = float(position.qty)

                open_orders = self.trading_client.get_orders(
                    filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL])
                )
                trailing_order = next(
                    (o for o in open_orders if getattr(getattr(o, "order_type", None), "value", "") == "trailing_stop"),
                    None,
                )
                if trailing_order is not None:
                    self.state.position.partial_exit_done = True
                    self.state.position.trailing_order_id = str(trailing_order.id)
            except APIError:
                self.state.unrealized_pnl = 0.0
                self.state.position.has_position = False
                self.state.position.entry_price = None
                self.state.position.entry_qty = 0.0
                self.state.position.partial_exit_done = False
                self.state.position.trailing_order_id = None

            if reset_day or self.state.trading_day != now.date():
                self.state.trading_day = now.date()
                self.state.start_of_day_equity = float(account.equity)
                self.state.trading_enabled = True
                self.state.stop_reason = None
                logger.info("Daily equity baseline reset to %.2f", self.state.start_of_day_equity)

    def day_pnl(self) -> float:
        with self.lock:
            if self.state.start_of_day_equity is None:
                return 0.0
            return self.state.current_balance - self.state.start_of_day_equity

    def enforce_daily_limits(self) -> None:
        pnl = self.day_pnl()
        if pnl <= MAX_DAILY_LOSS:
            self.disable_trading(f"Max daily loss hit ({pnl:.2f})")
        elif pnl >= MAX_DAILY_PROFIT:
            self.disable_trading(f"Max daily profit hit ({pnl:.2f})")

    def disable_trading(self, reason: str) -> None:
        with self.lock:
            if not self.state.trading_enabled:
                return
            self.state.trading_enabled = False
            self.state.stop_reason = reason

        logger.warning("Trading disabled: %s", reason)
        self.cancel_symbol_orders()
        self.flatten_position(reason=reason)

    def cancel_symbol_orders(self) -> None:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL])
        open_orders = self.trading_client.get_orders(filter=request)
        for order in open_orders:
            try:
                self.trading_client.cancel_order_by_id(order.id)
                logger.info("Cancelled order %s", order.id)
            except Exception as exc:
                logger.exception("Failed to cancel order %s: %s", order.id, exc)

    # ------------------------------
    # Market data / signal
    # ------------------------------
    def fetch_intraday_bars(self) -> pd.DataFrame:
        now = self.now_et()
        market_open = datetime.combine(now.date(), dtime(9, 30), tzinfo=TIMEZONE)

        request = StockBarsRequest(
            symbol_or_symbols=SYMBOL,
            timeframe=TimeFrame.Minute,
            start=market_open,
            end=now,
            feed=DATA_FEED,
            limit=500,
        )
        bars = self.data_client.get_stock_bars(request).df

        if bars.empty:
            return pd.DataFrame()

        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(SYMBOL)

        bars = bars.copy()
        if bars.index.tz is None:
            bars.index = bars.index.tz_localize("UTC").tz_convert(TIMEZONE)
        else:
            bars.index = bars.index.tz_convert(TIMEZONE)

        bars = bars.sort_index()
        return bars

    @staticmethod
    def compute_indicators(bars: pd.DataFrame) -> pd.DataFrame:
        df = bars.copy()
        df["tpv"] = df["close"] * df["volume"]
        df["cum_tpv"] = df["tpv"].cumsum()
        df["cum_vol"] = df["volume"].cumsum().replace(0, pd.NA)
        df["vwap"] = df["cum_tpv"] / df["cum_vol"]
        df["vwap_deviation"] = df["close"] - df["vwap"]
        df["std_vwap_dev"] = df["vwap_deviation"].rolling(VWAP_STD_LOOKBACK).std()
        df["lower_band_3sigma"] = df["vwap"] - 3.0 * df["std_vwap_dev"]
        df["avg_volume_20"] = df["volume"].rolling(VOLUME_LOOKBACK).mean()
        df["rsi"] = ta.rsi(df["close"], length=RSI_LENGTH)
        return df

    def entry_signal(self, df: pd.DataFrame) -> bool:
        if len(df) < max(VWAP_STD_LOOKBACK, RSI_LENGTH) + 2:
            return False

        prev_bar = df.iloc[-2]
        last_bar = df.iloc[-1]

        if pd.isna(last_bar["lower_band_3sigma"]) or pd.isna(last_bar["avg_volume_20"]) or pd.isna(last_bar["rsi"]):
            return False

        crossed_below = prev_bar["close"] >= prev_bar["lower_band_3sigma"] and last_bar["close"] < last_bar["lower_band_3sigma"]
        volume_confirmed = last_bar["volume"] > VOLUME_MULTIPLIER * last_bar["avg_volume_20"]
        rsi_confirmed = last_bar["rsi"] < RSI_THRESHOLD

        with self.lock:
            duplicate_bar = self.state.position.last_signal_bar_ts == last_bar.name.isoformat()

        return crossed_below and volume_confirmed and rsi_confirmed and not duplicate_bar

    # ------------------------------
    # Position sizing / execution
    # ------------------------------
    def calculate_order_qty(self, price: float) -> int:
        account = self.trading_client.get_account()
        cash = float(account.cash)
        available_cash = max(cash - MIN_CASH_BUFFER, 0.0)

        if available_cash <= 0:
            return 0

        risk_per_share = price * HARD_STOP_PCT
        risk_qty = math.floor(POSITION_RISK_BUDGET / risk_per_share) if risk_per_share > 0 else 0
        cash_qty = math.floor(available_cash / price)
        qty = max(min(risk_qty, cash_qty), 0)
        return qty

    def submit_market_order(self, side: OrderSide, qty: float, client_order_prefix: str) -> None:
        if qty <= 0:
            return

        order = MarketOrderRequest(
            symbol=SYMBOL,
            qty=qty,
            side=side,
            type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            client_order_id=f"{client_order_prefix}-{uuid4().hex[:10]}",
        )
        self.trading_client.submit_order(order_data=order)
        logger.info("Submitted %s market order for %.4f shares", side.value, qty)

    def enter_position(self, price: float, signal_ts: str) -> None:
        qty = self.calculate_order_qty(price)
        if qty < 1:
            logger.warning("Signal fired but calculated quantity was < 1 share")
            return

        self.submit_market_order(OrderSide.BUY, qty, "entry")
        time.sleep(1.5)
        self.sync_account_state(reset_day=False)

        with self.lock:
            self.state.position.partial_exit_done = False
            self.state.position.trailing_order_id = None
            self.state.position.last_signal_bar_ts = signal_ts

        logger.info("Entered %s position | qty=%s | approx_price=%.2f", SYMBOL, qty, price)

    def take_partial_profit_and_arm_trailing(self) -> None:
        with self.lock:
            if not self.state.position.has_position or self.state.position.partial_exit_done:
                return
            current_qty = self.state.position.entry_qty

        partial_qty = math.floor(current_qty * 0.70)
        remaining_qty = current_qty - partial_qty

        if partial_qty < 1:
            logger.info("Position too small for 70%% partial exit; skipping partial logic")
            return

        self.cancel_symbol_orders()
        self.submit_market_order(OrderSide.SELL, partial_qty, "partial")
        time.sleep(1.0)
        self.sync_account_state(reset_day=False)

        trailing_qty = max(math.floor(remaining_qty), 0)
        if trailing_qty >= 1:
            trailing_order = TrailingStopOrderRequest(
                symbol=SYMBOL,
                qty=trailing_qty,
                side=OrderSide.SELL,
                type=OrderType.TRAILING_STOP,
                time_in_force=TimeInForce.GTC,
                trail_percent=TRAIL_STOP_PCT,
                client_order_id=f"trail-{uuid4().hex[:10]}",
            )
            order = self.trading_client.submit_order(order_data=trailing_order)
            with self.lock:
                self.state.position.trailing_order_id = str(order.id)

        with self.lock:
            self.state.position.partial_exit_done = True

        logger.info("Partial profit executed and trailing stop armed")

    def flatten_position(self, reason: str) -> None:
        try:
            position = self.trading_client.get_open_position(SYMBOL)
        except APIError:
            return

        qty = float(position.qty)
        if qty <= 0:
            return

        logger.warning("Flattening %s position | qty=%s | reason=%s", SYMBOL, qty, reason)
        self.cancel_symbol_orders()
        self.submit_market_order(OrderSide.SELL, qty, "flatten")
        time.sleep(1.0)
        self.sync_account_state(reset_day=False)

    # ------------------------------
    # Position management
    # ------------------------------
    def manage_open_position(self, df: pd.DataFrame) -> None:
        with self.lock:
            if not self.state.position.has_position or self.state.position.entry_price is None:
                return
            entry_price = self.state.position.entry_price
            partial_exit_done = self.state.position.partial_exit_done

        last_bar = df.iloc[-1]
        current_price = float(last_bar["close"])
        current_vwap = float(last_bar["vwap"])
        hard_stop = entry_price * (1.0 - HARD_STOP_PCT)

        if current_price <= hard_stop:
            self.flatten_position(reason=f"Hard stop hit at {current_price:.2f}")
            return

        if not partial_exit_done and current_price >= current_vwap:
            self.take_partial_profit_and_arm_trailing()

    # ------------------------------
    # Main loop
    # ------------------------------
    def run_loop(self) -> None:
        logger.info("Trading loop running")

        while not self.shutdown_event.is_set():
            try:
                now = self.now_et()

                # Reset state on a new day.
                with self.lock:
                    current_day = self.state.trading_day
                if current_day != now.date():
                    self.sync_account_state(reset_day=True)

                # Refresh account / position state every cycle.
                self.sync_account_state(reset_day=False)
                self.enforce_daily_limits()

                if not self.in_regular_session(now):
                    sleep_for = self.seconds_until_next_poll() if now.time() < dtime(16, 5) else 30
                    self.shutdown_event.wait(timeout=sleep_for)
                    continue

                bars = self.fetch_intraday_bars()
                if bars.empty:
                    self.shutdown_event.wait(timeout=self.seconds_until_next_poll())
                    continue

                df = self.compute_indicators(bars)
                self.manage_open_position(df)

                with self.lock:
                    has_position = self.state.position.has_position
                    trading_enabled = self.state.trading_enabled

                if trading_enabled and not has_position and self.in_entry_window(now) and self.entry_signal(df):
                    signal_ts = df.iloc[-1].name.isoformat()
                    signal_price = float(df.iloc[-1]["close"])
                    self.enter_position(signal_price, signal_ts)

            except Exception as exc:
                logger.exception("Unexpected error in run loop: %s", exc)

            self.shutdown_event.wait(timeout=self.seconds_until_next_poll())

        logger.info("Trading loop stopped")

    # ------------------------------
    # Status API
    # ------------------------------
    def status_payload(self) -> dict:
        self.sync_account_state(reset_day=False)
        with self.lock:
            return {
                "current_balance": round(self.state.current_balance, 2),
                "unrealized_pnl": round(self.state.unrealized_pnl, 2),
                "is_running": bool(self.state.is_running and not self.shutdown_event.is_set()),
                "trading_enabled": self.state.trading_enabled,
                "day_pnl": round(self.day_pnl(), 2),
                "stop_reason": self.state.stop_reason,
                "symbol": SYMBOL,
                "strategy": "Mean Reversion 3.0",
            }


bot = MeanReversionBot()
app = FastAPI(title="Mean Reversion 3.0", version="1.0.0")


@app.on_event("startup")
def on_startup() -> None:
    bot.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    bot.stop()


@app.get("/status")
def get_status() -> JSONResponse:
    return JSONResponse(bot.status_payload())


@app.get("/")
def root() -> JSONResponse:
    return JSONResponse({"message": "Mean Reversion 3.0 bot online", "status_endpoint": "/status"})


def _handle_signal(signum, _frame) -> None:
    logger.info("Received signal %s", signum)
    bot.stop()


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
