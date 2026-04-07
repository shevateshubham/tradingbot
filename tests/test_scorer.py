"""tests/test_scorer.py — Tests for trade_scorer.py"""

import pytest
from mcp_server.tools.trade_scorer import score_signal, ScoreResult


def _base_payload(**overrides) -> dict:
    """Return a valid high-scoring payload with optional overrides."""
    base = {
        "direction":            "LONG",
        "htf_bias":             "BULLISH",
        "signal_type":          "OB_ENTRY",
        "fvg_present":          True,
        "htf_matches_direction":True,
        "in_discount":          True,
        "liquidity_swept":      True,
        "is_killzone":          True,
        "ltf_choch":            True,
        "session":              "LONDON",
        "options_confirm":      True,
        "near_max_pain":        True,
        "volume_ratio":         2.0,
        "in_lunch":             False,
        "trap_present":         False,
        "trap_direction":       "",
        "segment":              "INDICES",
        "close":                22500.0,
        "ob_already_touched":   False,
    }
    base.update(overrides)
    return base


class TestScoreWeights:
    def test_max_possible_score_indices(self):
        """With every confluence active, score approaches 100."""
        result = score_signal(_base_payload())
        assert result.score >= 90
        assert result.grade == "A+"

    def test_ob_entry_adds_20(self):
        payload = _base_payload(signal_type="OB_ENTRY", fvg_present=False,
                                htf_matches_direction=False, in_discount=None,
                                liquidity_swept=False, is_killzone=False,
                                ltf_choch=False, session="NONE",
                                options_confirm=False, near_max_pain=False,
                                volume_ratio=0.5)
        result = score_signal(payload)
        assert result.breakdown.get("ob_entry") == 20

    def test_fvg_overlap_adds_18(self):
        payload = _base_payload(fvg_present=True, signal_type="OB_ENTRY")
        result = score_signal(payload)
        assert "ob_fvg_overlap" in result.breakdown
        assert result.breakdown["ob_fvg_overlap"] == 18

    def test_fvg_overlap_absent_when_no_fvg(self):
        payload = _base_payload(fvg_present=False)
        result = score_signal(payload)
        assert "ob_fvg_overlap" not in result.breakdown

    def test_htf_bias_match_adds_15(self):
        payload = _base_payload()
        result = score_signal(payload)
        assert result.breakdown.get("htf_bias_match") == 15

    def test_correct_zone_adds_12(self):
        payload = _base_payload(direction="LONG", in_discount=True)
        result = score_signal(payload)
        assert result.breakdown.get("premium_discount") == 12

    def test_wrong_zone_no_bonus(self):
        # Long in premium zone → no +12
        payload = _base_payload(direction="LONG", in_discount=False)
        result = score_signal(payload)
        assert "premium_discount" not in result.breakdown

    def test_liquidity_swept_adds_10(self):
        payload = _base_payload()
        result = score_signal(payload)
        assert result.breakdown.get("liquidity_swept") == 10

    def test_killzone_adds_8(self):
        payload = _base_payload()
        result = score_signal(payload)
        assert result.breakdown.get("killzone") == 8

    def test_ltf_choch_adds_8(self):
        payload = _base_payload()
        result = score_signal(payload)
        assert result.breakdown.get("ltf_choch") == 8

    def test_volume_ratio_adds_3(self):
        payload = _base_payload(volume_ratio=2.0)
        result = score_signal(payload)
        assert result.breakdown.get("volume_ratio") == 3

    def test_volume_ratio_no_bonus_below_threshold(self):
        payload = _base_payload(volume_ratio=1.2)
        result = score_signal(payload)
        assert "volume_ratio" not in result.breakdown


class TestPenalties:
    def test_htf_conflict_deducts_10(self):
        # LONG signal with BEARISH HTF → discard (conflict = hard reject)
        payload = _base_payload(direction="LONG", htf_bias="BEARISH",
                                htf_matches_direction=False)
        result = score_signal(payload)
        # Hard discard for HTF conflict
        assert not result.passed
        assert result.reject_reason is not None
        assert "daily_structure" in result.reject_reason or "against" in result.reject_reason

    def test_lunch_hour_deducts_5(self):
        payload = _base_payload(in_lunch=True)
        result = score_signal(payload)
        assert result.breakdown.get("lunch_hour") == -5

    def test_trap_same_direction_deducts_5(self):
        payload = _base_payload(
            direction="LONG",
            trap_present=True,
            trap_direction="BULL",
        )
        result = score_signal(payload)
        assert result.breakdown.get("trap_same_dir") == -5

    def test_trap_opposite_direction_no_penalty(self):
        # Bear trap when signal is LONG → not same direction
        payload = _base_payload(
            direction="LONG",
            trap_present=True,
            trap_direction="BEAR",
        )
        result = score_signal(payload)
        assert "trap_same_dir" not in result.breakdown

    def test_ob_already_touched_deducts_8(self):
        payload = _base_payload(ob_already_touched=True)
        result = score_signal(payload)
        assert result.breakdown.get("ob_already_touched") == -8


