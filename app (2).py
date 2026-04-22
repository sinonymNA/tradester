import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv()

TIMEZONE = ZoneInfo("America/New_York")
BOT_STATUS_URL = os.getenv("BOT_STATUS_URL", "http://127.0.0.1:8000/status")
API_KEY = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_SECRET_KEY")
PAPER_TRADING = os.getenv("ALPACA_PAPER", "true").lower() == "true"

st.set_page_config(
    page_title="Mean Reversion 3.0 | Command Center",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
        :root {
            --bg: #0b1220;
            --panel: #111827;
            --panel-2: #0f172a;
            --border: rgba(148, 163, 184, 0.18);
            --text: #e5e7eb;
            --muted: #94a3b8;
            --green: #10b981;
            --red: #ef4444;
            --amber: #f59e0b;
            --blue: #38bdf8;
        }

        .stApp {
            background:
                radial-gradient(circle at top left, rgba(56, 189, 248, 0.08), transparent 25%),
                radial-gradient(circle at top right, rgba(16, 185, 129, 0.06), transparent 20%),
                linear-gradient(180deg, #07101d 0%, #0b1220 100%);
            color: var(--text);
        }

        .block-container {
            padding-top: 1.6rem;
            padding-bottom: 2rem;
            max-width: 1400px;
        }

        [data-testid="stMetricValue"],
        [data-testid="stMetricLabel"] {
            color: var(--text);
        }

        div[data-testid="stHorizontalBlock"] > div {
            background: transparent;
        }

        .hero-wrap {
            background: linear-gradient(180deg, rgba(17,24,39,0.92), rgba(15,23,42,0.88));
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 1.15rem 1.25rem;
            margin-bottom: 1rem;
            box-shadow: 0 10px 30px rgba(2, 6, 23, 0.35);
        }

        .hero-title {
            font-size: 2rem;
            font-weight: 750;
            letter-spacing: 0.02em;
            margin-bottom: 0.15rem;
        }

        .hero-subtitle {
            color: var(--muted);
            font-size: 0.98rem;
        }

        .metric-card {
            background: linear-gradient(180deg, rgba(17,24,39,0.96), rgba(15,23,42,0.92));
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 1.2rem 1.25rem;
            min-height: 170px;
            box-shadow: 0 10px 25px rgba(2, 6, 23, 0.28);
        }

        .metric-label {
            color: var(--muted);
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 0.6rem;
        }

        .metric-value {
            color: var(--text);
            font-size: 2.35rem;
            font-weight: 800;
            line-height: 1.05;
            margin-bottom: 0.55rem;
        }

        .metric-subtitle {
            color: var(--muted);
            font-size: 0.92rem;
        }

        .section-card {
            background: linear-gradient(180deg, rgba(17,24,39,0.96), rgba(15,23,42,0.92));
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 1.2rem 1.25rem;
            margin-top: 1rem;
            box-shadow: 0 10px 25px rgba(2, 6, 23, 0.28);
        }

        .section-title {
            font-size: 1.2rem;
            font-weight: 700;
            margin-bottom: 0.35rem;
        }

        .pill {
            display: inline-block;
            padding: 0.34rem 0.7rem;
            border-radius: 999px;
            font-size: 0.82rem;
            font-weight: 700;
            letter-spacing: 0.03em;
            margin-right: 0.45rem;
            margin-bottom: 0.45rem;
            border: 1px solid transparent;
        }

        .pill-green {
            background: rgba(16, 185, 129, 0.14);
            color: #86efac;
            border-color: rgba(16, 185, 129, 0.3);
        }

        .pill-red {
            background: rgba(239, 68, 68, 0.14);
            color: #fca5a5;
            border-color: rgba(239, 68, 68, 0.3);
        }

        .pill-amber {
            background: rgba(245, 158, 11, 0.14);
            color: #fcd34d;
            border-color: rgba(245, 158, 11, 0.3);
        }

        .pill-blue {
            background: rgba(56, 189, 248, 0.14);
            color: #7dd3fc;
            border-color: rgba(56, 189, 248, 0.3);
        }

        div.stButton > button {
            width: 100%;
            min-height: 84px;
            font-size: 1.35rem;
            font-weight: 800;
            letter-spacing: 0.04em;
            border-radius: 18px;
            color: white;
            border: 1px solid rgba(239, 68, 68, 0.55);
            background: linear-gradient(180deg, rgba(185, 28, 28, 1), rgba(127, 29, 29, 1));
            box-shadow: 0 14px 30px rgba(127, 29, 29, 0.35);
        }

        div.stButton > button:hover {
            border-color: rgba(248, 113, 113, 0.9);
            background: linear-gradient(180deg, rgba(220, 38, 38, 1), rgba(153, 27, 27, 1));
        }

        .tiny-note {
            color: var(--muted);
            font-size: 0.88rem;
            margin-top: 0.35rem;
        }

        .footer-note {
            color: var(--muted);
            font-size: 0.85rem;
            margin-top: 1rem;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


def format_currency(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


def format_count(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,}"


@st.cache_resource
def get_trading_client() -> TradingClient:
    if not API_KEY or not API_SECRET:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set.")
    return TradingClient(API_KEY, API_SECRET, paper=PAPER_TRADING)


def fetch_bot_status() -> tuple[dict | None, str | None]:
    try:
        req = urllib.request.Request(BOT_STATUS_URL, headers={"User-Agent": "streamlit-command-center"})
        with urllib.request.urlopen(req, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload, None
    except urllib.error.URLError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, str(exc)


def fetch_account_snapshot() -> tuple[dict | None, str | None]:
    try:
        client = get_trading_client()
        account = client.get_account()
        positions = client.get_all_positions()

        equity = float(account.equity)
        last_equity = float(account.last_equity) if getattr(account, "last_equity", None) is not None else None

        return {
            "equity": equity,
            "last_equity": last_equity,
            "positions_count": len(positions),
        }, None
    except Exception as exc:
        return None, str(exc)


def render_metric_card(title: str, value: str, subtitle: str) -> None:
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-label">{title}</div>
            <div class="metric-value">{value}</div>
            <div class="metric-subtitle">{subtitle}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


st.markdown(
    """
    <div class="hero-wrap">
        <div class="hero-title">Mean Reversion 3.0 — Command Center</div>
        <div class="hero-subtitle">
            Institutional dark-mode dashboard for QQQ. Live heartbeat refreshes every 30 seconds and the emergency control path can liquidate all open positions immediately.
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

if not API_KEY or not API_SECRET:
    st.error("Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_SECRET_KEY in Railway variables.")
    st.stop()

control_col, info_col = st.columns([1.3, 2.2], gap="large")

with control_col:
    if st.button("EMERGENCY LIQUIDATE", use_container_width=True, type="primary"):
        try:
            client = get_trading_client()
            responses = client.close_all_positions(cancel_orders=True)
            st.success(f"Emergency liquidation sent. Close requests submitted: {len(responses)}")
        except Exception as exc:
            st.error(f"Emergency liquidation failed: {exc}")

    st.markdown(
        """
        <div class="tiny-note">
            This button calls Alpaca directly and attempts to cancel open orders before liquidating all open positions.
        </div>
        """,
        unsafe_allow_html=True,
    )

with info_col:
    st.markdown(
        f"""
        <div class="section-card">
            <div class="section-title">Deployment Linkage</div>
            <div class="tiny-note">
                By default, this dashboard reads the bot heartbeat from <code>{BOT_STATUS_URL}</code>. If you use the included Railway start script, no extra wiring is needed.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

metrics_container = st.container()
heartbeat_container = st.container()


@st.fragment(run_every="30s")
def live_dashboard() -> None:
    status_payload, status_error = fetch_bot_status()
    account_snapshot, account_error = fetch_account_snapshot()

    fallback_day_pnl = None
    if account_snapshot and account_snapshot.get("equity") is not None and account_snapshot.get("last_equity") is not None:
        fallback_day_pnl = account_snapshot["equity"] - account_snapshot["last_equity"]

    total_account_value = None
    if account_snapshot and account_snapshot.get("equity") is not None:
        total_account_value = account_snapshot["equity"]
    elif status_payload:
        total_account_value = status_payload.get("current_balance")

    todays_pnl = None
    pnl_source = "Bot day PnL"
    if status_payload and status_payload.get("day_pnl") is not None:
        todays_pnl = status_payload.get("day_pnl")
    else:
        todays_pnl = fallback_day_pnl
        pnl_source = "Alpaca equity delta fallback"

    active_positions = None
    if account_snapshot:
        active_positions = account_snapshot.get("positions_count")

    with metrics_container:
        col1, col2, col3 = st.columns(3, gap="large")
        with col1:
            render_metric_card("Total Account Value", format_currency(total_account_value), "Live account equity")
        with col2:
            render_metric_card("Today's PnL", format_currency(todays_pnl), pnl_source)
        with col3:
            render_metric_card("Active Positions", format_count(active_positions), "Open positions at Alpaca")

    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %I:%M:%S %p ET")

    with heartbeat_container:
        st.markdown(
            """
            <div class="section-card">
                <div class="section-title">Live Heartbeat</div>
            """,
            unsafe_allow_html=True,
        )

        if status_payload:
            is_running = bool(status_payload.get("is_running"))
            trading_enabled = bool(status_payload.get("trading_enabled", False))
            stop_reason = status_payload.get("stop_reason")

            status_pills = []
            status_pills.append(
                '<span class="pill pill-green">BOT RUNNING</span>' if is_running
                else '<span class="pill pill-red">BOT STOPPED</span>'
            )
            status_pills.append(
                '<span class="pill pill-blue">TRADING ENABLED</span>' if trading_enabled
                else '<span class="pill pill-amber">TRADING DISABLED</span>'
            )
            status_pills.append('<span class="pill pill-blue">HEARTBEAT ONLINE</span>')

            st.markdown("".join(status_pills), unsafe_allow_html=True)

            row1, row2, row3 = st.columns(3)
            row1.metric("Strategy", status_payload.get("strategy", "Mean Reversion 3.0"))
            row2.metric("Symbol", status_payload.get("symbol", "QQQ"))
            row3.metric("Unrealized PnL", format_currency(status_payload.get("unrealized_pnl")))

            st.caption(f"Last poll: {now}")

            if stop_reason:
                st.warning(f"Trading disabled reason: {stop_reason}")

            with st.expander("Raw /status payload"):
                st.json(status_payload)
        else:
            st.error(f"Heartbeat offline. Could not reach /status endpoint. Error: {status_error}")
            st.caption(f"Last poll attempt: {now}")

        if account_error:
            st.warning(f"Account snapshot warning: {account_error}")

        st.markdown(
            """
            <div class="footer-note">
                Auto-refresh interval: 30 seconds.
            </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


live_dashboard()
