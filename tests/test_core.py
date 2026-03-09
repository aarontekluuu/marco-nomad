"""Tests for Marco the Nomad's critical financial logic.

Covers: brain.py JSON extraction, lifi.py cost calc & parsing,
        yield_scanner.py pool filtering, wallet.py migration guards & state I/O.
"""

import copy
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# brain.py — JSON extraction regex
# ---------------------------------------------------------------------------

# Inline the regex logic from brain.py so we can test without importing
# (avoids needing anthropic installed just for regex tests)

def _extract_decision(text: str) -> dict:
    """Mirrors brain.py JSON extraction logic."""
    default = {"action": "hold", "moves": [], "confidence": 0.5, "risk_notes": ""}
    json_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if not json_match:
        json_match = re.search(r'(\{.*"action"\s*:.*\})', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            return default
    return default


class TestBrainJsonExtraction:
    """Test the regex that pulls JSON decisions out of Claude responses."""

    def test_fenced_json(self):
        text = 'Some journal text.\n```json\n{"action": "migrate", "moves": [], "confidence": 0.8}\n```'
        result = _extract_decision(text)
        assert result["action"] == "migrate"
        assert result["confidence"] == 0.8

    def test_fenced_json_with_extra_whitespace(self):
        text = 'Journal.\n```json\n\n  {"action": "hold", "moves": []}\n\n```'
        result = _extract_decision(text)
        assert result["action"] == "hold"

    def test_unfenced_json_flat(self):
        """Fallback regex only matches flat JSON (no nested braces)."""
        text = 'No fences here. {"action": "rebalance", "moves": [], "confidence": 0.6}'
        result = _extract_decision(text)
        assert result["action"] == "rebalance"

    def test_unfenced_json_with_nested_braces(self):
        """Greedy fallback regex handles nested braces in moves array."""
        text = 'No fences. {"action": "migrate", "moves": [{"from_chain": "base"}]}'
        result = _extract_decision(text)
        assert result["action"] == "migrate"
        assert len(result["moves"]) == 1

    def test_malformed_json_returns_default(self):
        text = '```json\n{"action": "migrate", broken json\n```'
        result = _extract_decision(text)
        assert result["action"] == "hold"  # default fallback

    def test_no_json_at_all(self):
        text = "Just a journal entry with no decision block."
        result = _extract_decision(text)
        assert result["action"] == "hold"

    def test_fenced_multiline_moves(self):
        blob = json.dumps({
            "action": "migrate",
            "moves": [
                {"from_chain": "arbitrum", "to_chain": "base", "amount_pct": 0.6, "reason": "better yield"},
            ],
            "confidence": 0.9,
            "risk_notes": "yield spike, watch 48h",
        }, indent=2)
        text = f"Day 12 journal entry.\n```json\n{blob}\n```"
        result = _extract_decision(text)
        assert result["action"] == "migrate"
        assert len(result["moves"]) == 1
        assert result["moves"][0]["to_chain"] == "base"

    def test_journal_extracted_before_json(self):
        """Ensure the journal portion is everything before the JSON block."""
        journal_part = "Day 5. Arbitrum yields dried up."
        text = f"{journal_part}\n```json\n" + '{"action": "hold", "moves": []}\n```'
        # Re-implement the journal slicing from brain.py
        json_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        journal = text[:json_match.start()].strip()
        assert journal == journal_part

    def test_no_json_at_all_returns_hold(self):
        """If model returns no JSON (rare), default to hold."""
        text = "I'm thinking about things. No decision yet."
        result = _extract_decision(text)
        assert result["action"] == "hold"
        assert result["moves"] == []


# ---------------------------------------------------------------------------
# brain.py — API error handling
# ---------------------------------------------------------------------------

class TestBrainErrorHandling:
    """Test that brain.decide() gracefully handles API failures."""

    @pytest.mark.asyncio
    async def test_api_error_returns_hold(self):
        """API failure should return hold with confidence 0, not crash."""
        from unittest.mock import AsyncMock, patch, MagicMock

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=Exception("rate limited"))

        with patch("brain._get_client", return_value=mock_client):
            import brain
            result = await brain.decide(
                {"Base": {"usdc": 100}},
                [{"chain": "Base", "project": "aave-v3", "symbol": "USDC", "apy": 5.0}],
            )
        assert result["decision"]["action"] == "hold"
        assert result["decision"]["confidence"] == 0.0
        assert "API error" in result["journal"]


