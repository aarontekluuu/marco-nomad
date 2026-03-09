# Marco the Nomad

**An autonomous AI agent that roams across blockchains, chasing the best yields and journaling every move.**

Built for the [LI.FI Vibeathon](https://li.fi) — Marco migrates capital across chains using LI.FI bridges, evaluates opportunities with Claude, and writes about it like a restless trader keeping a travel diary.

---

## What Marco Does

Marco is a **cross-chain yield nomad**. Every cycle, he:

1. **Scans** — Pulls live yield data from DefiLlama across Base, Arbitrum, Optimism, Polygon
2. **Quotes** — Gets real bridge costs from LI.FI to see if moving is worth it
3. **Thinks** — Claude evaluates the spread vs. bridge cost with Marco's personality
4. **Moves** — Executes the migration (or holds, if the math doesn't work)
5. **Journals** — Logs every decision in first-person, like a nomad's travel diary

> *"Day 5. Arbitrum yields dried up overnight — 4.2% down from 6.1% last week.
> Meanwhile Base is heating up, Aave v3 offering 7.1% on USDC. Bridge cost via LI.FI:
> 0.3%. That's a no-brainer. Moving 60% over."*

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  Marco Agent Loop               │
│                   (main.py)                     │
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
| `/v1/status` | Track bridge transaction completion |
| `/v1/chains` | Discover supported chains dynamically |
| Cost calculation | Extract fees, gas, slippage from quote estimates |
| Transaction execution | Sign + send bridge TX via LI.FI's transaction request |

Marco's decision engine weighs **yield spread vs. LI.FI bridge cost** — a migration only happens when the math makes sense:

```
if (target_apy - current_apy) * days_to_recoup > bridge_cost:
    migrate()  # via LI.FI
else:
    hold()     # not worth the toll
```

---

## Marco's Personality

Marco isn't just a bot — he's a **character**. Every decision gets a journal entry written by Claude in Marco's voice:

- **Restless** — Never stays on one chain too long
- **Pragmatic** — Only moves when the numbers work
- **Opinionated** — Has strong views on chains and protocols
- **Risk-aware** — Never concentrates more than 40% on one chain
- **Journaling** — Documents everything like a travel diary

---

## Sample Output

```
[14:32:01] Marco the Nomad waking up...
[14:32:01] Chains: [8453, 42161, 10, 137] | Min TVL: $500,000 | Min APY: 3.0%
[14:32:02] Scanning yields...
[14:32:03] Found 12 opportunities. Top 5:
  USDC on Base (chain 8453) | APY: 7.14% | TVL: $45,200,000 | Project: aave-v3
  USDC on Arbitrum (chain 42161) | APY: 5.82% | TVL: $38,100,000 | Project: aave-v3
  USDT-USDC on Optimism (chain 10) | APY: 4.91% | TVL: $12,400,000 | Project: velodrome
[14:32:04] Getting bridge quotes...
  -> Arbitrum: $0.42
  -> Optimism: $0.38
[14:32:05] Marco is thinking...
[14:32:06] Decision: HOLD
[14:32:06] Journal: Already sitting pretty on Base at 7.14%. Arb and OP
           aren't offering enough to justify the toll. I'll wait. Patience
           is a position too...
```

---

## Quick Start

```bash
# Clone
git clone https://github.com/aarontekluuu/marco-nomad.git
cd marco-nomad

# Install
pip install -r requirements.txt

# Configure
cp .env.template .env
# Add your ANTHROPIC_API_KEY (required)
# Add LIFI_API_KEY (optional, for higher rate limits)
# Add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (optional, for notifications)

# Run once
python marco.py --once

# Run continuous (hourly cycles)
python marco.py
```

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `SCAN_CHAINS` | `8453,42161,10,137` | Chain IDs to scan (Base, Arb, OP, Polygon) |
| `MIN_TVL_USD` | `500000` | Skip pools under this TVL |
| `MIN_APY` | `3.0` | Minimum APY threshold |
| `MAX_BRIDGE_COST_PCT` | `2.0` | Max bridge cost as % of position |
| `POSITION_SIZE_USD` | `100` | Simulated position size |
| `DEMO_MODE` | `true` | Simulate moves (no real txns) |
| `LOOP_INTERVAL` | `3600` | Seconds between cycles |

---

## Tech Stack

- **[LI.FI](https://li.fi)** — Cross-chain bridge quotes, routes, and execution
- **[DefiLlama](https://defillama.com)** — Real-time yield pool data
- **[Claude](https://anthropic.com)** — Decision reasoning + personality engine
- **[python-telegram-bot](https://python-telegram-bot.org)** — Optional notifications
- **Python 3.11+** / asyncio / httpx

---

## File Structure

```
marco-nomad/
├── marco.py           # Main agent loop (CLI entry point)
├── main.py            # Alternative entry with Telegram bot integration
├── brain.py           # Claude-powered decision engine + personality
├── lifi.py            # LI.FI API: quotes, routes, cost calc, execution
├── yield_scanner.py   # DefiLlama yield scanning + filtering
├── wallet.py          # Position state tracking + migration history
├── telegram_bot.py    # Telegram bot for /status, /scan, /migrate commands
├── wallet_state.json  # Current position state
├── requirements.txt   # Python dependencies
└── .env.template      # Environment variable template
```

---

*Marco never sleeps. He just waits for the next yield.*
