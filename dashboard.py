"""Marco the Nomad — Streamlit Dashboard.

Run: streamlit run dashboard.py
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Marco the Nomad", page_icon="🏜️", layout="wide")

# ---------------------------------------------------------------------------
# Custom styling
# ---------------------------------------------------------------------------

st.markdown("""
<style>
    div[data-testid="stMetric"] {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
        border: 1px solid #0f3460;
        border-radius: 12px;
        padding: 16px 20px;
    }
    div[data-testid="stMetric"] label {
        color: #8899aa !important;
    }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        color: #e0e0e0 !important;
    }
    .journal-card {
        background: linear-gradient(135deg, #16213e 0%, #1a1a2e 100%);
        border-left: 3px solid #e94560;
        padding: 16px 20px;
        margin: 10px 0;
        border-radius: 0 10px 10px 0;
        line-height: 1.7;
    }
    .journal-card .j-time {
        color: #e94560;
        font-size: 0.78em;
        font-weight: 600;
        letter-spacing: 0.5px;
        text-transform: uppercase;
    }
    .journal-card .j-text {
        color: #c8d0d8;
        font-style: italic;
        margin-top: 6px;
    }
    .migration-card {
        background: linear-gradient(135deg, #0f3460 0%, #16213e 100%);
        border-radius: 10px;
        padding: 14px 18px;
        margin: 8px 0;
        border: 1px solid #1a3a6a;
    }
    .migration-card .m-route {
        font-size: 1.05em;
        font-weight: 600;
        color: #e0e0e0;
    }
    .migration-card .m-detail {
        color: #8899aa;
        font-size: 0.85em;
        margin-top: 4px;
    }
    .migration-card .m-reason {
        color: #6a7a8a;
        font-size: 0.8em;
        margin-top: 6px;
        font-style: italic;
    }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Load state
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "wallet_state.json"
JOURNAL_FILE = ROOT / "journal.json"
DEMO_STATE = ROOT / "demo" / "wallet_state.json"
DEMO_JOURNAL = ROOT / "demo" / "journal.json"

CHAIN_NAMES = {
    1: "Ethereum", 8453: "Base", 42161: "Arbitrum",
    10: "Optimism", 137: "Polygon", 56: "BSC",
    43114: "Avalanche", 250: "Fantom",
}
CHAIN_EMOJI = {
    "Base": "🔵", "Optimism": "🔴", "Arbitrum": "🔷",
    "Polygon": "🟣", "Ethereum": "⬛", "BSC": "🟡",
    "Avalanche": "🔺", "Fantom": "👻",
}


def load_state() -> dict:
    for f in (STATE_FILE, DEMO_STATE):
        if f.exists():
            return json.loads(f.read_text())
    return {"current_chain": 8453, "current_pool": None,
            "position_usd": 100.0, "migrations": []}


def load_journal() -> list[str]:
    for f in (JOURNAL_FILE, DEMO_JOURNAL):
        if f.exists():
            return json.loads(f.read_text())
    return []


state = load_state()
journal = load_journal()
migrations = state.get("migrations", [])

chain_name = CHAIN_NAMES.get(state["current_chain"], f"Chain {state['current_chain']}")
chain_emoji = CHAIN_EMOJI.get(chain_name, "🔗")
pool = state.get("current_pool")
total_cost = sum(m.get("cost_usd", 0) for m in migrations)

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.markdown("# 🏜️ Marco the Nomad")
st.markdown("*Autonomous cross-chain yield nomad — powered by LI.FI + Claude*")
st.markdown("")

# ---------------------------------------------------------------------------
# Hero metrics
# ---------------------------------------------------------------------------

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("💰 Position", f"${state['position_usd']:.2f}")
c2.metric(f"{chain_emoji} Chain", chain_name)
c3.metric("📈 Current APY", f"{pool['apy']:.1f}%" if pool else "—")
c4.metric("🗺️ Migrations", str(len(migrations)))
c5.metric("🌉 Bridge Costs", f"${total_cost:.2f}")

if pool:
    st.markdown("")
    st.markdown(
        f"**Active position:** `{pool.get('symbol', '?')}` on "
        f"**{pool.get('project', '?').title()}** ({chain_name}) "
        f"— earning **{pool.get('apy', 0):.1f}% APY**"
    )

st.divider()

# ---------------------------------------------------------------------------
# Two-column layout: Journal + Migrations
# ---------------------------------------------------------------------------

left, right = st.columns([3, 2], gap="large")

with left:
    st.markdown("### 📖 Marco's Journal")
    st.caption("Marco writes a journal entry every decision cycle — his reasoning, observations, and personality.")

    if journal:
        for entry in reversed(journal):
            if entry.startswith("["):
                ts_end = entry.find("]")
                if ts_end > 0:
                    raw_ts = entry[1:ts_end]
                    text = entry[ts_end + 2:]
                    try:
                        dt = datetime.fromisoformat(raw_ts[:19])
                        time_str = dt.strftime("%b %d · %H:%M")
                    except ValueError:
                        time_str = raw_ts[:16]
                else:
                    time_str = ""
                    text = entry
            else:
                time_str = ""
                text = entry

            if " [RISK:" in text:
                text = text[:text.rfind(" [RISK:")]

            st.markdown(f'''
<div class="journal-card">
    <div class="j-time">{time_str}</div>
    <div class="j-text">{text}</div>
</div>
''', unsafe_allow_html=True)
    else:
        st.info("Journal is empty. Run `python marco.py --once` to start.")

with right:
    st.markdown("### 🗺️ Migration History")

    if migrations:
        for m in reversed(migrations):
            from_name = CHAIN_NAMES.get(m.get("from_chain", 0), f"Chain {m.get('from_chain', '?')}")
            to_name = CHAIN_NAMES.get(m.get("to_chain", 0), f"Chain {m.get('to_chain', '?')}")
            from_emoji = CHAIN_EMOJI.get(from_name, "🔗")
            to_emoji = CHAIN_EMOJI.get(to_name, "🔗")
            cost = m.get("cost_usd", 0)
            apy = m.get("pool_apy", 0)
            pool_sym = m.get("pool_symbol", "?")
            pool_proj = m.get("pool_project", "?").title()
            reason = m.get("reason", "")

            st.markdown(f'''
<div class="migration-card">
    <div class="m-route">{from_emoji} {from_name} → {to_emoji} {to_name}</div>
    <div class="m-detail">
        {pool_sym} on {pool_proj} · {apy:.1f}% APY · Bridge: ${cost:.2f} via LI.FI
    </div>
    <div class="m-reason">{reason[:150]}</div>
</div>
''', unsafe_allow_html=True)
    else:
        st.info("No migrations yet. Marco is watching the yields...")

    st.markdown("")

    if migrations:
        st.markdown("### 💸 Cost Analysis")
        st.markdown(f"""
| Metric | Value |
|--------|-------|
| Total bridge costs | **${total_cost:.2f}** |
| Migrations | **{len(migrations)}** |
| Avg cost / migration | **${total_cost / len(migrations):.2f}** |
| Starting position | **$100.00** |
| Current position | **${state['position_usd']:.2f}** |
| Net cost impact | **{total_cost / 100 * 100:.2f}%** |
""")

    st.markdown("### 🔑 Wallet")
    addr = state.get("address", "—")
    st.code(addr, language=None)

# ---------------------------------------------------------------------------
# Live Yield Scanner
# ---------------------------------------------------------------------------

st.divider()
st.markdown("### 📡 Live Yield Scanner")

if st.button("Scan Yields Now", type="primary"):
    with st.spinner("Scanning DefiLlama for yields across Base, Arbitrum, Optimism, Polygon..."):
        import httpx
        from yield_scanner import scan_yields

        async def _scan():
            async with httpx.AsyncClient() as client:
                return await scan_yields(client, chains=[8453, 42161, 10, 137])

        try:
            pools = asyncio.run(_scan())
            if pools:
                rows = []
                for p in pools[:15]:
                    rows.append({
                        "Chain": p.get("chain", "?"),
                        "Protocol": p.get("project", "?"),
                        "Pool": p.get("symbol", "?"),
                        "APY": f"{p.get('apy', 0):.2f}%",
                        "30d Avg": f"{p.get('apyMean30d', 0):.2f}%",
                        "TVL": f"${p.get('tvlUsd', 0):,.0f}",
                    })
                st.dataframe(rows, use_container_width=True, hide_index=True)
            else:
                st.warning("No pools found matching criteria.")
        except Exception as e:
            st.error(f"Scan failed: {e}")
else:
    st.caption("Click to fetch live yield data from DefiLlama.")

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    "Built for the [LI.FI Vibeathon](https://li.fi) · "
    "Marco uses **LI.FI** for cross-chain bridge quotes and execution, "
    "**DefiLlama** for yield data, and **Claude** for decision-making."
)
