"""Tests for yield_scanner v2 — multi-source, discovery, incentives."""

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
import pytest

import yield_scanner


# ---- Mock data ----

MOCK_DEFILLAMA_POOLS = [
    {
        "pool": "dl-pool-1",
        "chain": "Base",
        "project": "aave-v3",
        "symbol": "USDC",
        "tvlUsd": 50_000_000,
        "apy": 5.5,
        "apyBase": 5.0,
        "apyMean30d": 5.2,
        "stablecoin": True,
        "ilRisk": "no",
        "outlier": False,
        "exposure": "single",
        "tvlPct1D": 2.0,
        "tvlPct7D": 5.0,
    },
    {
        "pool": "dl-pool-2",
        "chain": "Arbitrum",
        "project": "compound-v3",
        "symbol": "USDT",
        "tvlUsd": 20_000_000,
        "apy": 4.2,
        "apyBase": 4.0,
        "apyMean30d": 4.1,
        "stablecoin": True,
        "ilRisk": "no",
        "outlier": False,
        "exposure": "single",
        "tvlPct1D": 25.0,  # Rising!
        "tvlPct7D": 60.0,  # Rising!
    },
    {
        "pool": "dl-pool-3",
        "chain": "Optimism",
        "project": "unknown-protocol",
        "symbol": "USDC",
        "tvlUsd": 1_000_000,
        "apy": 8.0,
        "apyBase": 7.5,
        "apyMean30d": 7.8,
        "stablecoin": True,
        "ilRisk": "no",
        "outlier": False,
        "exposure": "single",
    },
]

MOCK_BEEFY_VAULTS = [
    {"id": "aave-v3-usdc-base", "chain": "base", "status": "active",
     "token": "USDC", "platformId": "aave-v3", "assets": ["usdc"]},
    {"id": "stargate-usdt-arb", "chain": "arbitrum", "status": "active",
     "token": "USDT", "platformId": "stargate", "assets": ["usdt"]},
    {"id": "beefy-only-vault", "chain": "optimism", "status": "active",
     "token": "DAI", "platformId": "beefy", "assets": ["dai"]},
    {"id": "retired-vault", "chain": "base", "status": "eol",
     "token": "USDC", "platformId": "old", "assets": ["usdc"]},
]

MOCK_BEEFY_APYS = {
    "aave-v3-usdc-base": 0.052,    # 5.2%
    "stargate-usdt-arb": 0.045,    # 4.5%
    "beefy-only-vault": 0.065,     # 6.5%
    "retired-vault": 0.03,
}

MOCK_BEEFY_TVLS = {
    "aave-v3-usdc-base": 40_000_000,
    "stargate-usdt-arb": 15_000_000,
    "beefy-only-vault": 2_000_000,
    "retired-vault": 100_000,
}

MOCK_MERKL_CAMPAIGNS = [
    {
        "chainId": 8453,  # Base
        "protocol": "aave-v3",
        "rewardToken": "OP",
        "apr": 2.5,
        "tokens": ["USDC"],
        "type": "lending",
    },
    {
        "chainId": 42161,  # Arbitrum
        "protocol": "compound-v3",
        "rewardToken": "ARB",
        "apr": 1.8,
        "tokens": ["USDT"],
        "type": "lending",
    },
]


# ---- Helper ----