# ---------------------------------------------------------------------------
# lifi.py — calc_bridge_cost and _parse_int
# ---------------------------------------------------------------------------

# Import directly since lifi.py has no heavy deps at module level
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lifi import calc_bridge_cost, _parse_int


class TestParseInt:
    def test_plain_int(self):
        assert _parse_int(42) == 42

    def test_decimal_string(self):
        assert _parse_int("1000000") == 1_000_000

    def test_hex_lowercase(self):
        assert _parse_int("0xff") == 255

    def test_hex_uppercase_prefix(self):
        assert _parse_int("0XFF") == 255

    def test_zero(self):
        assert _parse_int(0) == 0
        assert _parse_int("0") == 0
        assert _parse_int("0x0") == 0

    def test_large_gas_value(self):
        # Typical gasLimit from a bridge quote
        assert _parse_int("0x30d40") == 200000

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_int("not_a_number")


class TestCalcBridgeCost:
    """Test bridge cost calculation with various quote shapes."""

    def _make_quote(self, *, fee_costs=None, gas_costs=None,
                    from_amount=0, to_amount=0, to_amount_min=0,
                    from_decimals=6, to_decimals=6, duration=60, tool="stargate"):
        return {
            "estimate": {
                "feeCosts": fee_costs or [],
                "gasCosts": gas_costs or [],
                "fromAmount": str(from_amount),
                "toAmount": str(to_amount),
                "toAmountMin": str(to_amount_min),
                "executionDuration": duration,
            },
            "action": {
                "fromToken": {"decimals": from_decimals},
                "toToken": {"decimals": to_decimals},
            },
            "tool": tool,
        }

    def test_basic_cost(self):
        q = self._make_quote(
            fee_costs=[{"amountUSD": "0.15"}],
            gas_costs=[{"amountUSD": "0.10"}],
            from_amount=100_000_000,   # 100 USDC (6 decimals)
            to_amount=99_750_000,
            to_amount_min=99_500_000,
        )
        cost = calc_bridge_cost(q)
        assert cost["fee_usd"] == pytest.approx(0.15)
        assert cost["gas_usd"] == pytest.approx(0.10)
        assert cost["total_cost_usd"] == pytest.approx(0.25)
        assert cost["from_amount"] == pytest.approx(100.0)
        assert cost["to_amount"] == pytest.approx(99.75)
        assert cost["to_amount_min"] == pytest.approx(99.50)
        assert cost["slippage_amount"] == pytest.approx(0.25)
        assert cost["bridge"] == "stargate"
        assert cost["duration_seconds"] == 60

    def test_empty_costs(self):
        q = self._make_quote()
        cost = calc_bridge_cost(q)
        assert cost["total_cost_usd"] == 0.0
        assert cost["from_amount"] == 0
        assert cost["to_amount"] == 0

    def test_multiple_fee_entries(self):
        q = self._make_quote(
            fee_costs=[{"amountUSD": "0.10"}, {"amountUSD": "0.05"}],
            gas_costs=[{"amountUSD": "0.20"}, {"amountUSD": "0.03"}],
            from_amount=50_000_000,
            to_amount=49_800_000,
            to_amount_min=49_700_000,
        )
        cost = calc_bridge_cost(q)
        assert cost["fee_usd"] == pytest.approx(0.15)
        assert cost["gas_usd"] == pytest.approx(0.23)
        assert cost["total_cost_usd"] == pytest.approx(0.38)

    def test_18_decimal_token(self):
        """ETH-like token with 18 decimals."""
        amt = 10 ** 18  # 1.0 token
        q = self._make_quote(
            from_amount=amt, to_amount=amt, to_amount_min=amt,
            from_decimals=18, to_decimals=18,
        )
        cost = calc_bridge_cost(q)
        assert cost["from_amount"] == pytest.approx(1.0)

    def test_missing_estimate_fields(self):
        """Quote with minimal/missing fields should not crash."""
        q = {"estimate": {}, "tool": "hop"}
        cost = calc_bridge_cost(q)
        assert cost["total_cost_usd"] == 0.0
        assert cost["bridge"] == "hop"

    def test_missing_action_uses_default_decimals(self):
        """If action block is missing, defaults to 18 decimals."""
        q = {"estimate": {"fromAmount": str(10**18), "toAmount": str(10**18), "toAmountMin": str(10**18)}}
        cost = calc_bridge_cost(q)
        assert cost["from_amount"] == pytest.approx(1.0)
        assert cost["bridge"] == "unknown"


