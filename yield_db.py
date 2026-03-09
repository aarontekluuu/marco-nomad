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

def calc_trend(pool_id: str = "", symbol: str = "", project: str = "", chain: str = "", days: int = 7) -> dict | None:
    """Calculate APY trend signals for a pool. Returns None if insufficient data.

    Looks up by pool_id first, falls back to symbol+project+chain.
    Returns: {slope, volatility, tvl_change_pct, is_rising, is_stable, data_points}
    """
    db = _get_db()
    cutoff = time.time() - days * 86400

    if pool_id:
        rows = db.execute(
            "SELECT timestamp, apy, tvl_usd FROM yield_snapshots WHERE pool_id = ? AND timestamp > ? ORDER BY timestamp",
            (pool_id, cutoff),
        ).fetchall()
    elif symbol and project:
        query = "SELECT timestamp, apy, tvl_usd FROM yield_snapshots WHERE symbol = ? AND project = ? AND timestamp > ? ORDER BY timestamp"
        params = [symbol, project, cutoff]
        if chain:
            query = "SELECT timestamp, apy, tvl_usd FROM yield_snapshots WHERE symbol = ? AND project = ? AND chain = ? AND timestamp > ? ORDER BY timestamp"
            params = [symbol, project, chain, cutoff]
        rows = db.execute(query, params).fetchall()
    else:
        db.close()
        return None

    db.close()

    if len(rows) < 3:
        return None  # Need at least 3 data points for meaningful trend

    apys = [r[1] for r in rows]
    tvls = [r[2] for r in rows if r[2]]
    timestamps = [r[0] for r in rows]

    # Linear regression slope (APY change per day)
    n = len(apys)
    t_norm = [(ts - timestamps[0]) / 86400 for ts in timestamps]  # days from first
    mean_t = sum(t_norm) / n
    mean_apy = sum(apys) / n
    num = sum((t - mean_t) * (a - mean_apy) for t, a in zip(t_norm, apys))
    den = sum((t - mean_t) ** 2 for t in t_norm)
    slope = num / den if den > 0 else 0.0

    # Volatility (std dev as % of mean)
    if mean_apy > 0:
        variance = sum((a - mean_apy) ** 2 for a in apys) / n
        volatility = (variance ** 0.5) / mean_apy * 100
    else:
        volatility = 0.0

    # TVL trajectory
    tvl_change_pct = 0.0
    if len(tvls) >= 2 and tvls[0] > 0:
        tvl_change_pct = (tvls[-1] - tvls[0]) / tvls[0] * 100

    return {
        "slope": round(slope, 4),           # APY change per day
        "volatility": round(volatility, 1),  # std dev as % of mean
        "tvl_change_pct": round(tvl_change_pct, 1),
        "is_rising": slope > 0.1,           # >0.1% per day = rising
        "is_stable": volatility < 15,       # <15% relative std dev = stable
        "data_points": n,
    }


def cleanup(max_age_days: int = 90):
    """Remove snapshots older than N days."""
    db = _get_db()
    cutoff = time.time() - max_age_days * 86400
    db.execute("DELETE FROM yield_snapshots WHERE timestamp < ?", (cutoff,))
    db.commit()
    db.close()