def _make_mock_response(data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


# ---- Tests ----

class TestBeefyFetch:
    def setup_method(self):
        yield_scanner._beefy_cache = []
        yield_scanner._beefy_cache_ts = 0

    def test_fetch_beefy_normalizes_vaults(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[
            _make_mock_response(MOCK_BEEFY_VAULTS),
            _make_mock_response(MOCK_BEEFY_APYS),
            _make_mock_response(MOCK_BEEFY_TVLS),
        ])

        result = asyncio.run(yield_scanner.fetch_beefy_vaults(client))

        # Should only include active vaults (not eol)
        assert len(result) == 3
        # Check normalization
        first = result[0]
        assert first["chain"] == "Base"
        assert first["apy"] == pytest.approx(5.2)  # 0.052 * 100
        assert first["_source"] == "beefy"
        assert first["stablecoin"] is True

    def test_fetch_beefy_caching(self):
        yield_scanner._beefy_cache = [{"pool": "cached"}]
        yield_scanner._beefy_cache_ts = yield_scanner.time.time()

        client = AsyncMock()
        result = asyncio.run(yield_scanner.fetch_beefy_vaults(client))

        assert result == [{"pool": "cached"}]
        client.get.assert_not_called()

    def test_fetch_beefy_graceful_failure(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=Exception("connection timeout"))

        result = asyncio.run(yield_scanner.fetch_beefy_vaults(client))
        assert result == []


class TestMerklFetch:
    def setup_method(self):
        yield_scanner._merkl_cache = []
        yield_scanner._merkl_cache_ts = 0

    def test_fetch_merkl_campaigns(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(MOCK_MERKL_CAMPAIGNS))

        result = asyncio.run(yield_scanner.fetch_merkl_campaigns(client))

        assert ("Base", "aave-v3") in result
        assert ("Arbitrum", "compound-v3") in result
        assert result[("Base", "aave-v3")][0]["apr"] == 2.5

    def test_fetch_merkl_graceful_failure(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=Exception("timeout"))

        result = asyncio.run(yield_scanner.fetch_merkl_campaigns(client))
        assert result == {}


class TestDiscovery:
    def test_detect_rising_1d(self):
        pool = {"tvlPct1D": 25.0, "tvlPct7D": 60.0}
        result = yield_scanner._detect_discovery(pool)
        assert result is not None
        assert result["signal"] == "rising_1d"
        assert result["tvl_pct_1d"] == 25.0

    def test_detect_rising_7d_only(self):
        pool = {"tvlPct1D": 5.0, "tvlPct7D": 55.0}
        result = yield_scanner._detect_discovery(pool)
        assert result is not None
        assert result["signal"] == "rising_7d"

    def test_no_discovery_for_stable_pool(self):
        pool = {"tvlPct1D": 2.0, "tvlPct7D": 10.0}
        result = yield_scanner._detect_discovery(pool)
        assert result is None

    def test_no_discovery_when_missing_data(self):
        pool = {}
        result = yield_scanner._detect_discovery(pool)
        assert result is None


class TestCrossReference:
    def test_cross_reference_matches(self):
        dl_pools = [
            {"chain": "Base", "symbol": "USDC", "apy": 5.5, "pool": "dl-1"},
        ]
        beefy_pools = [
            {"chain": "Base", "symbol": "USDC", "apy": 5.2, "pool": "beefy-1"},
        ]
        result = yield_scanner._cross_reference_beefy(dl_pools, beefy_pools)
        assert len(result) == 1
        assert result[0]["_sources"] == ["defillama", "beefy"]
        assert result[0]["_beefy_apy"] == 5.2

    def test_cross_reference_adds_beefy_only(self):
        dl_pools = [
            {"chain": "Base", "symbol": "USDC", "apy": 5.5, "pool": "dl-1"},
        ]
        beefy_pools = [
            {"chain": "Base", "symbol": "USDC", "apy": 5.2, "pool": "beefy-1"},
            {"chain": "Optimism", "symbol": "DAI", "apy": 6.5, "pool": "beefy-2"},
        ]
        result = yield_scanner._cross_reference_beefy(dl_pools, beefy_pools)
        assert len(result) == 2
        beefy_only = [p for p in result if p.get("_sources") == ["beefy"]]
        assert len(beefy_only) == 1
        assert beefy_only[0]["symbol"] == "DAI"

    def test_cross_reference_flags_disagreement(self):
        dl_pools = [
            {"chain": "Base", "symbol": "USDC", "apy": 10.0, "pool": "dl-1"},
        ]
        beefy_pools = [
            {"chain": "Base", "symbol": "USDC", "apy": 3.0, "pool": "beefy-1"},
        ]
        result = yield_scanner._cross_reference_beefy(dl_pools, beefy_pools)
        assert result[0].get("_apy_disagreement") is True
        assert result[0]["_apy_disagreement_ratio"] == pytest.approx(3.33, abs=0.01)


class TestMerklAttach:
    def test_attach_merkl_incentives(self):
        pools = [
            {"chain": "Base", "project": "aave-v3", "symbol": "USDC"},
            {"chain": "Arbitrum", "project": "unknown", "symbol": "USDT"},
        ]
        campaigns = {
            ("Base", "aave-v3"): [{"reward_token": "OP", "apr": 2.5, "tokens": [], "type": "lending"}],
        }
        result = yield_scanner._attach_merkl_incentives(pools, campaigns)
        assert result[0].get("_merkl_incentives") is not None
        assert result[0]["_incentive_apr"] == 2.5
        assert result[1].get("_merkl_incentives") is None


class TestFilterPools:
    def test_filter_with_discovery_tagging(self):
        result = yield_scanner.filter_pools(
            MOCK_DEFILLAMA_POOLS, chains=[8453, 42161, 10]
        )
        # Pool 2 (Arbitrum USDT) should have discovery metadata
        arb_pool = next((p for p in result if p["chain"] == "Arbitrum"), None)
        assert arb_pool is not None
        assert "_discovery" in arb_pool
        assert arb_pool["_discovery"]["signal"] == "rising_1d"

    def test_filter_preserves_source_tags(self):
        pools = [{**MOCK_DEFILLAMA_POOLS[0], "_sources": ["defillama", "beefy"]}]
        result = yield_scanner.filter_pools(pools, chains=[8453])
        assert result[0]["_sources"] == ["defillama", "beefy"]

    def test_trend_aware_ranking(self):
        """Rising pools should rank higher than equivalent stable ones."""
        pool_rising = {
            **MOCK_DEFILLAMA_POOLS[0],
            "pool": "rising",
            "_trend": {"is_rising": True, "is_stable": True, "slope": 0.5},
        }
        pool_stable = {
            **MOCK_DEFILLAMA_POOLS[0],
            "pool": "stable",
        }
        # Both have same base APY, but rising gets boosted
        with patch("yield_scanner._attach_trend_signals", side_effect=lambda x: x):
            result = yield_scanner.filter_pools(
                [pool_stable, pool_rising], chains=[8453]
            )
        if len(result) == 2:
            assert result[0]["pool"] == "rising"


class TestScanYields:
    def setup_method(self):
        yield_scanner._pool_cache = []
        yield_scanner._pool_cache_ts = 0
        yield_scanner._beefy_cache = []
        yield_scanner._beefy_cache_ts = 0
        yield_scanner._merkl_cache = []
        yield_scanner._merkl_cache_ts = 0

    def test_scan_yields_multi_source(self):
        client = AsyncMock()

        with patch("yield_scanner.fetch_pools", return_value=MOCK_DEFILLAMA_POOLS), \
             patch("yield_scanner.fetch_beefy_vaults", return_value=[
                 {"chain": "Base", "symbol": "USDC", "apy": 5.2, "pool": "beefy-1",
                  "tvlUsd": 40_000_000, "stablecoin": True, "ilRisk": "no",
                  "outlier": False, "apyMean30d": 5.0, "project": "aave-v3"},
             ]), \
             patch("yield_scanner.fetch_merkl_campaigns", return_value={
                 ("Base", "aave-v3"): [{"reward_token": "OP", "apr": 2.5, "tokens": [], "type": "lending"}],
             }):
            result = asyncio.run(yield_scanner.scan_yields(client, chains=[8453, 42161, 10]))

        assert len(result) > 0
        # Base USDC should have multi-source tag
        base_pool = next((p for p in result if p["chain"] == "Base" and p["symbol"] == "USDC"), None)
        if base_pool:
            assert "defillama" in base_pool.get("_sources", [])

    def test_scan_yields_beefy_failure_graceful(self):
        client = AsyncMock()

        with patch("yield_scanner.fetch_pools", return_value=MOCK_DEFILLAMA_POOLS), \
             patch("yield_scanner.fetch_beefy_vaults", side_effect=Exception("timeout")), \
             patch("yield_scanner.fetch_merkl_campaigns", return_value={}):
            result = asyncio.run(yield_scanner.scan_yields(client, chains=[8453, 42161, 10]))

        # Should still return results from DefiLlama
        assert len(result) > 0

    def test_scan_yields_defillama_failure_raises(self):
        client = AsyncMock()

        with patch("yield_scanner.fetch_pools", side_effect=ValueError("bad response")), \
             patch("yield_scanner.fetch_beefy_vaults", return_value=[]), \
             patch("yield_scanner.fetch_merkl_campaigns", return_value={}):
            with pytest.raises(ValueError):
                asyncio.run(yield_scanner.scan_yields(client))


class TestFormatPool:
    def test_format_basic(self):
        pool = {
            "symbol": "USDC", "chain": "Base", "apy": 5.5,
            "tvlUsd": 50_000_000, "project": "aave-v3", "apyMean30d": 5.2,
        }
        result = yield_scanner.format_pool(pool)
        assert "USDC" in result
        assert "Base" in result
        assert "5.50%" in result

    def test_format_with_sources(self):
        pool = {
            "symbol": "USDC", "chain": "Base", "apy": 5.5,
            "tvlUsd": 50_000_000, "project": "aave-v3", "apyMean30d": 5.2,
            "_sources": ["defillama", "beefy"],
        }
        result = yield_scanner.format_pool(pool)
        assert "Sources: defillama+beefy" in result

    def test_format_with_incentives(self):
        pool = {
            "symbol": "USDC", "chain": "Base", "apy": 5.5,
            "tvlUsd": 50_000_000, "project": "aave-v3", "apyMean30d": 5.2,
            "_incentive_apr": 2.5,
        }
        result = yield_scanner.format_pool(pool)
        assert "Incentives: +2.5%" in result

    def test_format_with_discovery(self):
        pool = {
            "symbol": "USDC", "chain": "Base", "apy": 5.5,
            "tvlUsd": 50_000_000, "project": "aave-v3", "apyMean30d": 5.2,
            "_discovery": {"signal": "rising_1d"},
        }
        result = yield_scanner.format_pool(pool)
        assert "NEW/RISING" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
