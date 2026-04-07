"""tests/test_signal_filter.py — Tests for signal_filter.py"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from mcp_server.tools.signal_filter import apply_filters, _timeframe_allowed, FilterResult
from mcp_server.models.database import UserPreferences


def _mock_user(
    markets_enabled=None,
    timeframe_mode="INTRADAY",
    quality_filter="A_AND_APLUS",
    max_signals_per_day=5,
    signals_paused=False,
    paper_mode=True,
    pause_until=None,
    consecutive_losses=0,
) -> UserPreferences:
    user = MagicMock(spec=UserPreferences)
    user.user_id = 1
    user.chat_id = "123456789"
    user.markets_enabled = markets_enabled or ["INDICES", "INDIAN_FNO", "CRYPTO"]
    user.timeframe_mode = timeframe_mode
    user.quality_filter = quality_filter
    user.max_signals_per_day = max_signals_per_day
    user.signals_paused = signals_paused
    user.paper_mode = paper_mode
    user.pause_until = pause_until
    user.consecutive_losses = consecutive_losses
    return user


def _payload(
    segment="INDICES",
    direction="LONG",
    timeframe="15",
    score=85,
    grade="A",
) -> dict:
    return {
        "instrument": "NSE:NIFTY",
        "segment": segment,
        "direction": direction,
        "timeframe": timeframe,
        "score": score,
        "grade": grade,
    }


@pytest.mark.asyncio
async def test_filter_passes_all_checks():
    """A valid signal with matching preferences should pass all filters."""
    mock_session = AsyncMock()
    user = _mock_user(markets_enabled=["INDICES"])

    with patch("mcp_server.tools.signal_filter.get_or_create_user", return_value=user), \
         patch("mcp_server.tools.signal_filter.get_signals_sent_today", return_value=0), \
         patch("mcp_server.tools.signal_filter.count_open_trades", return_value=0), \
         patch("mcp_server.tools.signal_filter.get_recent_losses", return_value=0):

        result = await apply_filters(mock_session, "123456789", _payload(), score=85, grade="A")

    assert result.passed


@pytest.mark.asyncio
async def test_filter_rejects_paused():
    """Paused signals should be immediately discarded."""
    mock_session = AsyncMock()
    user = _mock_user(signals_paused=True, pause_until=None)

    with patch("mcp_server.tools.signal_filter.get_or_create_user", return_value=user):
        result = await apply_filters(mock_session, "123456789", _payload(), score=85, grade="A")

    assert not result.passed
    assert "signals_paused" in result.reason


@pytest.mark.asyncio
async def test_filter_rejects_disabled_segment():
    """Signal for a segment not in user's enabled markets should be silently discarded."""
    mock_session = AsyncMock()
    user = _mock_user(markets_enabled=["INDICES"])  # CRYPTO not enabled

    with patch("mcp_server.tools.signal_filter.get_or_create_user", return_value=user), \
         patch("mcp_server.tools.signal_filter.get_signals_sent_today", return_value=0), \
         patch("mcp_server.tools.signal_filter.count_open_trades", return_value=0), \
         patch("mcp_server.tools.signal_filter.get_recent_losses", return_value=0):

        result = await apply_filters(
            mock_session, "123456789",
            _payload(segment="CRYPTO"),
            score=85, grade="A",
        )

    assert not result.passed
    assert "segment_not_enabled" in result.reason


@pytest.mark.asyncio
async def test_filter_rejects_wrong_timeframe():
    """Signal on 1m timeframe for INTRADAY-only user should be discarded."""
    mock_session = AsyncMock()
    user = _mock_user(markets_enabled=["INDICES"], timeframe_mode="INTRADAY")

    with patch("mcp_server.tools.signal_filter.get_or_create_user", return_value=user), \
         patch("mcp_server.tools.signal_filter.get_signals_sent_today", return_value=0), \
         patch("mcp_server.tools.signal_filter.count_open_trades", return_value=0), \
         patch("mcp_server.tools.signal_filter.get_recent_losses", return_value=0):

        result = await apply_filters(
            mock_session, "123456789",
            _payload(timeframe="1"),  # Scalp timeframe
            score=85, grade="A",
        )

    assert not result.passed
    assert "timeframe_mismatch" in result.reason


