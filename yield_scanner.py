"""DefiLlama yield scanner - finds best yields across chains."""

import asyncio
import time

import httpx

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


def filter_pools(
    pools: list[dict],
    chains: list[int] | None = None,
    min_tvl: float = 500_000,
    min_apy: float = 3.0,
    max_apy: float = 100.0,
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
        if stablecoin_only and not p.get("stablecoin"):
            continue
        if exclude_outliers and p.get("outlier"):
            continue
        if no_il_risk and p.get("ilRisk") == "yes":
            continue

        # Shallow copy so we don't mutate cached pool objects
        p = {**p}

        # Detect suspicious APY spikes: current >> 30-day average
        mean30d = p.get("apyMean30d") or 0
        p["_apy_spike"] = mean30d > 0 and apy / mean30d > MAX_APY_SPIKE_RATIO

        # Mark protocol trust level
        project = (p.get("project") or "").lower()
        p["_trusted"] = project in TRUSTED_PROTOCOLS

        filtered.append(p)

    # Sort by apyMean30d (sustainable yield) — more reliable than spot APY or apyBase
    # Trusted protocols get a 1.5x boost in sort score
    def _sort_key(x):
        base = x.get("apyMean30d") or x.get("apyBase") or x.get("apy") or 0
        if x.get("_trusted"):
            base *= 1.5
        if x.get("_apy_spike"):
            base *= 0.5  # Penalize spikes in ranking
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
