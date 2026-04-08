"""
config.py — All system configuration loaded from environment variables.
Never hardcode secrets. All values come from Railway dashboard.
"""

import os
import logging
from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field, validator


class Settings(BaseSettings):
    """
    All configuration loaded from environment variables.
    Railway injects these at runtime from the Variables tab.
    """

    # ── Core API Keys ──────────────────────────────────────
    telegram_bot_token: str = Field(..., env="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(..., env="TELEGRAM_CHAT_ID")
    anthropic_api_key: str = Field(..., env="ANTHROPIC_API_KEY")
    tv_webhook_secret: str = Field(..., env="TV_WEBHOOK_SECRET")

    # ── Database ───────────────────────────────────────────
    database_url: str = Field(..., env="DATABASE_URL")

    # ── GitHub Integration ─────────────────────────────────
    github_token: str = Field(default="", env="GITHUB_TOKEN")
    github_username: str = Field(default="", env="GITHUB_USERNAME")
    github_repo: str = Field(default="smc-trading-bot", env="GITHUB_REPO")

    # ── Railway API (for rollback) ─────────────────────────
    railway_api_token: str = Field(default="", env="RAILWAY_API_TOKEN")
    railway_service_id: str = Field(default="", env="RAILWAY_SERVICE_ID")
    railway_project_id: str = Field(default="", env="RAILWAY_PROJECT_ID")

    # ── Server ─────────────────────────────────────────────
    port: int = Field(default=8000, env="PORT")
    railway_environment: str = Field(default="production", env="RAILWAY_ENVIRONMENT")

    # ── Trading Parameters ─────────────────────────────────
    account_size: int = Field(default=100_000, env="ACCOUNT_SIZE")
    risk_per_trade_percent: float = Field(default=1.0, env="RISK_PER_TRADE_PERCENT")
    min_confluence_score: int = Field(default=72, env="MIN_CONFLUENCE_SCORE")
    max_active_trades: int = Field(default=3, env="MAX_ACTIVE_TRADES")
    min_rr_ratio: float = Field(default=2.0, env="MIN_RR_RATIO")
    paper_mode: bool = Field(default=True, env="PAPER_MODE")

    @validator("database_url", pre=True)
    def fix_postgres_url(cls, v: str) -> str:
        """Railway provides postgres:// — SQLAlchemy needs postgresql+asyncpg://"""
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://") and "+asyncpg" not in v:
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


# ── Scoring weights (matches ABSOLUTE CONSTRAINTS spec) ───────────
SCORE_WEIGHTS = {
    "ob_entry":          20,
    "ob_fvg_overlap":    18,
    "htf_bias_match":    15,
    "premium_discount":  12,
    "liquidity_swept":   10,
    "killzone":           8,
    "ltf_choch":          8,
    "session_bias":       5,
    "options_confirm":    5,
    "max_pain_proximity": 4,
    "volume_ratio":       3,
}

SCORE_PENALTIES = {
    "htf_conflict":      -10,
    "ob_already_touched": -8,
    "lunch_hour":         -5,
    "trap_same_dir":      -5,
}

GRADE_THRESHOLDS = {
    "A_PLUS": 90,
    "A":      72,
    "B":      55,
    "C":       0,
}

# ── Segments ───────────────────────────────────────────────────────
SEGMENTS = {
    "INDICES":        "Indices (Nifty/BN)",
    "INDIAN_FNO":     "Indian F&O",
    "INDIAN_STOCKS":  "Indian Stocks",
    "COMMODITY":      "Commodity MCX",
    "CURRENCY_FOREX": "Currency / Forex",
    "CRYPTO":         "Crypto",
    "GLOBAL_INDICES": "Global Indices",
}

# ── F&O eligible stock list (NSE top 50 F&O symbols) ──────────────
FNO_SYMBOLS = {
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "HINDUNILVR", "HDFC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "ITC", "LT", "AXISBANK", "ASIANPAINT", "BAJFINANCE",
    "HCLTECH", "MARUTI", "ULTRACEMCO", "TITAN", "NESTLEIND",
    "WIPRO", "TECHM", "POWERGRID", "NTPC", "ONGC",
    "JSWSTEEL", "TATASTEEL", "HINDALCO", "COALINDIA", "ADANIENT",
    "ADANIPORTS", "BAJAJFINSV", "DIVISLAB", "DRREDDY", "CIPLA",
    "SUNPHARMA", "BRITANNIA", "GRASIM", "INDUSINDBK", "BPCL",
    "IOC", "TATAMOTORS", "HEROMOTOCO", "EICHERMOT", "BAJAJ-AUTO",
    "UPL", "SHREECEM", "TATACONSUM", "APOLLOHOSP", "PIDILITIND",
}

# ── Index symbols ──────────────────────────────────────────────────
INDEX_SYMBOLS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
    "NIFTY50", "NIFTYBANK", "SENSEX",
}

# ── MCX symbols ────────────────────────────────────────────────────
MCX_COMMODITIES = {
    "GOLD", "GOLDM", "SILVER", "SILVERM", "CRUDEOIL",
    "CRUDEOILM", "NATURALGAS", "COPPER", "ZINC", "ALUMINIUM",
}

# ── Session times (IST) ────────────────────────────────────────────
SESSIONS = {
    "ASIA":   {"start": "02:30", "end": "06:30"},
    "LONDON": {"start": "13:30", "end": "17:30"},
    "NY":     {"start": "18:30", "end": "23:30"},
    "INDIA":  {"start": "09:15", "end": "15:30"},
}