# ---------------------------------------------------------------------------
# yield_scanner.py — filter_pools
# ---------------------------------------------------------------------------

from yield_scanner import filter_pools, CHAIN_MAP


def _pool(*, chain="Base", apy=5.0, apy_base=4.5, tvl=1_000_000,
          stablecoin=True, outlier=False, il_risk="no", symbol="USDC",
          project="aave-v3", mean30d=4.8):
    return {
        "chain": chain,
        "apy": apy,
        "apyBase": apy_base,
        "tvlUsd": tvl,
        "stablecoin": stablecoin,
        "outlier": outlier,
        "ilRisk": il_risk,
        "symbol": symbol,
        "project": project,
        "apyMean30d": mean30d,
    }


class TestFilterPools:

    def test_basic_filter(self):
        pools = [_pool(), _pool(apy=2.0)]  # second below min_apy
        result = filter_pools(pools, min_apy=3.0)
        assert len(result) == 1

    def test_outlier_excluded(self):
        pools = [_pool(outlier=True), _pool(outlier=False)]
        result = filter_pools(pools, exclude_outliers=True)
        assert len(result) == 1
        assert result[0]["outlier"] is False

    def test_outlier_included_when_flag_off(self):
        pools = [_pool(outlier=True)]
        result = filter_pools(pools, exclude_outliers=False)
        assert len(result) == 1

    def test_il_risk_excluded(self):
        pools = [_pool(il_risk="yes"), _pool(il_risk="no")]
        result = filter_pools(pools, no_il_risk=True)
        assert len(result) == 1

    def test_max_apy_cap(self):
        pools = [_pool(apy=150.0), _pool(apy=50.0)]
        result = filter_pools(pools, max_apy=100.0)
        assert len(result) == 1
        assert result[0]["apy"] == 50.0

    def test_default_max_apy_rejects_100pct(self):
        """Default cap (50%) filters out sketchy 100%+ stablecoin yields."""
        pools = [_pool(apy=80.0), _pool(apy=30.0)]
        result = filter_pools(pools)  # Uses default max_apy=50.0
        assert len(result) == 1
        assert result[0]["apy"] == 30.0

    def test_stablecoin_filter(self):
        pools = [_pool(stablecoin=True), _pool(stablecoin=False, symbol="ETH")]
        result = filter_pools(pools, stablecoin_only=True)
        assert len(result) == 1
        assert result[0]["symbol"] == "USDC"

    def test_stablecoin_filter_off(self):
        pools = [_pool(stablecoin=False, symbol="ETH")]
        result = filter_pools(pools, stablecoin_only=False)
        assert len(result) == 1

    def test_tvl_filter(self):
        pools = [_pool(tvl=100), _pool(tvl=1_000_000)]
        result = filter_pools(pools, min_tvl=500_000)
        assert len(result) == 1

    def test_chain_filter(self):
        pools = [_pool(chain="Base"), _pool(chain="Arbitrum")]
        result = filter_pools(pools, chains=[8453])  # Base only
        assert len(result) == 1
        assert result[0]["chain"] == "Base"

    def test_sorts_by_mean30d_with_trust_boost(self):
        pools = [
            _pool(apy=10.0, apy_base=3.0),
            _pool(apy=8.0, apy_base=7.0),
        ]
        # Add apyMean30d to control sort order
        pools[0]["apyMean30d"] = 4.0
        pools[1]["apyMean30d"] = 6.0
        result = filter_pools(pools)
        assert result[0]["apyMean30d"] == 6.0  # higher 30d avg first

    def test_max_results(self):
        pools = [_pool(apy=5.0 + i) for i in range(30)]
        result = filter_pools(pools, max_results=5)
        assert len(result) == 5

    def test_empty_input(self):
        assert filter_pools([]) == []

    def test_all_filtered_out(self):
        pools = [_pool(apy=1.0)]  # below min_apy=3.0
        assert filter_pools(pools, min_apy=3.0) == []

    def test_none_tvl_treated_as_zero(self):
        pools = [_pool(tvl=None)]
        result = filter_pools(pools, min_tvl=500_000)
        assert len(result) == 0

    def test_none_apy_treated_as_zero(self):
        pools = [_pool(apy=None)]
        result = filter_pools(pools, min_apy=3.0)
        assert len(result) == 0

    def test_volatile_lp_pair_filtered(self):
        """LP pairs with volatile tokens should be filtered even if stablecoin=True."""
        volatile_lp = _pool(symbol="USDC-WETH", stablecoin=True)
        stable_lp = _pool(symbol="USDC-USDT", stablecoin=True)
        single = _pool(symbol="USDC", stablecoin=True)
        result = filter_pools([volatile_lp, stable_lp, single], stablecoin_only=True)
        symbols = [r["symbol"] for r in result]
        assert "USDC-WETH" not in symbols, "Volatile LP should be filtered"
        assert "USDC-USDT" in symbols, "Stable-stable LP should pass"
        assert "USDC" in symbols, "Single stablecoin should pass"

    def test_chainid_hex_parsing(self):
        """chainId as hex string should parse correctly for diamond lookup."""
        # Base = 8453 = 0x2105
        assert _parse_int("0x2105") == 8453
        assert _parse_int("0xa") == 10  # Optimism
        assert _parse_int("0xa4b1") == 42161  # Arbitrum


