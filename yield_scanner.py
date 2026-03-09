"""DefiLlama yield scanner - finds best yields across chains."""

import asyncio
import time

import httpx

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


# Trusted protocols — large, audited, battle-tested DeFi protocols
# Pools from unlisted protocols are deprioritized (not excluded) for hackathon flexibility
TRUSTED_PROTOCOLS = {
    "aave-v3", "aave-v2", "compound-v3", "compound-v2", "morpho", "morpho-blue",
    "spark", "maker", "sky", "curve-dex", "convex-finance", "yearn-finance",
    "lido", "rocket-pool", "frax-ether", "benqi-lending", "radiant-v2",
    "silo-v2", "moonwell", "seamless-protocol", "fluid", "euler",
    "venus", "stargate", "across", "hop-protocol", "synapse", "merkl",
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
    if mean30d > 0:
        ratio = apy / mean30d
        if 0.8 <= ratio <= 1.2: score += 20
        elif 0.5 <= ratio <= 2.0: score += 10
    # Not an outlier (15 points)
    if not pool.get("outlier"): score += 15
    # No IL risk (15 points)
    if pool.get("ilRisk") != "yes": score += 15
    return min(score, 100)


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

        filtered.append(p)

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
        return -base

    filtered.sort(key=_sort_key)
    return filtered[:max_results]


def format_pool(p: dict) -> str:
    """Format a pool for display."""
    chain_id = CHAIN_MAP_REVERSE.get(p["chain"], "?")
    return (
        f"{p['symbol']} on {p['chain']} (chain {chain_id}) | "
        f"APY: {p.get('apy', 0):.2f}% | "
        f"TVL: ${p.get('tvlUsd', 0):,.0f} | "
        f"Project: {p.get('project', '?')} | "
        f"30d avg: {p.get('apyMean30d', 0):.2f}%"
    )


async def scan_yields(
    client: httpx.AsyncClient,
    chains: list[int] | None = None,
    min_tvl: float = 500_000,
    min_apy: float = 3.0,
) -> list[dict]:
    """Scan for best yields across chains."""
    pools = await fetch_pools(client)
    return filter_pools(pools, chains=chains, min_tvl=min_tvl, min_apy=min_apy)
