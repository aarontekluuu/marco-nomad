"""Multi-source yield scanner — DefiLlama + Beefy + Merkl incentives.

v2: AAR-117 — multi-source, on-chain verification, discovery.
"""

import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)

# Protocol metadata cache (DefiLlama /protocols endpoint)
_protocol_cache: dict = {}
_protocol_cache_ts: float = 0


async def fetch_protocol_info(client: httpx.AsyncClient, project: str) -> dict | None:
    """Fetch protocol info from DefiLlama. Cached for 1 hour."""
    global _protocol_cache, _protocol_cache_ts
    if _protocol_cache and (time.time() - _protocol_cache_ts) < 3600:
        return _protocol_cache.get(project)
    try:
        resp = await client.get("https://api.llama.fi/protocols", timeout=15)
        resp.raise_for_status()
        protocols = resp.json()
        _protocol_cache = {p.get("slug", ""): p for p in protocols}
        _protocol_cache_ts = time.time()
        return _protocol_cache.get(project)
    except Exception:
        return None

POOLS_URL = "https://yields.llama.fi/pools"

# Cache to avoid re-fetching 5-10MB pool data every cycle
_pool_cache: list[dict] = []
_pool_cache_ts: float = 0
CACHE_TTL = 300  # 5 minutes

# --- Beefy Finance secondary source ---
BEEFY_VAULTS_URL = "https://api.beefy.finance/vaults"
BEEFY_APY_URL = "https://api.beefy.finance/apy"
BEEFY_TVL_URL = "https://api.beefy.finance/tvl"

_beefy_cache: list[dict] = []
_beefy_cache_ts: float = 0

# Beefy chain key -> our chain name
_BEEFY_CHAIN_MAP = {
    "ethereum": "Ethereum", "base": "Base", "arbitrum": "Arbitrum",
    "optimism": "Optimism", "polygon": "Polygon", "bsc": "BSC",
    "avax": "Avalanche", "fantom": "Fantom", "zksync": "zkSync Era",
    "linea": "Linea", "scroll": "Scroll",
}

# --- Merkl incentive awareness ---
MERKL_API_URL = "https://api.merkl.xyz/v4/opportunities"

_merkl_cache: list[dict] = []
_merkl_cache_ts: float = 0

# LI.FI chain ID -> DefiLlama chain name
CHAIN_MAP = {
    1: "Ethereum",
    8453: "Base",
    42161: "Arbitrum",
    10: "Optimism",
    137: "Polygon",
    56: "BSC",
    43114: "Avalanche",
    250: "Fantom",
    324: "zkSync Era",
    59144: "Linea",
    534352: "Scroll",
}

CHAIN_MAP_REVERSE = {v: k for k, v in CHAIN_MAP.items()}


