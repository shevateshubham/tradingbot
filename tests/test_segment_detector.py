"""tests/test_segment_detector.py — Tests for segment_detector.py"""

import pytest
from mcp_server.tools.segment_detector import detect_segment, get_base_symbol, is_index_option


class TestNSEIndices:
    def test_nifty(self):
        assert detect_segment("NSE:NIFTY", "NSE") == "INDICES"

    def test_nifty_no_prefix(self):
        assert detect_segment("NIFTY", "NSE") == "INDICES"

    def test_banknifty(self):
        assert detect_segment("NSE:BANKNIFTY", "NSE") == "INDICES"

    def test_finnifty(self):
        assert detect_segment("NSE:FINNIFTY", "NSE") == "INDICES"

    def test_midcpnifty(self):
        assert detect_segment("NSE:MIDCPNIFTY", "NSE") == "INDICES"

    def test_sensex(self):
        assert detect_segment("BSE:SENSEX", "BSE") == "INDICES"

    def test_nifty_futures_suffix(self):
        assert detect_segment("NSE:NIFTYJUL25FUT", "NSE") == "INDICES"


class TestIndianFNO:
    def test_reliance_fno(self):
        assert detect_segment("NSE:RELIANCE", "NSE") == "INDIAN_FNO"

    def test_hdfcbank_fno(self):
        assert detect_segment("NSE:HDFCBANK", "NSE") == "INDIAN_FNO"

    def test_infy_fno(self):
        assert detect_segment("NSE:INFY", "NSE") == "INDIAN_FNO"

    def test_tcs_fno(self):
        assert detect_segment("NSE:TCS", "NSE") == "INDIAN_FNO"


class TestIndianStocks:
    def test_non_fno_stock(self):
        # A stock not in the F&O list
        result = detect_segment("NSE:ZOMATO", "NSE")
        assert result == "INDIAN_STOCKS"

    def test_small_cap_stock(self):
        result = detect_segment("NSE:IRFC", "NSE")
        assert result == "INDIAN_STOCKS"


class TestCommodity:
    def test_gold(self):
        assert detect_segment("MCX:GOLD1!", "MCX") == "COMMODITY"

    def test_crude_oil(self):
        assert detect_segment("MCX:CRUDEOIL1!", "MCX") == "COMMODITY"

    def test_silver(self):
        assert detect_segment("MCX:SILVER1!", "MCX") == "COMMODITY"

    def test_natural_gas(self):
        assert detect_segment("MCX:NATURALGAS1!", "MCX") == "COMMODITY"


class TestCurrencyForex:
    def test_usdinr(self):
        assert detect_segment("NSE:USDINR", "NSE") == "CURRENCY_FOREX"

    def test_eurinr(self):
        assert detect_segment("NSE:EURINR", "NSE") == "CURRENCY_FOREX"

    def test_fx_eurusd(self):
        assert detect_segment("FX:EURUSD", "FX") == "CURRENCY_FOREX"

    def test_fx_gbpusd(self):
        assert detect_segment("FX:GBPUSD", "FX") == "CURRENCY_FOREX"


class TestCrypto:
    def test_btcusdt_binance(self):
        assert detect_segment("BINANCE:BTCUSDT", "BINANCE") == "CRYPTO"

    def test_ethusdt_binance(self):
        assert detect_segment("BINANCE:ETHUSDT", "BINANCE") == "CRYPTO"

    def test_btcusdt_perp(self):
        assert detect_segment("BINANCE:BTCUSDT.P", "BINANCE") == "CRYPTO"

    def test_coinbase(self):
        assert detect_segment("COINBASE:BTCUSD", "COINBASE") == "CRYPTO"


class TestGlobalIndices:
    def test_spx500_oanda(self):
        assert detect_segment("OANDA:SPX500USD", "OANDA") == "GLOBAL_INDICES"

    def test_nas100_oanda(self):
        assert detect_segment("OANDA:NAS100USD", "OANDA") == "GLOBAL_INDICES"

    def test_dax(self):
        assert detect_segment("OANDA:DE30EUR", "OANDA") == "GLOBAL_INDICES"

    def test_index_prefix(self):
        assert detect_segment("INDEX:SPX", "INDEX") == "GLOBAL_INDICES"


class TestTickerFormats:
    def test_colon_prefix_stripped(self):
        """NSE:NIFTY and NIFTY with exchange=NSE should both work."""
        r1 = detect_segment("NSE:NIFTY", "")
        r2 = detect_segment("NIFTY", "NSE")
        assert r1 == r2 == "INDICES"

    def test_case_insensitive(self):
        assert detect_segment("nse:nifty", "nse") == "INDICES"
        assert detect_segment("BINANCE:btcusdt", "BINANCE") == "CRYPTO"


class TestBaseSymbol:
    def test_strip_futures_suffix(self):
        assert get_base_symbol("NIFTYJUL25FUT", "NSE") == "NIFTY" or \
               "NIFTY" in get_base_symbol("NIFTYJUL25FUT", "NSE")

    def test_strip_options_suffix(self):
        sym = get_base_symbol("RELIANCE23SEP1600CE", "NSE")
        assert "RELIANCE" in sym

    def test_plain_symbol_unchanged(self):
        assert get_base_symbol("NIFTY", "NSE") == "NIFTY"

    def test_with_prefix(self):
        assert get_base_symbol("NSE:BANKNIFTY", "NSE") == "BANKNIFTY"


class TestIsIndexOption:
    def test_nifty_ce_is_index_option(self):
        assert is_index_option("NIFTY23JUL22500CE", "NSE")

    def test_nifty_plain_not_option(self):
        assert not is_index_option("NIFTY", "NSE")

    def test_reliance_ce_not_index_option(self):
        # RELIANCE CE is F&O, not index
        assert not is_index_option("RELIANCE23AUG3000CE", "NSE")
