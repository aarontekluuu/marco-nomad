# Marco the Nomad 🏔️

An autonomous cross-chain yield nomad agent for the [LI.FI Vibeathon](https://li.fi).

Marco roams across blockchains chasing optimal yields while factoring in bridge costs. He journals every migration decision with personality and reasoning.

## How it works

1. **Scan** — Queries DefiLlama for yield pools across configured chains
2. **Quote** — Gets LI.FI bridge quotes for the best opportunities
3. **Decide** — Claude evaluates whether migrating is worth the cost
4. **Journal** — Every decision is logged with Marco's personality and reasoning
5. **Notify** — Optional Telegram notifications with travel diary entries

## Architecture

```
marco.py          — Main agent loop
├── yield_scanner.py  — DefiLlama yield pool scanning
├── lifi.py           — LI.FI cross-chain quotes & cost calc
├── wallet.py         — Position state tracking
└── decision.py       — Claude-powered migration decisions
```

## Setup

```bash
cp .env.template .env
# Fill in your keys
pip install -r requirements.txt
```

## Run

```bash
# Single cycle
python marco.py --once

# Continuous loop (default: hourly)
python marco.py
```

## Config (via .env)

| Variable | Default | Description |
|---|---|---|
| `SCAN_CHAINS` | `8453,42161,10,137,1` | Chain IDs to scan |
| `MIN_TVL_USD` | `500000` | Minimum pool TVL |
| `MIN_APY` | `3.0` | Minimum APY threshold |
| `MAX_BRIDGE_COST_PCT` | `2.0` | Max bridge cost as % of position |
| `POSITION_SIZE_USD` | `100` | Simulated position size |
| `LOOP_INTERVAL` | `3600` | Seconds between scans |

## Built with

- [LI.FI](https://li.fi) — Cross-chain bridging quotes
- [DefiLlama](https://defillama.com) — Yield data
- [Claude](https://anthropic.com) — Decision reasoning
- [python-telegram-bot](https://python-telegram-bot.org) — Notifications
