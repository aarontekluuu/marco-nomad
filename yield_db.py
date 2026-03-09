"""Historical yield tracking via SQLite."""
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "yields.db"

def _get_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS yield_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        chain TEXT, project TEXT, symbol TEXT,
        apy REAL, apy_mean_30d REAL, tvl_usd REAL,
        pool_id TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ts ON yield_snapshots(timestamp)")
    return db

def record_snapshot(pools: list[dict], max_pools: int = 20):
    """Record top N pool APYs."""
    db = _get_db()
    ts = time.time()
    for p in pools[:max_pools]:
        db.execute(
            "INSERT INTO yield_snapshots (timestamp, chain, project, symbol, apy, apy_mean_30d, tvl_usd, pool_id) VALUES (?,?,?,?,?,?,?,?)",
            (ts, p.get("chain"), p.get("project"), p.get("symbol"), p.get("apy", 0), p.get("apyMean30d", 0), p.get("tvlUsd", 0), p.get("pool")),
        )
    db.commit()
    db.close()

def get_trends(pool_symbol: str, days: int = 7) -> list[dict]:
    """Get APY trend for a pool over N days."""
    db = _get_db()
    cutoff = time.time() - days * 86400
    rows = db.execute(
        "SELECT timestamp, apy, tvl_usd FROM yield_snapshots WHERE symbol = ? AND timestamp > ? ORDER BY timestamp",
        (pool_symbol, cutoff),
    ).fetchall()
    db.close()
    return [{"timestamp": r[0], "apy": r[1], "tvl_usd": r[2]} for r in rows]

def cleanup(max_age_days: int = 90):
    """Remove snapshots older than N days."""
    db = _get_db()
    cutoff = time.time() - max_age_days * 86400
    db.execute("DELETE FROM yield_snapshots WHERE timestamp < ?", (cutoff,))
    db.commit()
    db.close()