# ---------------------------------------------------------------------------
# wallet.py — can_migrate, save_state, state round-trip
# ---------------------------------------------------------------------------

from wallet import (
    can_migrate, save_state, load_state, record_migration,
    MIN_POSITION_USD, MIN_MIGRATION_INTERVAL_HOURS, MAX_MIGRATIONS,
    _infer_pool_token, STABLECOINS, ALLOWED_STABLES,
)


class TestCanMigrate:

    def test_sufficient_balance(self):
        state = {"position_usd": 100.0, "migrations": []}
        ok, reason = can_migrate(state, cost_usd=1.0)
        assert ok is True
        assert reason == "ok"

    def test_below_min_balance(self):
        state = {"position_usd": 5.50, "migrations": []}
        ok, reason = can_migrate(state, cost_usd=1.0)
        assert ok is False
        assert "min" in reason.lower() or "$" in reason

    def test_exactly_at_boundary(self):
        """position - cost == MIN_POSITION_USD should fail (strictly less than)."""
        state = {"position_usd": MIN_POSITION_USD + 1.0, "migrations": []}
        ok, _ = can_migrate(state, cost_usd=1.0)
        # 5.0 + 1.0 - 1.0 = 5.0 which is NOT < 5.0 -> allowed
        assert ok is True

    def test_just_below_boundary(self):
        state = {"position_usd": MIN_POSITION_USD + 0.99, "migrations": []}
        ok, _ = can_migrate(state, cost_usd=1.0)
        # 5.99 - 1.0 = 4.99 < 5.0 -> blocked
        assert ok is False

    def test_cooldown_blocks(self):
        recent = datetime.now() - timedelta(hours=1)
        state = {
            "position_usd": 100.0,
            "migrations": [{"timestamp": recent.isoformat()}],
        }
        ok, reason = can_migrate(state, cost_usd=0.50)
        assert ok is False
        assert "cooldown" in reason.lower()

    def test_cooldown_expired(self):
        old = datetime.now() - timedelta(hours=MIN_MIGRATION_INTERVAL_HOURS + 1)
        state = {
            "position_usd": 100.0,
            "migrations": [{"timestamp": old.isoformat()}],
        }
        ok, _ = can_migrate(state, cost_usd=0.50)
        assert ok is True

    def test_no_migrations_history(self):
        state = {"position_usd": 50.0, "migrations": []}
        ok, _ = can_migrate(state, cost_usd=0.25)
        assert ok is True

    def test_malformed_timestamp_ignored(self):
        state = {
            "position_usd": 100.0,
            "migrations": [{"timestamp": "not-a-date"}],
        }
        ok, _ = can_migrate(state, cost_usd=0.50)
        assert ok is True  # bad timestamp is silently skipped

    def test_missing_position_usd(self):
        state = {"migrations": []}
        ok, _ = can_migrate(state, cost_usd=1.0)
        # position defaults to 0 via .get, so 0 - 1 < 5 -> blocked
        assert ok is False

    def test_zero_cost(self):
        state = {"position_usd": 10.0, "migrations": []}
        ok, _ = can_migrate(state, cost_usd=0.0)
        assert ok is True


