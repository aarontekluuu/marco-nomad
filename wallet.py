"""Wallet module - tracks position state and chain location."""

import json
import os
from pathlib import Path

STATE_FILE = Path(__file__).parent / "wallet_state.json"

# Well-known USDC addresses per chain
USDC = {
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    10: "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    137: "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
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
    """Save wallet state to disk."""
    STATE_FILE.write_text(json.dumps(state, indent=2))


def record_migration(state: dict, from_chain: int, to_chain: int, pool: dict, cost_usd: float, reason: str):
    """Record a migration decision."""
    state["migrations"].append({
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
