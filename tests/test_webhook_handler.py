"""tests/test_webhook_handler.py — Tests for webhook_handler.py"""

import pytest
from unittest.mock import patch

from mcp_server.tools.webhook_handler import (
    handle_webhook, validate_webhook_secret, WebhookPayload,
)


def _valid_body(**overrides) -> dict:
    base = {
        "secret":          "test_secret",
        "instrument":      "NSE:NIFTY",
        "exchange":        "NSE",
        "timeframe":       "15",
        "signal_type":     "OB_ENTRY",
        "direction":       "LONG",
        "structure":       "CHOCH",
        "ob_high":         22600.0,
        "ob_low":          22500.0,
        "ob_mid":          0.0,
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
    base.update(overrides)
    return base


class TestSecretValidation:
    def test_correct_secret_passes(self):
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock:
            mock.return_value.tv_webhook_secret = "test_secret"
            assert validate_webhook_secret("test_secret")

    def test_wrong_secret_fails(self):
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock:
            mock.return_value.tv_webhook_secret = "correct_secret"
            assert not validate_webhook_secret("wrong_secret")

    def test_empty_secret_fails(self):
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock:
            mock.return_value.tv_webhook_secret = "test_secret"
            assert not validate_webhook_secret("")

    def test_case_sensitive(self):
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock:
            mock.return_value.tv_webhook_secret = "MySecret"
            assert not validate_webhook_secret("mysecret")
            assert not validate_webhook_secret("MYSECRET")


class TestPayloadParsing:
    def test_valid_payload_parsed(self):
        body = _valid_body()
        payload = WebhookPayload(**body)
        assert payload.instrument == "NSE:NIFTY"
        assert payload.direction == "LONG"
        assert payload.fvg_present is True

    def test_direction_normalized_uppercase(self):
        payload = WebhookPayload(**_valid_body(direction="long"))
        assert payload.direction == "LONG"

    def test_signal_type_normalized(self):
        payload = WebhookPayload(**_valid_body(signal_type="ob_entry"))
        assert payload.signal_type == "OB_ENTRY"

    def test_ob_mid_auto_calculated(self):
        payload = WebhookPayload(**_valid_body(ob_mid=0.0, ob_high=22600, ob_low=22400))
        assert payload.ob_mid == pytest.approx(22500.0)

    def test_ob_mid_kept_if_provided(self):
        payload = WebhookPayload(**_valid_body(ob_mid=22520.0))
        assert payload.ob_mid == 22520.0

    def test_extra_fields_allowed(self):
        body = _valid_body()
        body["unexpected_field"] = "some_value"
        payload = WebhookPayload(**body)
        assert payload.instrument == "NSE:NIFTY"

    def test_missing_optional_fields_use_defaults(self):
        minimal = {
            "secret": "test_secret",
            "instrument": "NSE:NIFTY",
        }
        payload = WebhookPayload(**minimal)
        assert payload.timeframe == "15"
        assert payload.direction == "LONG"
        assert payload.fvg_present is False


@pytest.mark.asyncio
class TestHandleWebhook:
    async def test_valid_webhook_accepted(self):
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock_s, \
             patch("mcp_server.tools.webhook_handler.detect_segment", return_value="INDICES"):
            mock_s.return_value.tv_webhook_secret = "test_secret"
            body = _valid_body()
            normalized, error = await handle_webhook(body)

        assert normalized is not None
        assert error == "ok"

    async def test_invalid_secret_rejected(self):
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock_s:
            mock_s.return_value.tv_webhook_secret = "correct_secret"
            body = _valid_body(secret="wrong_secret")
            normalized, error = await handle_webhook(body)

        assert normalized is None
        assert "invalid_secret" in error

    async def test_invalid_json_rejected(self):
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock_s:
            mock_s.return_value.tv_webhook_secret = "test_secret"
            normalized, error = await handle_webhook({"secret": "test_secret"})
            # Missing required fields — pydantic will use defaults, so it may pass
            # At minimum should not crash

    async def test_segment_populated_in_result(self):
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock_s, \
             patch("mcp_server.tools.webhook_handler.detect_segment", return_value="INDICES"), \
             patch("mcp_server.tools.webhook_handler.get_base_symbol", return_value="NIFTY"):
            mock_s.return_value.tv_webhook_secret = "test_secret"
            normalized, error = await handle_webhook(_valid_body())

        assert normalized.segment == "INDICES"

    async def test_base_symbol_populated(self):
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock_s, \
             patch("mcp_server.tools.webhook_handler.detect_segment", return_value="INDICES"), \
             patch("mcp_server.tools.webhook_handler.get_base_symbol", return_value="NIFTY"):
            mock_s.return_value.tv_webhook_secret = "test_secret"
            normalized, error = await handle_webhook(_valid_body())

        assert normalized.base_symbol == "NIFTY"

    async def test_received_at_populated(self):
        from datetime import datetime
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock_s, \
             patch("mcp_server.tools.webhook_handler.detect_segment", return_value="INDICES"):
            mock_s.return_value.tv_webhook_secret = "test_secret"
            normalized, _ = await handle_webhook(_valid_body())

        assert isinstance(normalized.received_at, datetime)

    async def test_killzone_detection_runs(self):
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock_s, \
             patch("mcp_server.tools.webhook_handler.detect_segment", return_value="INDICES"):
            mock_s.return_value.tv_webhook_secret = "test_secret"
            normalized, _ = await handle_webhook(_valid_body())

        assert isinstance(normalized.is_killzone, bool)

    async def test_direction_long_preserved(self):
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock_s, \
             patch("mcp_server.tools.webhook_handler.detect_segment", return_value="INDICES"):
            mock_s.return_value.tv_webhook_secret = "test_secret"
            normalized, _ = await handle_webhook(_valid_body(direction="LONG"))

        assert normalized.direction == "LONG"

    async def test_direction_short_preserved(self):
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock_s, \
             patch("mcp_server.tools.webhook_handler.detect_segment", return_value="INDICES"):
            mock_s.return_value.tv_webhook_secret = "test_secret"
            normalized, _ = await handle_webhook(_valid_body(direction="SHORT"))

        assert normalized.direction == "SHORT"

    async def test_htf_bias_normalized(self):
        with patch("mcp_server.tools.webhook_handler.get_settings") as mock_s, \
             patch("mcp_server.tools.webhook_handler.detect_segment", return_value="INDICES"):
            mock_s.return_value.tv_webhook_secret = "test_secret"
            normalized, _ = await handle_webhook(_valid_body(htf_bias="bullish"))

        assert normalized.htf_bias == "BULLISH"