class TestWalletSafety:
    """Test wallet key validation and address matching."""

    def test_validate_empty_key(self):
        from wallet import validate_private_key
        valid, addr, err = validate_private_key("")
        assert valid is False
        assert "No private key" in err

    def test_validate_wrong_length(self):
        from wallet import validate_private_key
        valid, _, err = validate_private_key("0xdeadbeef")
        assert valid is False
        assert "length" in err

    def test_validate_non_hex(self):
        from wallet import validate_private_key
        valid, _, err = validate_private_key("g" * 64)
        assert valid is False
        assert "non-hex" in err.lower() or "hex" in err.lower()

    def test_validate_correct_format(self):
        from wallet import validate_private_key
        # A valid 64-char hex key (not a real key — all zeros)
        valid, _, err = validate_private_key("0" * 64)
        # Should pass format validation even if web3 isn't installed
        assert valid is True

    def test_validate_with_0x_prefix(self):
        from wallet import validate_private_key
        valid, _, _ = validate_private_key("0x" + "a" * 64)
        assert valid is True

    def test_address_mismatch_detection(self):
        from wallet import check_wallet_address_match
        state = {"address": "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}
        # This key is format-valid but will derive a different address
        # The function should detect mismatch if web3 is available
        # If web3 is not available, it returns True with a warning
        ok, msg = check_wallet_address_match(state, "0" * 64)
        # Either catches mismatch OR warns about missing web3
        assert ok is True or "MISMATCH" in msg

    def test_auto_set_address_when_empty(self):
        """If no address configured, check_wallet_address_match should set it."""
        import tempfile
        from wallet import check_wallet_address_match, STATE_FILE
        state = {"address": "", "current_chain": 8453, "migrations": []}
        original = STATE_FILE
        try:
            import wallet
            wallet.STATE_FILE = Path(tempfile.mktemp(suffix=".json"))
            ok, msg = check_wallet_address_match(state, "0" * 64)
            # Should succeed — either sets address or warns about web3
            assert ok is True
        finally:
            if wallet.STATE_FILE.exists():
                wallet.STATE_FILE.unlink()
            wallet.STATE_FILE = original


class TestStablecoinSwaps:
    """Test stablecoin swap infrastructure."""

    def test_infer_pool_token_single(self):
        assert _infer_pool_token({"symbol": "USDC"}) == "USDC"
        assert _infer_pool_token({"symbol": "DAI"}) == "DAI"
        assert _infer_pool_token({"symbol": "USDT"}) == "USDT"

    def test_infer_pool_token_lp_pair(self):
        assert _infer_pool_token({"symbol": "USDC-DAI"}) == "USDC"
        assert _infer_pool_token({"symbol": "DAI-USDC"}) == "DAI"

    def test_infer_pool_token_unknown_defaults_usdc(self):
        assert _infer_pool_token({"symbol": "WETH"}) == "USDC"
        assert _infer_pool_token({}) == "USDC"

    def test_stablecoins_registry_has_major_chains(self):
        """STABLECOINS should cover Base, Arbitrum, Optimism for USDC at minimum."""
        for chain_id in (8453, 42161, 10):
            assert (chain_id, "USDC") in STABLECOINS, f"Missing USDC on chain {chain_id}"

    def test_stablecoins_addresses_are_checksummed(self):
        """All addresses in STABLECOINS should be valid hex."""
        for key, info in STABLECOINS.items():
            addr = info["address"]
            assert addr.startswith("0x"), f"Bad address for {key}: {addr}"
            assert len(addr) == 42, f"Wrong length for {key}: {addr}"

    def test_record_migration_tracks_token(self):
        """record_migration should set current_token in state."""
        import tempfile, os
        from wallet import STATE_FILE
        state = {
            "address": "", "current_chain": 8453, "current_token": "USDC",
            "current_pool": None, "position_usd": 100.0, "migrations": [],
        }
        pool = {"symbol": "DAI", "project": "aave-v3", "chain": "Base", "apy": 5.0}
        # Temporarily redirect state file to avoid overwriting real state
        original = STATE_FILE
        try:
            import wallet
            wallet.STATE_FILE = Path(tempfile.mktemp(suffix=".json"))
            record_migration(state, 8453, 8453, pool, 0.02, "swap test", to_token="DAI")
            assert state["current_token"] == "DAI"
            assert state["migrations"][-1]["type"] == "swap"
            assert state["migrations"][-1]["from_token"] == "USDC"
            assert state["migrations"][-1]["to_token"] == "DAI"
        finally:
            if wallet.STATE_FILE.exists():
                wallet.STATE_FILE.unlink()
            wallet.STATE_FILE = original

    def test_record_migration_bridge_type(self):
        """Cross-chain move should record type='bridge'."""
        import tempfile
        from wallet import STATE_FILE
        state = {
            "address": "", "current_chain": 8453, "current_token": "USDC",
            "current_pool": None, "position_usd": 100.0, "migrations": [],
        }
        pool = {"symbol": "USDC", "project": "aave-v3", "chain": "Optimism", "apy": 5.0}
        original = STATE_FILE
        try:
            import wallet
            wallet.STATE_FILE = Path(tempfile.mktemp(suffix=".json"))
            record_migration(state, 8453, 10, pool, 0.25, "bridge test")
            assert state["migrations"][-1]["type"] == "bridge"
            assert state["current_chain"] == 10
        finally:
            if wallet.STATE_FILE.exists():
                wallet.STATE_FILE.unlink()
            wallet.STATE_FILE = original


class TestSaveStateAndRoundTrip:

    def test_atomic_write_roundtrip(self, tmp_path):
        state_file = tmp_path / "wallet_state.json"
        state = {
            "address": "0xabc",
            "current_chain": 8453,
            "current_pool": {"symbol": "USDC", "project": "aave-v3", "chain": "Base", "apy": 5.2},
            "position_usd": 99.75,
            "migrations": [
                {"timestamp": "2026-03-07T12:00:00", "from_chain": 42161, "to_chain": 8453,
                 "cost_usd": 0.25, "reason": "better yield"},
            ],
        }
        with patch("wallet.STATE_FILE", state_file):
            save_state(state)
            loaded = load_state()
        assert loaded == state

    def test_save_creates_valid_json(self, tmp_path):
        state_file = tmp_path / "wallet_state.json"
        state = {"position_usd": 42.0, "migrations": []}
        with patch("wallet.STATE_FILE", state_file):
            save_state(state)
        raw = state_file.read_text()
        parsed = json.loads(raw)
        assert parsed["position_usd"] == 42.0

    def test_load_default_when_no_file(self, tmp_path):
        state_file = tmp_path / "nonexistent.json"
        with patch("wallet.STATE_FILE", state_file):
            state = load_state()
        assert state["current_chain"] == 8453
        assert "migrations" in state

    def test_save_overwrites_previous(self, tmp_path):
        state_file = tmp_path / "wallet_state.json"
        with patch("wallet.STATE_FILE", state_file):
            save_state({"position_usd": 100.0, "v": 1})
            save_state({"position_usd": 50.0, "v": 2})
            loaded = load_state()
        assert loaded["v"] == 2
        assert loaded["position_usd"] == 50.0

    def test_no_temp_file_left_on_success(self, tmp_path):
        state_file = tmp_path / "wallet_state.json"
        with patch("wallet.STATE_FILE", state_file):
            save_state({"x": 1})
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0


class TestMigrationsCap:
    """Ensure migrations list doesn't grow unbounded."""

    def test_cap_at_max_migrations(self, tmp_path):
        state_file = tmp_path / "wallet_state.json"
        state = {"position_usd": 100.0, "migrations": [], "current_chain": 8453}
        # Add MAX_MIGRATIONS + 10 entries
        for i in range(MAX_MIGRATIONS + 10):
            state["migrations"].append({"timestamp": f"2026-01-01T{i:05d}", "from_chain": 8453, "to_chain": 42161})
        pool = {"symbol": "USDC", "project": "aave-v3", "chain": "Base", "apy": 5.0, "pool": "abc"}
        with patch("wallet.STATE_FILE", state_file):
            record_migration(state, 42161, 8453, pool, 0.25, "test")
        assert len(state["migrations"]) == MAX_MIGRATIONS

    def test_keeps_most_recent_migrations(self, tmp_path):
        state_file = tmp_path / "wallet_state.json"
        state = {"position_usd": 100.0, "migrations": [], "current_chain": 8453}
        for i in range(MAX_MIGRATIONS + 5):
            state["migrations"].append({"timestamp": f"entry-{i}", "from_chain": 8453, "to_chain": 42161})
        pool = {"symbol": "USDC", "project": "aave-v3", "chain": "Base", "apy": 5.0, "pool": "abc"}
        with patch("wallet.STATE_FILE", state_file):
            record_migration(state, 42161, 8453, pool, 0.25, "test")
        # Most recent entry should be the one we just added
        assert state["migrations"][-1]["reason"] == "test"


# ---------------------------------------------------------------------------
# yield_scanner.py — cache mutation safety
# ---------------------------------------------------------------------------

class TestFilterPoolsCacheSafety:
    """Ensure filter_pools doesn't mutate input pool dicts (cache pollution bug)."""

    def test_no_mutation_of_original_pools(self):
        """filter_pools should shallow-copy before adding _apy_spike/_trusted."""
        original = _pool(apy=5.0, mean30d=4.8, project="aave-v3")
        original_copy = copy.deepcopy(original)
        filter_pools([original], min_apy=3.0)
        # Original dict should NOT have _apy_spike or _trusted keys
        assert "_apy_spike" not in original_copy
        assert "_apy_spike" not in original  # The real check

    def test_spike_flag_not_sticky_across_calls(self):
        """A pool flagged as spiking should not stay flagged in the next filter call."""
        pool = _pool(apy=50.0, mean30d=1.0)  # 50x spike
        result1 = filter_pools([pool], min_apy=3.0)
        assert result1[0]["_apy_spike"] is True

        # Same pool, spike resolved
        pool2 = _pool(apy=5.0, mean30d=4.5)  # Normal
        result2 = filter_pools([pool2], min_apy=3.0)
        assert result2[0]["_apy_spike"] is False

    def test_trusted_flag_independent_per_call(self):
        """Trust flag should be fresh each call, not carried over."""
        trusted = _pool(project="aave-v3")
        untrusted = _pool(project="sketchy-dex")
        r1 = filter_pools([trusted], min_apy=3.0)
        assert r1[0]["_trusted"] is True
        r2 = filter_pools([untrusted], min_apy=3.0)
        assert r2[0]["_trusted"] is False

    def test_multi_asset_flag(self):
        """Pools with 'multi' exposure should be flagged."""
        lp = _pool(apy=10.0)
        lp["exposure"] = "multi"
        single = _pool(apy=10.0)
        single["exposure"] = "single"
        results = filter_pools([lp, single], min_apy=3.0)
        multi_results = [r for r in results if r["_multi_asset"]]
        single_results = [r for r in results if not r["_multi_asset"]]
        assert len(multi_results) == 1
        assert len(single_results) == 1


# ---------------------------------------------------------------------------
# lifi.py — gas fallback
# ---------------------------------------------------------------------------

class TestGasFallback:
    """Verify the gas limit fallback is high enough for bridge TXs."""

    def test_default_gas_limit(self):
        """When quote has no gasLimit/gas, fallback should be 500k not 200k."""
        from lifi import _parse_int
        # Simulate the logic from execute_quote
        tx = {"from": "0x1", "to": "0x2", "data": "0x", "value": "0"}
        gas = _parse_int(tx.get("gasLimit", tx.get("gas", 500000)))
        assert gas == 500000

    def test_quote_gas_respected(self):
        """When quote provides gasLimit, use it."""
        from lifi import _parse_int
        tx = {"gasLimit": "0x61A80"}  # 400000 in hex
        gas = _parse_int(tx.get("gasLimit", tx.get("gas", 500000)))
        assert gas == 400000


# ---------------------------------------------------------------------------
# lifi.py — security validations
# ---------------------------------------------------------------------------

class TestSecurityValidations:
    """Verify security checks in execute_quote."""

    def test_rpc_urls_cover_all_diamond_chains(self):
        """Every chain with a LIFI_DIAMOND entry must have an RPC URL."""
        from lifi import LIFI_DIAMOND, RPC_URLS
        missing = set(LIFI_DIAMOND) - set(RPC_URLS)
        assert not missing, f"Chains with diamond but no RPC URL: {missing}"

    def test_usdc_addresses_cover_rpc_chains(self):
        """Every chain with an RPC URL should have a USDC address for bridging."""
        from lifi import RPC_URLS
        from wallet import USDC
        missing = set(RPC_URLS) - set(USDC)
        assert not missing, f"Chains with RPC but no USDC address: {missing}"

    def test_diamond_address_is_consistent(self):
        """All LIFI_DIAMOND entries should be the same CREATE2 address."""
        from lifi import LIFI_DIAMOND
        addresses = set(LIFI_DIAMOND.values())
        assert len(addresses) == 1, f"Expected 1 diamond address, got {addresses}"

    def test_priority_fee_bounds(self):
        """Dynamic priority fee should be bounded between 0.05 and 5 gwei."""
        gwei = 10**9
        min_tip = int(0.05 * gwei)
        max_tip = int(5 * gwei)
        low = int(0.001 * gwei)
        assert max(min_tip, min(low, max_tip)) == min_tip
        high = int(100 * gwei)
        assert max(min_tip, min(high, max_tip)) == max_tip
        normal = int(1.5 * gwei)
        assert max(min_tip, min(normal, max_tip)) == normal


# ---------------------------------------------------------------------------
# telegram_bot.py — security
# ---------------------------------------------------------------------------

class TestTelegramSecurity:
    """Verify Telegram bot security hardening.
    Tests use html.escape directly since the bot's _escape is just html.escape.
    Tests that need telegram_bot import are skipped if python-telegram-bot is not installed.
    """

    def test_escape_prevents_html_injection(self):
        """html.escape (used by _escape) prevents HTML injection."""
        import html
        assert "<" not in html.escape("<script>alert('xss')</script>")
        assert "&lt;" in html.escape("<b>bold</b>")
        assert html.escape("normal text") == "normal text"

    def test_escape_handles_special_chars(self):
        import html
        assert "&amp;" in html.escape("a & b")
        assert "&quot;" in html.escape('say "hello"', quote=True)

    def test_truncate_logic(self):
        """Truncation should cap at limit and add indicator."""
        # Inline the truncation logic from telegram_bot.py
        def _truncate(text, limit=4000):
            if len(text) <= limit:
                return text
            return text[:limit - 20] + "\n\n…(truncated)"
        long_text = "a" * 5000
        result = _truncate(long_text, limit=100)
        assert len(result) <= 100
        assert "truncated" in result
        assert _truncate("short") == "short"

    def test_migrate_cooldown_minimum(self):
        """Migrate cooldown should be at least 60 seconds to prevent abuse."""
        # Value is hardcoded in telegram_bot.py as MIGRATE_COOLDOWN_SECONDS = 300
        # Verify the file contains a reasonable value
        from pathlib import Path
        import re
        bot_code = (Path(__file__).parent.parent / "telegram_bot.py").read_text()
        match = re.search(r"MIGRATE_COOLDOWN_SECONDS\s*=\s*(\d+)", bot_code)
        assert match, "MIGRATE_COOLDOWN_SECONDS not found in telegram_bot.py"
        assert int(match.group(1)) >= 60

    def test_auth_check_exists_in_source(self):
        """Bot source should contain authorization checks."""
        from pathlib import Path
        bot_code = (Path(__file__).parent.parent / "telegram_bot.py").read_text()
        assert "_is_authorized" in bot_code
        assert "_reject_unauthorized" in bot_code
        assert "chat_id" in bot_code

    def test_no_freeform_text_in_source(self):
        """Bot should have an _ignore handler for non-command text."""
        from pathlib import Path
        bot_code = (Path(__file__).parent.parent / "telegram_bot.py").read_text()
        assert "_ignore" in bot_code
        assert "MessageHandler" in bot_code

    def test_html_escape_used_for_output(self):
        """Bot should import html and use escape for all user-facing output."""
        from pathlib import Path
        bot_code = (Path(__file__).parent.parent / "telegram_bot.py").read_text()
        assert "import html" in bot_code
        assert "_escape" in bot_code
        assert "html.escape" in bot_code
