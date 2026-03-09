"""Wallet module - tracks position state and chain location."""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

STATE_FILE = Path(__file__).parent / "wallet_state.json"
MIN_POSITION_USD = 5.0  # Never migrate if position would drop below this
MIN_MIGRATION_INTERVAL_HOURS = 4  # Cooldown between migrations to prevent thrashing

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


def load_state() -> dict:
    """Load wallet state from disk."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "address": os.getenv("WALLET_ADDRESS", ""),
        "current_chain": 8453,
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


def record_migration(state: dict, from_chain: int, to_chain: int, pool: dict, cost_usd: float, reason: str):
    """Record a migration decision."""
    state["migrations"].append({
        "timestamp": datetime.now().isoformat(),
        "from_chain": from_chain,
        "to_chain": to_chain,
        "pool_symbol": pool.get("symbol", "?"),
        "pool_project": pool.get("project", "?"),
        "pool_apy": pool.get("apy", 0),
        "cost_usd": cost_usd,
        "reason": reason,
    })
    state["current_chain"] = to_chain
    state["current_pool"] = {
        "symbol": pool.get("symbol"),
        "project": pool.get("project"),
        "chain": pool.get("chain"),
        "apy": pool.get("apy"),
    }
    save_state(state)


def format_state(state: dict) -> str:
    """Format current wallet state for display."""
    pool = state.get("current_pool")
    pool_str = f"{pool['symbol']} on {pool['chain']} ({pool['project']}, {pool['apy']:.2f}%)" if pool else "None"
    return (
        f"Chain: {state['current_chain']} | Position: ${state['position_usd']:.2f}\n"
        f"Pool: {pool_str}\n"
        f"Migrations: {len(state.get('migrations', []))}"
    )