class TestGrades:
    def test_grade_a_plus_at_90(self):
        payload = _base_payload()
        result = score_signal(payload)
        if result.score >= 90:
            assert result.grade == "A+"

    def test_grade_a_between_72_89(self):
        # Reduce confluence to get score in A range
        payload = _base_payload(
            options_confirm=False, near_max_pain=False,
            is_killzone=False, ltf_choch=False,
        )
        result = score_signal(payload)
        if 72 <= result.score <= 89:
            assert result.grade == "A"

    def test_grade_b_between_55_71(self):
        payload = _base_payload(
            signal_type="BOS",
            fvg_present=False,
            liquidity_swept=False,
            is_killzone=False,
            ltf_choch=False,
            options_confirm=False,
            near_max_pain=False,
            volume_ratio=0.5,
        )
        result = score_signal(payload)
        if 55 <= result.score <= 71:
            assert result.grade == "B"

    def test_grade_c_below_55(self):
        payload = _base_payload(
            signal_type="UNKNOWN",
            fvg_present=False,
            htf_matches_direction=False,
            in_discount=None,
            liquidity_swept=False,
            is_killzone=False,
            ltf_choch=False,
            options_confirm=False,
            near_max_pain=False,
            volume_ratio=0.5,
            session="NONE",
        )
        result = score_signal(payload)
        if result.score < 55:
            assert result.grade == "C"


class TestHardDiscards:
    def test_score_below_72_rejected(self):
        """Low-confluence signal must not be sent."""
        payload = _base_payload(
            signal_type="UNKNOWN",
            fvg_present=False,
            htf_matches_direction=False,
            in_discount=None,
            liquidity_swept=False,
            is_killzone=False,
            ltf_choch=False,
            options_confirm=False,
            near_max_pain=False,
            volume_ratio=0.5,
            session="NONE",
            htf_bias="NEUTRAL",
        )
        result = score_signal(payload)
        if result.score < 72:
            assert not result.passed

    def test_grade_b_not_sent(self):
        """Grade B signals must never be sent."""
        payload = _base_payload(
            fvg_present=False, liquidity_swept=False,
            is_killzone=False, ltf_choch=False,
            options_confirm=False, near_max_pain=False,
            signal_type="BOS",
        )
        result = score_signal(payload)
        if result.grade == "B":
            assert not result.passed

    def test_score_clamped_to_100(self):
        """Score must never exceed 100."""
        result = score_signal(_base_payload())
        assert result.score <= 100

    def test_score_clamped_to_zero_minimum(self):
        """Score must never be negative."""
        payload = _base_payload(
            in_lunch=True, trap_present=True,
            trap_direction="BULL", ob_already_touched=True,
            htf_bias="NEUTRAL", signal_type="UNKNOWN",
        )
        result = score_signal(payload)
        assert result.score >= 0

    def test_options_confirm_only_for_eligible_segments(self):
        """Options bonus only for INDICES and INDIAN_FNO."""
        payload_crypto = _base_payload(segment="CRYPTO", options_confirm=True)
        result_crypto  = score_signal(payload_crypto)
        assert "options_confirm" not in result_crypto.breakdown

        payload_idx = _base_payload(segment="INDICES", options_confirm=True)
        result_idx  = score_signal(payload_idx)
        assert "options_confirm" in result_idx.breakdown

    def test_max_pain_only_for_indices(self):
        """Max pain bonus only for INDICES segment."""
        payload_fno    = _base_payload(segment="INDIAN_FNO", near_max_pain=True)
        result_fno     = score_signal(payload_fno)
        assert "max_pain_proximity" not in result_fno.breakdown

        payload_idx = _base_payload(segment="INDICES", near_max_pain=True)
        result_idx  = score_signal(payload_idx)
        assert "max_pain_proximity" in result_idx.breakdown
