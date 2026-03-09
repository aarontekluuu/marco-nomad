# Marco the Nomad

**An autonomous AI agent that roams across blockchains, chasing the best yields and journaling every move.**

Built for the [LI.FI Vibeathon](https://li.fi) — Marco migrates capital across chains using LI.FI bridges, evaluates opportunities with Claude, and writes about it like a restless trader keeping a travel diary.

---

## Why This Matters

DeFi yield is fragmented across dozens of chains. A USDC pool paying 28% on Base today might drop to 12% tomorrow while Optimism surges to 16%. Manually tracking yields, comparing bridge costs, and executing migrations is tedious and error-prone.

**Marco solves this.** He autonomously scans yields across chains, gets real bridge costs from LI.FI before committing, and only moves when the math works. Every decision is journaled in first-person — you can read exactly *why* he moved and what he was thinking.

**In his first demo run**, Marco:
- Started with $100 USDC on Base at 15.6% APY
- Migrated to Optimism when Velodrome hit 16.2% (bridge cost: $0.26 via LI.FI)
- Migrated back to Base when Merkl surged to 28.47% (bridge cost: $0.18 via LI.FI)
- **Total bridge costs: $0.44** across 2 migrations — paid for itself in under 2 hours of yield spread
- **Final position: $99.84** with access to 28.47% APY

---

## What Marco Does

Marco is a **cross-chain yield nomad**. Every cycle, he:

1. **Scans** — Pulls live yield data from DefiLlama across Base, Arbitrum, Optimism, Polygon
2. **Quotes** — Gets real bridge costs from LI.FI to see if moving is worth it
3. **Thinks** — Claude evaluates the spread vs. bridge cost with Marco's personality
4. **Moves** — Executes the migration (or holds, if the math doesn't work)
5. **Journals** — Logs every decision in first-person, like a nomad's travel diary

> *"Woke up to a shakeup. Optimism Velodrome compressed to 14.62%. Meanwhile Base is on fire — Merkl USDC surging at 28.47% with a 30-day average of 23.51%. LI.FI quote for moving back to Base: $0.18 via eco bridge, 4 seconds. On a 13.85% spread, that bridge cost pays for itself in under 2 hours. The math is screaming. Pulling back to Base."*

---

## Dashboard

Marco includes a Streamlit dashboard for visual monitoring:

```bash
streamlit run dashboard.py
```

The dashboard shows:
- Current position (chain, pool, APY, balance)
- Migration history with costs and reasons
- Marco's journal entries
- Live yield scanner (fetches from DefiLlama on demand)
- Bridge cost analysis across all migrations

Ships with demo data — works out of the box without API keys.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  Marco Agent Loop               │
│                   (marco.py)                    │
├────────────┬──────────┬────────────┬────────────┤
│            │          │            │            │
│   Yield    │  LI.FI   │   Brain    │  Wallet   │
│  Scanner   │  Bridge  │  (Claude)  │  State    │
│            │          │            │            │
│ DefiLlama  │ Quotes   │ Decide +   │ Track     │
│ pools API  │ Routes   │ Journal    │ position  │
│ Filter +   │ Costs    │ Personality│ Record    │
│ rank       │ Execute  │ Risk mgmt  │ migrations│
└────┬───────┴────┬─────┴─────┬──────┴─────┬─────┘
     │            │           │            │
     ▼            ▼           ▼            ▼
 DefiLlama    LI.FI API   Claude API   Local JSON
 yields.llama  li.quest    Anthropic    wallet_state
   .fi/pools   /v1/quote               .json
```

---

## How LI.FI Powers Marco

LI.FI is Marco's **bridge brain** — the critical piece that makes cross-chain migration possible:

| LI.FI Feature | How Marco Uses It |
|---|---|
| `/v1/quote` | Get optimal bridge route + cost for any chain pair |
| `/v1/advanced/routes` | Compare multiple routes to find cheapest path |
| `/v1/status` | Track bridge transaction completion with exponential backoff |
| `/v1/chains` | Discover supported chains dynamically |
| Cost calculation | Extract fees, gas, and spread from quote estimates |
| Transaction execution | Sign + send bridge TX via LI.FI's transaction request |
| Diamond validation | Verify TX destination matches known LI.FI contracts before signing |

Marco's decision engine weighs **yield spread vs. LI.FI bridge cost** — a migration only happens when the math makes sense:

```
if (target_apy - current_apy) * position * hold_days > bridge_cost:
    migrate()  # via LI.FI
else:
    hold()     # not worth the toll
```

---

## Marco's Personality

Marco isn't just a bot — he's a **character**. Every decision gets a journal entry written by Claude in Marco's voice:

- **Restless** — Never stays on one chain too long if yields compress
- **Pragmatic** — Only moves when the numbers work (yield spread > bridge cost)
- **Opinionated** — Has strong views on chains, protocols, and market conditions
- **Risk-aware** — Moves his whole bag but with TX simulation, confidence gating, and safety guards
- **Journaling** — Documents everything like a travel diary

---

## Sample Output

```
[14:32:01] Marco the Nomad waking up... Mode: DEMO (simulated)
[14:32:01] Chains: [8453, 42161, 10, 137] | Min TVL: $500,000 | Min APY: 3.0%
[14:32:02] Scanning yields...
[14:32:03] Found 12 opportunities. Top 5:
  USDC on Base (chain 8453) | APY: 28.47% | TVL: $190,000 | Project: merkl
  USDC on Optimism (chain 10) | APY: 14.95% | TVL: $401,000 | Project: extra-finance
  USDC-MSUSD on Optimism (chain 10) | APY: 14.62% | TVL: $520,000 | Project: velodrome-v2
[14:32:04] Getting bridge quotes...
  -> Optimism: $0.18 (0.18% of position) via lifi
[14:32:05] Marco is thinking...
[14:32:06] Decision: HOLD (confidence: 85%)
[14:32:06] Journal: Base Merkl holding strong at 28.47%. Arb is a ghost town
           for stables. Sometimes the best trade is no trade. Holding.
```

---

## Safety & Security

Marco is built for real money, not just demos:

- **TX simulation** — `eth_call` before signing to catch reverts without wasting gas
- **TX destination validation** — every transaction checked against known LI.FI diamond contracts (CREATE2 deterministic: `0x1231DEB6...` on all 11 supported chains)
- **Exact-amount approvals** — no `MAX_UINT256` approvals that could drain the wallet
- **EIP-1559 gas pricing** — on all transactions including approvals to reduce MEV exposure
- **Approval receipt verification** — confirms approval TX succeeded before attempting bridge TX
- **Confidence gating** — brain must output >60% confidence to trigger a migration
- **Position safety guards** — minimum $5 balance, 4-hour migration cooldown
- **Quote freshness** — re-fetches bridge quote right before execution
- **On-chain balance reconciliation** — compares tracked position with `balanceOf()` every cycle
- **APY spike detection** — flags pools where current APY exceeds 5x the 30-day average
- **Protocol trust scoring** — battle-tested protocols (Aave, Compound, Morpho, etc.) ranked higher
- **Exponential backoff** — bridge status polling backs off from 5s to 30s (not hammering LI.FI API)

## Quick Start

```bash
# Clone
git clone https://github.com/aarontekluuu/marco-nomad.git
cd marco-nomad

# Install
pip install -r requirements.txt

# Dashboard (works immediately with demo data — no API keys needed)
streamlit run dashboard.py

# Configure for agent mode
cp .env.template .env
# Add your ANTHROPIC_API_KEY (required for agent)
# Add LIFI_API_KEY (optional, for higher rate limits)

# Run one cycle (demo mode — no real transactions)
python marco.py --once

# Run continuous (hourly cycles)
python marco.py

# Run tests
pytest tests/ -v
```

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | **Required.** Claude API key for brain |
| `DEMO_MODE` | `true` | Simulate moves (no real transactions) |
| `SCAN_CHAINS` | `8453,42161,10,137` | Chain IDs to scan (Base, Arb, OP, Polygon) |
| `MIN_TVL_USD` | `500000` | Skip pools under this TVL |
| `MIN_APY` | `3.0` | Minimum APY threshold |
| `MAX_BRIDGE_COST_PCT` | `2.0` | Max bridge cost as % of position |
| `MIN_CONFIDENCE` | `0.6` | Brain must be this confident to migrate |
| `POSITION_SIZE_USD` | `100` | Position size in USD |
| `LOOP_INTERVAL` | `3600` | Seconds between cycles |
| `WALLET_PRIVATE_KEY` | — | LIVE mode only. Private key for signing |

---

## Tech Stack

- **[LI.FI](https://li.fi)** — Cross-chain bridge quotes, routes, and execution
- **[DefiLlama](https://defillama.com)** — Real-time yield pool data
- **[Claude](https://anthropic.com)** — Decision reasoning + personality engine
- **[Streamlit](https://streamlit.io)** — Dashboard for visual monitoring
- **[python-telegram-bot](https://python-telegram-bot.org)** — Optional Telegram notifications
- **Python 3.11+** / asyncio / httpx / web3.py

---

## File Structure

```
marco-nomad/
├── marco.py           # Main agent loop — CLI entry point
├── brain.py           # Claude decision engine + nomad personality
├── lifi.py            # LI.FI API: quotes, routes, cost calc, TX execution
├── yield_scanner.py   # DefiLlama scanning + protocol trust + spike detection
├── wallet.py          # Position tracking, balance reconciliation, safety guards
├── dashboard.py       # Streamlit dashboard for visual monitoring
├── telegram_bot.py    # Telegram bot interface (optional)
├── main.py            # Alternative entry with Telegram bot integration
├── demo/              # Seed data for dashboard (works without API keys)
├── tests/
│   └── test_core.py   # 56 tests: bridge cost, yield filter, brain parsing, wallet
├── requirements.txt   # Python dependencies
└── .env.template      # Environment variable template
```

---

*Marco never sleeps. He just waits for the next yield.*