async def fetch_pools(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all yield pools from DefiLlama, with caching."""
    global _pool_cache, _pool_cache_ts
    if _pool_cache and (time.time() - _pool_cache_ts) < CACHE_TTL:
        return _pool_cache
    # Retry once on timeout — DefiLlama's 5-10MB response can be slow
    last_err = None
    for attempt in range(2):
        try:
            resp = await client.get(POOLS_URL, timeout=30)
            resp.raise_for_status()
            break
        except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
            last_err = e
            if attempt == 0:
                await asyncio.sleep(2)
    else:
        raise last_err  # type: ignore[misc]
    body = resp.json()
    if not isinstance(body, dict) or "data" not in body:
        raise ValueError(f"Unexpected DefiLlama response: {str(body)[:200]}")
    _pool_cache = body["data"]
    _pool_cache_ts = time.time()
    return _pool_cache


# ---- Beefy Finance fetch ----

async def fetch_beefy_vaults(client: httpx.AsyncClient) -> list[dict]:
    """Fetch Beefy Finance vaults and normalize to DefiLlama-compatible format.

    Returns pools with _source='beefy' for cross-referencing.
    """
    global _beefy_cache, _beefy_cache_ts
    if _beefy_cache and (time.time() - _beefy_cache_ts) < CACHE_TTL:
        return _beefy_cache

    # Fetch vaults, APYs, and TVLs in parallel
    results = await asyncio.gather(
        client.get(BEEFY_VAULTS_URL, timeout=15),
        client.get(BEEFY_APY_URL, timeout=15),
        client.get(BEEFY_TVL_URL, timeout=15),
        return_exceptions=True,
    )

    for r in results:
        if isinstance(r, Exception):
            log.warning("Beefy fetch partial failure: %s", r)
            return []

    vaults_resp, apy_resp, tvl_resp = results
    vaults_resp.raise_for_status()
    apy_resp.raise_for_status()
    tvl_resp.raise_for_status()

    vaults = vaults_resp.json()
    apys = apy_resp.json()  # {vault_id: apy_decimal}
    tvls = tvl_resp.json()  # {vault_id: tvl_usd} or nested

    normalized = []
    for v in vaults:
        if v.get("status") != "active":
            continue
        vid = v.get("id", "")
        chain_key = v.get("chain", "")
        chain_name = _BEEFY_CHAIN_MAP.get(chain_key)
        if not chain_name:
            continue

        apy_val = apys.get(vid)
        if apy_val is None:
            continue
        apy_pct = apy_val * 100  # Beefy returns decimal (0.05 = 5%)

        # TVL: Beefy returns {vault_id: tvl_usd} for /tvl
        tvl_val = tvls.get(vid, 0)
        if isinstance(tvl_val, dict):
            tvl_val = tvl_val.get(vid, 0)

        token_sym = v.get("token", v.get("oracleId", vid)).upper()
        platform_id = v.get("platformId", "beefy")

        normalized.append({
            "pool": f"beefy-{vid}",
            "chain": chain_name,
            "project": platform_id,
            "symbol": token_sym,
            "tvlUsd": tvl_val,
            "apy": apy_pct,
            "apyBase": apy_pct,
            "apyMean30d": None,  # Beefy doesn't provide this
            "stablecoin": v.get("assets", [""])[0].lower() in {
                "usdc", "usdt", "dai", "frax", "lusd", "gusd", "busd",
                "usdc.e", "usdt.e", "usdbc", "pyusd", "crvusd", "gho",
            } if v.get("assets") else False,
            "ilRisk": "no",
            "outlier": False,
            "_source": "beefy",
            "_beefy_vault_id": vid,
            "_beefy_platform": platform_id,
        })

    _beefy_cache = normalized
    _beefy_cache_ts = time.time()
    log.info("Beefy: fetched %d active vaults", len(normalized))
    return normalized


# ---- Merkl incentive campaigns ----

async def fetch_merkl_campaigns(client: httpx.AsyncClient) -> dict:
    """Fetch active Merkl reward campaigns.

    Returns a dict keyed by (chain_name, protocol) -> list of campaign info.
    Used to tag pools with active incentives.
    """
    global _merkl_cache, _merkl_cache_ts
    if _merkl_cache and (time.time() - _merkl_cache_ts) < CACHE_TTL:
        return _merkl_cache

    try:
        resp = await client.get(MERKL_API_URL, timeout=15, params={"status": "LIVE"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("Merkl fetch failed: %s", e)
        return {}

    campaigns_by_key: dict = {}
    items = data if isinstance(data, list) else data.get("opportunities", data.get("data", []))

    for opp in items if isinstance(items, list) else []:
        chain_id = opp.get("chainId")
        chain_name = CHAIN_MAP.get(chain_id, "")
        if not chain_name:
            continue
        protocol = (opp.get("protocol") or "").lower()
        tokens = opp.get("tokens", [])
        reward_token = opp.get("rewardToken", opp.get("distributionToken", ""))
        apr = opp.get("apr", 0)

        key = (chain_name, protocol)
        if key not in campaigns_by_key:
            campaigns_by_key[key] = []
        campaigns_by_key[key].append({
            "reward_token": reward_token if isinstance(reward_token, str) else str(reward_token),
            "apr": apr,
            "tokens": tokens,
            "type": opp.get("type", "unknown"),
        })

    _merkl_cache = campaigns_by_key
    _merkl_cache_ts = time.time()
    log.info("Merkl: fetched campaigns for %d chain/protocol pairs", len(campaigns_by_key))
    return campaigns_by_key


# Trusted protocols — large, audited, battle-tested DeFi protocols
# Pools from unlisted protocols are deprioritized (not excluded) for hackathon flexibility
TRUSTED_PROTOCOLS = {
    "aave-v3", "aave-v2", "compound-v3", "compound-v2", "morpho", "morpho-blue",
    "spark", "maker", "sky", "curve-dex", "convex-finance", "yearn-finance",
    "lido", "rocket-pool", "frax-ether", "benqi-lending", "radiant-v2",
    "silo-v2", "moonwell", "seamless-protocol", "fluid", "euler",
    "venus", "stargate", "across", "hop-protocol", "synapse", "merkl",
    # Beefy itself is trusted as an aggregator
    "beefy",
}

# Max ratio of current APY to 30-day average before flagging as suspicious spike
MAX_APY_SPIKE_RATIO = 5.0


def calc_risk_score(pool: dict) -> int:
    """Calculate 0-100 risk score (higher = safer)."""
    score = 0
    # TVL (30 points)
    tvl = pool.get("tvlUsd", 0)
    if tvl > 100_000_000: score += 30
    elif tvl > 10_000_000: score += 25
    elif tvl > 1_000_000: score += 15
    elif tvl > 500_000: score += 5
    # Protocol trust (20 points)
    if pool.get("_trusted"): score += 20
    # APY stability (20 points) - closer to 30d avg = more stable
    apy = pool.get("apy", 0)
    mean30d = pool.get("apyMean30d", 0)
    if mean30d and mean30d > 0:
        ratio = apy / mean30d
        if 0.8 <= ratio <= 1.2: score += 20
        elif 0.5 <= ratio <= 2.0: score += 10
    # Not an outlier (15 points)
    if not pool.get("outlier"): score += 15
    # No IL risk (15 points)
    if pool.get("ilRisk") != "yes": score += 15
    return min(score, 100)


def _detect_discovery(pool: dict) -> dict | None:
    """Detect new/rising pool based on TVL percentage changes.

    Returns discovery metadata dict if pool qualifies, else None.
    """
    tvl_pct_1d = pool.get("tvlPct1D")
    tvl_pct_7d = pool.get("tvlPct7D")

    if tvl_pct_1d is None and tvl_pct_7d is None:
        return None

    # Rising TVL: >20% in 1 day or >50% in 7 days signals a new/growing pool
    is_rising_1d = tvl_pct_1d is not None and tvl_pct_1d > 20
    is_rising_7d = tvl_pct_7d is not None and tvl_pct_7d > 50

    if not (is_rising_1d or is_rising_7d):
        return None

    return {
        "is_new_pool": is_rising_1d and (tvl_pct_7d is None or tvl_pct_7d > 100),
        "tvl_pct_1d": tvl_pct_1d,
        "tvl_pct_7d": tvl_pct_7d,
        "signal": "rising_1d" if is_rising_1d else "rising_7d",
    }


def _cross_reference_beefy(
    defillama_pools: list[dict],
    beefy_pools: list[dict],
) -> list[dict]:
    """Cross-reference DefiLlama pools with Beefy vaults.

    - Tags DefiLlama pools with Beefy APY for comparison
    - Adds Beefy-only pools that DefiLlama missed
    - Flags stale/inflated data when sources disagree significantly
    """
    # Build lookup: (chain, symbol_normalized) -> beefy pool
    beefy_by_key: dict[tuple, dict] = {}
    for bp in beefy_pools:
        key = (bp.get("chain", ""), bp.get("symbol", "").upper())
        beefy_by_key[key] = bp

    seen_beefy_keys = set()
    enriched = []

    for p in defillama_pools:
        p = {**p}  # shallow copy
        key = (p.get("chain", ""), (p.get("symbol") or "").upper())
        beefy_match = beefy_by_key.get(key)

        if beefy_match:
            seen_beefy_keys.add(key)
            beefy_apy = beefy_match.get("apy", 0)
            p["_beefy_apy"] = beefy_apy
            p["_sources"] = ["defillama", "beefy"]

            # Flag stale/inflated: >2x difference between sources
            dl_apy = p.get("apy") or 0
            if dl_apy > 0 and beefy_apy > 0:
                ratio = max(dl_apy, beefy_apy) / min(dl_apy, beefy_apy)
                if ratio > 2.0:
                    p["_apy_disagreement"] = True
                    p["_apy_disagreement_ratio"] = round(ratio, 2)
        else:
            p["_sources"] = ["defillama"]

        enriched.append(p)

    # Add Beefy-only pools (not in DefiLlama)
    for key, bp in beefy_by_key.items():
        if key not in seen_beefy_keys:
            bp = {**bp}
            bp["_sources"] = ["beefy"]
            enriched.append(bp)

    return enriched


def _attach_merkl_incentives(pools: list[dict], merkl_campaigns: dict) -> list[dict]:
    """Attach Merkl incentive data to matching pools."""
    if not merkl_campaigns:
        return pools

    for p in pools:
        chain_name = p.get("chain", "")
        project = (p.get("project") or "").lower()
        key = (chain_name, project)
        campaigns = merkl_campaigns.get(key)
        if campaigns:
            p["_merkl_incentives"] = campaigns
            total_extra_apr = sum(c.get("apr", 0) for c in campaigns)
            p["_incentive_apr"] = total_extra_apr

    return pools


def _attach_trend_signals(pools: list[dict]) -> list[dict]:
    """Attach yield_db trend signals to pools for trend-aware ranking."""
    try:
        from yield_db import calc_trend
    except ImportError:
        return pools

    for p in pools:
        trend = calc_trend(
            pool_id=p.get("pool", ""),
            symbol=p.get("symbol", ""),
            project=p.get("project", ""),
            chain=p.get("chain", ""),
        )
        if trend:
            p["_trend"] = trend

    return pools


def filter_pools(
    pools: list[dict],
    chains: list[int] | None = None,
    min_tvl: float = 500_000,
    min_apy: float = 3.0,
    max_apy: float = 50.0,
    stablecoin_only: bool = True,
    exclude_outliers: bool = True,
    no_il_risk: bool = True,
    max_results: int = 20,
) -> list[dict]:
    """Filter and rank pools by base APY, favoring sustainable yields."""
    chain_names = None
    if chains:
        chain_names = {CHAIN_MAP.get(c) for c in chains if c in CHAIN_MAP}

    filtered = []
    for p in pools:
        if chain_names and p.get("chain") not in chain_names:
            continue
        if (p.get("tvlUsd") or 0) < min_tvl:
            continue
        apy = p.get("apy") or 0
        if apy < min_apy or apy > max_apy:
            continue
        if stablecoin_only:
            if not p.get("stablecoin"):
                continue
            # Extra guard: DefiLlama's stablecoin flag can be unreliable for LP pairs.
            # Skip pools whose symbol contains known volatile tokens mixed with stables.
            symbol = (p.get("symbol") or "").upper()
            # Only surface tokens Marco can actually hold (USD-pegged stablecoins)
            _HOLDABLE_TOKENS = {"USDC", "USDT", "DAI", "FRAX", "LUSD", "GUSD", "BUSD",
                                "USDC.E", "USDT.E", "USDBC", "PYUSD", "CRVUSD", "GHO"}
            if "-" in symbol or "/" in symbol:
                # LP pair — check both sides are holdable stablecoins
                parts = [t.strip() for t in symbol.replace("/", "-").split("-")]
                if not all(t in _HOLDABLE_TOKENS for t in parts if t):
                    continue  # Has exotic/non-USD or volatile component — skip
            else:
                # Single token — must be holdable
                if symbol not in _HOLDABLE_TOKENS:
                    continue  # Exotic stable (EURC, FXUSD, YOUSD) — skip
        if exclude_outliers and p.get("outlier"):
            continue
        if no_il_risk and p.get("ilRisk") == "yes":
            continue

        # Shallow copy so we don't mutate cached pool objects
        p = {**p}

        # Detect suspicious APY spikes: current >> 30-day average
        mean30d = p.get("apyMean30d") or 0
        p["_apy_spike"] = mean30d > 0 and apy / mean30d > MAX_APY_SPIKE_RATIO

        # Detect yield collapse: current << 30-day average (dying pool)
        p["_apy_collapse"] = mean30d > 0 and apy < mean30d * 0.3  # Current < 30% of 30d avg

        # Mark protocol trust level
        project = (p.get("project") or "").lower()
        p["_trusted"] = project in TRUSTED_PROTOCOLS

        # Flag pools with single-asset exposure risk (e.g. "multi" exposure = LP pair)
        exposure = (p.get("exposure") or "").lower()
        p["_multi_asset"] = exposure == "multi"

        # Protocol age/audit from DefiLlama protocol cache (AAR-81)
        proto_info = _protocol_cache.get(project) if _protocol_cache else None
        if proto_info:
            listed_at = proto_info.get("listedAt")
            if listed_at:
                p["_protocol_age_days"] = int((time.time() - listed_at) / 86400)
            else:
                p["_protocol_age_days"] = None
            p["_audited"] = bool(proto_info.get("audits") or proto_info.get("audit_links"))
        else:
            p["_protocol_age_days"] = None
            p["_audited"] = None

        # Risk score (AAR-92)
        p["_risk_score"] = calc_risk_score(p)

        # New pool discovery (AAR-117)
        discovery = _detect_discovery(p)
        if discovery:
            p["_discovery"] = discovery

        filtered.append(p)

    # Trend-aware ranking (AAR-117): integrate yield_db signals
    filtered = _attach_trend_signals(filtered)

    # Sort by apyMean30d (sustainable yield) — more reliable than spot APY or apyBase
    # Light trust boost (1.15x) to surface trusted protocols without burying higher yields.
    # The brain makes the final trust vs. yield tradeoff — ranking just ensures variety.
    def _sort_key(x):
        base = x.get("apyMean30d") or x.get("apyBase") or x.get("apy") or 0
        if x.get("_trusted"):
            base *= 1.15  # Mild boost — enough to break ties, not dominate
        if x.get("_apy_spike"):
            base *= 0.5  # Penalize spikes in ranking
        if x.get("_apy_collapse"):
            base *= 0.3  # Heavily penalize collapsing yields

        # Trend-aware adjustments (AAR-117)
        trend = x.get("_trend")
        if trend:
            if trend.get("is_rising"):
                base *= 1.10  # Boost rising pools
            if not trend.get("is_stable"):
                base *= 0.90  # Penalize volatile pools

        # Incentive boost (AAR-117): Merkl campaigns add value
        incentive_apr = x.get("_incentive_apr", 0)
        if incentive_apr > 0:
            base += incentive_apr * 0.5  # Partial credit for incentive APR

        # Discovery bonus: new rising pools get mild visibility boost
        if x.get("_discovery"):
            base *= 1.05

        # Penalize APY disagreement between sources
        if x.get("_apy_disagreement"):
            base *= 0.85

        return -base

    filtered.sort(key=_sort_key)
    return filtered[:max_results]


def format_pool(p: dict) -> str:
    """Format a pool for display."""
    chain_id = CHAIN_MAP_REVERSE.get(p["chain"], "?")
    parts = [
        f"{p['symbol']} on {p['chain']} (chain {chain_id})",
        f"APY: {p.get('apy', 0):.2f}%",
        f"TVL: ${p.get('tvlUsd', 0):,.0f}",
        f"Project: {p.get('project', '?')}",
        f"30d avg: {p.get('apyMean30d', 0):.2f}%",
    ]
    # Show source info
    sources = p.get("_sources")
    if sources and len(sources) > 1:
        parts.append(f"Sources: {'+'.join(sources)}")
    # Show incentives
    incentive_apr = p.get("_incentive_apr", 0)
    if incentive_apr > 0:
        parts.append(f"Incentives: +{incentive_apr:.1f}%")
    # Show discovery tag
    if p.get("_discovery"):
        parts.append("NEW/RISING")
    return " | ".join(parts)


async def scan_yields(
    client: httpx.AsyncClient,
    chains: list[int] | None = None,
    min_tvl: float = 500_000,
    min_apy: float = 3.0,
) -> list[dict]:
    """Scan for best yields across chains from multiple sources.

    Fetches DefiLlama, Beefy Finance, and Merkl campaigns in parallel.
    Falls back to DefiLlama-only if secondary sources fail.
    """
    # Parallel fetch from all sources with graceful degradation
    results = await asyncio.gather(
        fetch_pools(client),
        fetch_beefy_vaults(client),
        fetch_merkl_campaigns(client),
        return_exceptions=True,
    )

    # DefiLlama (primary — required)
    defillama_pools = results[0]
    if isinstance(defillama_pools, Exception):
        raise defillama_pools  # Can't operate without primary source

    # Beefy (secondary — optional)
    beefy_pools = results[1]
    if isinstance(beefy_pools, Exception):
        log.warning("Beefy fetch failed, continuing with DefiLlama only: %s", beefy_pools)
        beefy_pools = []

    # Merkl (incentive data — optional)
    merkl_campaigns = results[2]
    if isinstance(merkl_campaigns, Exception):
        log.warning("Merkl fetch failed, continuing without incentive data: %s", merkl_campaigns)
        merkl_campaigns = {}

    # Cross-reference sources
    if beefy_pools:
        pools = _cross_reference_beefy(defillama_pools, beefy_pools)
        log.info("Cross-referenced %d DefiLlama + %d Beefy pools -> %d total",
                 len(defillama_pools), len(beefy_pools), len(pools))
    else:
        pools = defillama_pools

    # Filter and rank
    filtered = filter_pools(pools, chains=chains, min_tvl=min_tvl, min_apy=min_apy)

    # Attach Merkl incentive data to filtered results
    if merkl_campaigns:
        filtered = _attach_merkl_incentives(filtered, merkl_campaigns)

    return filtered