KILLZONE_MINUTES = 30  # ±30 min from each session open

# ── Timeframe modes ────────────────────────────────────────────────
TIMEFRAME_MODES = {
    "SCALP":     [1, 3, 5],
    "INTRADAY":  [15, 30, 60],
    "SWING":     [240, "D", "W"],
    "ALL":       [1, 3, 5, 15, 30, 60, 240, "D", "W"],
}

# ── Quality filters ────────────────────────────────────────────────
QUALITY_FILTERS = {
    "APLUS_ONLY":   {"min_score": 90, "max_per_day": 2},
    "A_AND_APLUS":  {"min_score": 72, "max_per_day": 5},
    "ALL_SCORED":   {"min_score": 55, "max_per_day": 8},
}

# ── Max SL by segment ─────────────────────────────────────────────
MAX_SL_PERCENT = {
    "INDICES":        1.5,
    "INDIAN_FNO":     2.0,
    "INDIAN_STOCKS":  2.0,
    "COMMODITY":      1.5,
    "CURRENCY_FOREX": 1.5,
    "CRYPTO":         1.5,
    "GLOBAL_INDICES": 1.5,
}

# ── NSE API endpoints ─────────────────────────────────────────────
NSE_BASE_URL = "https://www.nseindia.com"
NSE_OPTION_CHAIN_INDEX = "/api/option-chain-indices"
NSE_OPTION_CHAIN_EQUITY = "/api/option-chain-equities"
NSE_COOKIE_REFRESH_MINUTES = 30
NSE_OPTION_FETCH_INTERVAL_SECONDS = 180  # every 3 minutes

# ── Binance public API ────────────────────────────────────────────
BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_FUTURES_URL = "https://fapi.binance.com"

# ── Scheduling ────────────────────────────────────────────────────
DAILY_SUMMARY_INDIA_TIME = "15:45"   # 3:45 PM IST
DAILY_SUMMARY_GLOBAL_TIME = "23:30"  # 11:30 PM IST
WEEKLY_ANALYSIS_DAY = 6              # Sunday (0=Monday)
WEEKLY_ANALYSIS_TIME = "20:00"       # 8:00 PM IST
PRICE_CHECK_INTERVAL_SECONDS = 300   # every 5 minutes

# ── Consecutive loss protection ──────────────────────────────────
MAX_CONSECUTIVE_LOSSES = 3
CONSECUTIVE_LOSS_PAUSE_HOURS = 24

# ── Spread filter ─────────────────────────────────────────────────
MAX_SPREAD_PERCENT = 0.15

# ── Brokerage and charges by segment ─────────────────────────────
BROKERAGE_FLAT = 20.0       # ₹20 flat per order (F&O / Indian)
GST_RATE = 0.18             # 18% GST on brokerage + exchange + SEBI
SEBI_TURNOVER_CHARGE = 10   # ₹10 per crore
MIN_RR_RATIO = 2.0          # Minimum risk:reward ratio

CHARGES_CONFIG = {
    "INDICES": {
        "brokerage_per_order": 20.0,
        "stt_futures_sell": 0.000125,   # 0.0125% sell-side
        "stt_options_buy": 0.000625,    # 0.0625% of premium
        "nse_exchange": 0.000019,       # 0.0019%
        "stamp_buy": 0.00003,           # 0.003% buy-side
    },
    "INDIAN_FNO": {
        "brokerage_per_order": 20.0,
        "stt_futures_sell": 0.000125,
        "stt_options_buy": 0.000625,
        "nse_exchange": 0.000019,
        "stamp_buy": 0.00003,
    },
    "INDIAN_STOCKS": {
        "brokerage_per_order": 20.0,
        "stt_delivery": 0.001,
        "nse_exchange": 0.000019,
        "stamp_buy": 0.00015,
    },
    "COMMODITY": {
        "brokerage_per_order": 20.0,
        "mcx_exchange": 0.00003,
        "stamp_buy": 0.00003,
    },
    "CURRENCY_FOREX": {
        "brokerage_per_order": 20.0,
        "nse_exchange": 0.0000135,
        "stamp_buy": 0.00001,
    },
    "CRYPTO": {
        "taker_fee_per_side": 0.0004,   # 0.04% Binance
    },
    "GLOBAL_INDICES": {
        "commission_per_lot": 3.5,      # $3.5/lot
        "spread_percent": 0.0002,
    },
}

# Singleton instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Return cached settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def setup_logging() -> None:
    """Configure structured logging for Railway log capture."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Suppress noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

MARKET_LABELS = {
    "INDIAN_STOCKS": "Indian Stocks",
    "INDIAN_FNO": "Indian F&O",
    "INDICES": "Indices",
    "COMMODITY": "Commodity MCX",
    "CURRENCY_FOREX": "Currency Forex",
    "CRYPTO": "Crypto",
    "GLOBAL_INDICES": "Global Indices",
}
TF_LABELS = {
    "SCALP": "Scalp 1m-5m",
    "INTRADAY": "Intraday 15m-1H",
    "SWING": "Swing 4H-Daily",
    "ALL": "All Timeframes",
}
QF_LABELS = {
    "APLUS_ONLY": "Only A+ Setups max 2/day",
    "A_AND_APLUS": "A and A+ Setups max 5/day",
    "ALL_SCORED": "All Scored Setups up to 8/day",
}
# redeploy
