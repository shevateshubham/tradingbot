"""tests/conftest.py — Shared pytest fixtures and async configuration."""

import os
import pytest

# Set test environment variables before importing any app modules
os.environ.setdefault("TELEGRAM_BOT_TOKEN",       "test_token_placeholder")
os.environ.setdefault("TELEGRAM_CHAT_ID",          "123456789")
os.environ.setdefault("ANTHROPIC_API_KEY",         "test_anthropic_key")
os.environ.setdefault("TV_WEBHOOK_SECRET",         "test_secret")
os.environ.setdefault("DATABASE_URL",              "postgresql+asyncpg://testuser:testpass@localhost:5432/testdb")
os.environ.setdefault("ACCOUNT_SIZE",              "100000")
os.environ.setdefault("RISK_PER_TRADE_PERCENT",    "1.0")
os.environ.setdefault("MIN_CONFLUENCE_SCORE",      "72")
os.environ.setdefault("MAX_ACTIVE_TRADES",         "3")
os.environ.setdefault("MIN_RR_RATIO",              "2.0")
os.environ.setdefault("PAPER_MODE",                "true")
os.environ.setdefault("GITHUB_TOKEN",              "test_github_token")
os.environ.setdefault("GITHUB_USERNAME",           "testuser")
os.environ.setdefault("GITHUB_REPO",               "smc-trading-bot")
os.environ.setdefault("RAILWAY_API_TOKEN",         "test_railway_token")
os.environ.setdefault("RAILWAY_SERVICE_ID",        "test_service_id")
os.environ.setdefault("RAILWAY_PROJECT_ID",        "test_project_id")


# Configure pytest-asyncio
pytest_plugins = ["pytest_asyncio"]


@pytest.fixture
def sample_nse_chain():
    """Realistic NSE option chain data for testing."""
    def make_strike(strike, ce_oi, pe_oi, ce_iv=20.0, pe_iv=20.0):
        return {
            "strikePrice": strike,
            "CE": {
                "openInterest": ce_oi,
                "changeinOpenInterest": int(ce_oi * 0.1),
                "lastPrice": max(1.0, (strike - 22500) * -0.1 + 50),
                "impliedVolatility": ce_iv,
            },
            "PE": {
                "openInterest": pe_oi,
                "changeinOpenInterest": int(pe_oi * 0.08),
                "lastPrice": max(1.0, (22500 - strike) * -0.1 + 50),
                "impliedVolatility": pe_iv,
            },
        }

    return [
        make_strike(22000, 50000,  120000, pe_iv=23.0, ce_iv=16.0),
        make_strike(22200, 80000,  90000,  pe_iv=21.0, ce_iv=17.0),
        make_strike(22400, 150000, 70000,  pe_iv=19.5, ce_iv=18.5),
        make_strike(22500, 200000, 200000, pe_iv=19.0, ce_iv=19.0),  # ATM
        make_strike(22600, 180000, 60000,  pe_iv=18.0, ce_iv=19.5),
        make_strike(22800, 90000,  40000,  pe_iv=17.0, ce_iv=20.5),
        make_strike(23000, 50000,  25000,  pe_iv=16.0, ce_iv=22.0),
    ]


@pytest.fixture
def valid_webhook_body():
    """Valid TradingView webhook payload."""
    return {
        "secret":          "test_secret",
        "instrument":      "NSE:NIFTY",
        "exchange":        "NSE",
        "timeframe":       "15",
        "signal_type":     "OB_ENTRY",
        "direction":       "LONG",
        "structure":       "CHOCH",
        "ob_high":         22600.0,
        "ob_low":          22500.0,
        "ob_mid":          22550.0,
        "fvg_present":     True,
        "liquidity_swept": True,
        "session":         "LONDON",
        "htf_bias":        "BULLISH",
        "in_discount":     True,
        "trap_present":    False,
        "volume_ratio":    1.8,
        "close":           22580.0,
        "timestamp":       "2024-01-15T10:30:00Z",
    }


@pytest.fixture
def high_score_enriched():
    """Fully enriched payload that should score A+."""
    return {
        "instrument":            "NSE:NIFTY",
        "base_symbol":           "NIFTY",
        "segment":               "INDICES",
        "direction":             "LONG",
        "timeframe":             "15",
        "signal_type":           "OB_ENTRY",
        "structure":             "CHOCH",
        "ob_high":               22600.0,
        "ob_low":                22500.0,
        "ob_mid":                22550.0,
        "fvg_present":           True,
        "fvg_high":              22570.0,
        "fvg_low":               22520.0,
        "liquidity_swept":       True,
        "session":               "LONDON",
        "htf_bias":              "BULLISH",
        "in_discount":           True,
        "trap_present":          False,
        "trap_direction":        "",
        "volume_ratio":          2.1,
        "close":                 22580.0,
        "htf_matches_direction": True,
        "options_confirm":       True,
        "near_max_pain":         True,
        "is_killzone":           True,
        "ltf_choch":             True,
        "in_lunch":              False,
        "ob_already_touched":    False,
        "options_pcr":           1.35,
        "options_oi_bias":       "BULLISH",
        "max_pain":              22510.0,
        "gex":                   125000000.0,
        "ce_walls":              [22700.0, 22800.0, 23000.0],
        "pe_walls":              [22400.0, 22300.0, 22000.0],
        "setup_type":            "Bullish OB + FVG",
        "zone_type":             "Discount (below equilibrium)",
        "narrative":             "Price reacting to bullish OB in discount zone.",
        "confluences": {
            "htf_bias":        "BULLISH",
            "poi_type":        "Bullish OB + FVG",
            "zone_type":       "Discount",
            "liquidity_swept": True,
            "killzone":        True,
            "ltf_choch":       True,
        },
    }
