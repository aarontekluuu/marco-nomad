"""Wallet module - tracks position state and chain location."""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

STATE_FILE = Path(__file__).parent / "wallet_state.json"
MIN_POSITION_USD = 5.0  # Never migrate if position would drop below this
MIN_MIGRATION_INTERVAL_HOURS = 4  # Cooldown between migrations to prevent thrashing
MAX_MIGRATIONS = 200  # Cap migration history to prevent wallet_state.json bloat

# Well-known USDC addresses per chain (must match yield_scanner.CHAIN_MAP)
USDC = {
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",       # Ethereum
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",     # Base
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",    # Arbitrum
    10: "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",       # Optimism
    137: "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",      # Polygon
    56: "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",       # BSC
    43114: "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",   # Avalanche
    250: "0x04068DA6C83AFCFA0e13ba15A6696662335D5B75",      # Fantom
    324: "0x1d17CBcF0D6D143135aE902365D2E5e2A16538D4",      # zkSync Era
    59144: "0x176211869cA2b568f2A7D4EE941E073a821EE1ff",    # Linea (USDC.e)
    534352: "0x06eFdBFf2a14a7c8E15944D1F4A48F9F95F663A4",  # Scroll
}

USDC_DECIMALS = 6

# Stablecoins Marco can hold — keyed by (chain_id, symbol) -> {address, decimals}
# Used for same-chain swaps when a DAI/USDT pool pays better than USDC
STABLECOINS = {
    # Ethereum
    (1, "USDC"): {"address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": 6},
    (1, "USDT"): {"address": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "decimals": 6},
    (1, "DAI"):  {"address": "0x6B175474E89094C44Da98b954EedeAC495271d0F", "decimals": 18},
    # Base
    (8453, "USDC"):  {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
    (8453, "USDbC"): {"address": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", "decimals": 6},
    (8453, "DAI"):   {"address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "decimals": 18},
    # Arbitrum
    (42161, "USDC"):  {"address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
    (42161, "USDT"):  {"address": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", "decimals": 6},
    (42161, "DAI"):   {"address": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1", "decimals": 18},
    # Optimism
    (10, "USDC"): {"address": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", "decimals": 6},
    (10, "USDT"): {"address": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58", "decimals": 6},
    (10, "DAI"):  {"address": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1", "decimals": 18},
    # Polygon
    (137, "USDC"): {"address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "decimals": 6},
    (137, "USDT"): {"address": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", "decimals": 6},
    (137, "DAI"):  {"address": "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063", "decimals": 18},
}

# Which stablecoin symbols Marco can swap into (all pegged to $1)
ALLOWED_STABLES = {"USDC", "USDT", "DAI", "USDbC", "USDC.e", "FRAX", "LUSD", "GHO", "PYUSD", "crvUSD"}


def load_state() -> dict:
    """Load wallet state from disk."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "address": os.getenv("WALLET_ADDRESS", ""),
        "current_chain": 8453,
        "current_token": "USDC",  # Which stablecoin Marco currently holds
        "current_pool": None,
        "position_usd": float(os.getenv("POSITION_SIZE_USD", "100")),
        "migrations": [],
    }


def save_state(state: dict):
    """Save wallet state to disk atomically (write to tmp, then rename)."""
    data = json.dumps(state, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=STATE_FILE.parent, suffix=".tmp")
    try:
        os.write(fd, data.encode())
        os.close(fd)
        fd = -1  # Mark as closed
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def can_migrate(state: dict, cost_usd: float) -> tuple[bool, str]:
    """Check if migration is safe. Returns (allowed, reason)."""
    position = state.get("position_usd", 0)
    if position - cost_usd < MIN_POSITION_USD:
        return False, f"Position ${position:.2f} - bridge ${cost_usd:.2f} = ${position - cost_usd:.2f} < min ${MIN_POSITION_USD}"

    migrations = state.get("migrations", [])
    if migrations:
        last_ts = migrations[-1].get("timestamp", "")
        try:
            last_dt = datetime.fromisoformat(last_ts)
            hours_since = (datetime.now() - last_dt).total_seconds() / 3600
            if hours_since < MIN_MIGRATION_INTERVAL_HOURS:
                return False, f"Cooldown: {hours_since:.1f}h since last migration (min {MIN_MIGRATION_INTERVAL_HOURS}h)"
        except (ValueError, TypeError):
            pass

    return True, "ok"


def record_migration(
    state: dict, from_chain: int, to_chain: int, pool: dict,
    cost_usd: float, reason: str, to_token: str | None = None,
):
    """Record a migration decision. Caps history at MAX_MIGRATIONS."""
    from_token = state.get("current_token", "USDC")
    target_token = to_token or _infer_pool_token(pool)
    is_swap = from_chain == to_chain and from_token != target_token

    state["migrations"].append({
        "timestamp": datetime.now().isoformat(),
        "from_chain": from_chain,
        "to_chain": to_chain,
        "from_token": from_token,
        "to_token": target_token,
        "type": "swap" if is_swap else "bridge",
        "pool_symbol": pool.get("symbol", "?"),
        "pool_project": pool.get("project", "?"),
        "pool_apy": pool.get("apy", 0),
        "cost_usd": cost_usd,
        "reason": reason,
    })
    # Cap migration history
    if len(state["migrations"]) > MAX_MIGRATIONS:
        state["migrations"] = state["migrations"][-MAX_MIGRATIONS:]
    state["current_chain"] = to_chain
    state["current_token"] = target_token
    state["current_pool"] = {
        "pool_id": pool.get("pool"),  # DefiLlama unique pool UUID
        "symbol": pool.get("symbol"),
        "project": pool.get("project"),
        "chain": pool.get("chain"),
        "apy": pool.get("apy"),
    }
    save_state(state)


def _infer_pool_token(pool: dict) -> str:
    """Infer the primary token from a pool symbol. Returns 'USDC' as default."""
    symbol = (pool.get("symbol") or "USDC").upper()
    # Single token pool
    if symbol in ALLOWED_STABLES or symbol in {"USDC.E", "USDBC"}:
        return symbol
    # LP pair — return first recognized stablecoin
    for sep in ("-", "/"):
        if sep in symbol:
            for part in symbol.split(sep):
                part = part.strip()
                if part in ALLOWED_STABLES:
                    return part
    return "USDC"


BALANCE_OF_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]


def check_onchain_balance(
    chain_id: int, wallet_address: str, rpc_url: str, token: str = "USDC",
) -> float | None:
    """Query actual stablecoin balance on-chain. Returns USD amount or None on failure."""
    try:
        from web3 import Web3
        # Look up token address from STABLECOINS registry, fallback to USDC
        stable_info = STABLECOINS.get((chain_id, token))
        if stable_info:
            token_addr = stable_info["address"]
            decimals = stable_info["decimals"]
        else:
            token_addr = USDC.get(chain_id)
            decimals = USDC_DECIMALS
        if not token_addr or not wallet_address:
            return None
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr),
            abi=BALANCE_OF_ABI,
        )
        raw = contract.functions.balanceOf(
            Web3.to_checksum_address(wallet_address)
        ).call()
        return raw / (10 ** decimals)
    except Exception:
        return None


def reconcile_balance(state: dict, rpc_url: str) -> float | None:
    """Compare tracked position_usd with on-chain balance. Returns drift or None."""
    actual = check_onchain_balance(
        state["current_chain"],
        state.get("address", ""),
        rpc_url,
        token=state.get("current_token", "USDC"),
    )
    if actual is None:
        return None
    tracked = state.get("position_usd", 0)
    drift = actual - tracked
    if abs(drift) > 0.01:  # More than 1 cent drift
        state["position_usd"] = round(actual, 2)
        state["_last_reconcile"] = datetime.now().isoformat()
        state["_last_drift_usd"] = round(drift, 4)
        save_state(state)
    return drift


def format_state(state: dict) -> str:
    """Format current wallet state for display."""
    pool = state.get("current_pool")
    pool_str = f"{pool['symbol']} on {pool['chain']} ({pool['project']}, {pool['apy']:.2f}%)" if pool else "None"
    return (
        f"Chain: {state['current_chain']} | Position: ${state['position_usd']:.2f}\n"
        f"Pool: {pool_str}\n"
        f"Migrations: {len(state.get('migrations', []))}"
    )