@pytest.mark.asyncio
async def test_filter_rejects_low_score():
    """Score below quality filter threshold should be discarded."""
    mock_session = AsyncMock()
    user = _mock_user(markets_enabled=["INDICES"], quality_filter="A_AND_APLUS")

    with patch("mcp_server.tools.signal_filter.get_or_create_user", return_value=user), \
         patch("mcp_server.tools.signal_filter.get_signals_sent_today", return_value=0), \
         patch("mcp_server.tools.signal_filter.count_open_trades", return_value=0), \
         patch("mcp_server.tools.signal_filter.get_recent_losses", return_value=0):

        result = await apply_filters(
            mock_session, "123456789",
            _payload(score=60, grade="B"),  # Below A threshold (72)
            score=60, grade="B",
        )

    assert not result.passed
    assert "score_below_threshold" in result.reason


@pytest.mark.asyncio
async def test_filter_rejects_at_daily_limit():
    """When daily limit is reached, signal should be discarded + warning flag set."""
    mock_session = AsyncMock()
    user = _mock_user(markets_enabled=["INDICES"], max_signals_per_day=5)

    with patch("mcp_server.tools.signal_filter.get_or_create_user", return_value=user), \
         patch("mcp_server.tools.signal_filter.get_signals_sent_today", return_value=5), \
         patch("mcp_server.tools.signal_filter.count_open_trades", return_value=0), \
         patch("mcp_server.tools.signal_filter.get_recent_losses", return_value=0):

        result = await apply_filters(mock_session, "123456789", _payload(), score=85, grade="A")

    assert not result.passed
    assert "daily_limit_reached" in result.reason
    assert result.send_limit_warning


@pytest.mark.asyncio
async def test_filter_rejects_max_active_trades():
    """When max open trades reached, signal should be discarded."""
    mock_session = AsyncMock()
    user = _mock_user(markets_enabled=["INDICES"])

    with patch("mcp_server.tools.signal_filter.get_or_create_user", return_value=user), \
         patch("mcp_server.tools.signal_filter.get_signals_sent_today", return_value=0), \
         patch("mcp_server.tools.signal_filter.count_open_trades", return_value=3), \
         patch("mcp_server.tools.signal_filter.get_recent_losses", return_value=0), \
         patch("mcp_server.tools.signal_filter.get_settings") as mock_settings:

        mock_settings.return_value.max_active_trades = 3
        result = await apply_filters(mock_session, "123456789", _payload(), score=85, grade="A")

    assert not result.passed
    assert "max_active_trades" in result.reason


@pytest.mark.asyncio
async def test_filter_triggers_consecutive_loss_protection():
    """3 consecutive losses should pause signals and notify."""
    mock_session = AsyncMock()
    user = _mock_user(markets_enabled=["INDICES"], signals_paused=False)

    with patch("mcp_server.tools.signal_filter.get_or_create_user", return_value=user), \
         patch("mcp_server.tools.signal_filter.get_signals_sent_today", return_value=0), \
         patch("mcp_server.tools.signal_filter.count_open_trades", return_value=0), \
         patch("mcp_server.tools.signal_filter.get_recent_losses", return_value=3):

        result = await apply_filters(mock_session, "123456789", _payload(), score=85, grade="A")

    assert not result.passed
    assert "consecutive_loss_protection" in result.reason


class TestTimeframeAllowed:
    def test_scalp_allows_1m(self):
        assert _timeframe_allowed("1", "SCALP")

    def test_scalp_allows_5m(self):
        assert _timeframe_allowed("5", "SCALP")

    def test_scalp_blocks_15m(self):
        assert not _timeframe_allowed("15", "SCALP")

    def test_intraday_allows_15m(self):
        assert _timeframe_allowed("15", "INTRADAY")

    def test_intraday_allows_60m(self):
        assert _timeframe_allowed("60", "INTRADAY")

    def test_intraday_blocks_1m(self):
        assert not _timeframe_allowed("1", "INTRADAY")

    def test_intraday_blocks_daily(self):
        assert not _timeframe_allowed("D", "INTRADAY")

    def test_swing_allows_240(self):
        assert _timeframe_allowed("240", "SWING")

    def test_swing_allows_daily(self):
        assert _timeframe_allowed("D", "SWING")

    def test_all_allows_everything(self):
        for tf in ["1", "5", "15", "60", "240", "D"]:
            assert _timeframe_allowed(tf, "ALL")

    def test_1h_normalized(self):
        assert _timeframe_allowed("1H", "INTRADAY")

    def test_4h_normalized(self):
        assert _timeframe_allowed("4H", "SWING")
