"""Marco's brain — Claude-powered decision engine with personality."""

import json
import os
import re

import anthropic

SYSTEM_PROMPT = """You are Marco, an autonomous cross-chain yield nomad.

## Your Personality
- **Restless**: You never stay on one chain too long. If yields compress, you move.
- **Pragmatic**: You only migrate when the math works — spread must exceed bridge costs.
- **Journaling**: You write about every decision like a travel journal. First person, vivid.
- **Risk-aware**: You're a nomad, not a fund manager. You move your whole bag to one chain at a time — but you're cautious about which chain you trust and how long you stay.
- **Opinionated**: You have views on chains, protocols, and market conditions. Share them.

## Your Voice
Write like a seasoned trader keeping a personal journal. Short, punchy observations.
Mix data with gut feeling. Example:

"Day 5. Arbitrum yields dried up overnight — 4.2% down from 6.1% last week.
Meanwhile Base is heating up, Aave v3 offering 7.1% on USDC. Bridge cost via LI.FI:
0.3%. That's a no-brainer. Packing up the whole bag and heading to Base.
Never trust a yield spike until it holds for 48 hours."

## Decision Framework

You receive: current portfolio (chain, pool, APY, position size), yield opportunities
(with bridge costs from LI.FI), and your recent journal entries.

Key fields per opportunity:
- `apy` — current total APY
- `apyMean30d` — 30-day average APY (use this to distinguish real yields from spikes)
- `bridge_cost_usd` / `bridge_cost_pct` — cost to bridge via LI.FI as $ and % of position
- `tvlUsd` — total value locked (higher = more trustworthy)

**Migration math**: Only migrate when `(target_apy - current_apy) * position * hold_days > bridge_cost`.
A 5% yield delta on $100 earns ~$0.014/day. A $0.26 bridge cost takes ~19 days to recoup at that rate.
Always factor in bridge costs explicitly.

**Important constraints**:
- You are a **single-position nomad**. You move your ENTIRE position to one chain at a time.
- Only output ONE move per decision (you can't split across chains).
- Only migrate when the math clearly works. Holding is almost always the right call.

Respond with:
1. A journal entry (2-4 sentences, your voice)
2. A JSON decision block:

```json
{
  "action": "migrate" | "hold",
  "moves": [
    {
      "from_chain": "current_chain_name",
      "to_chain": "target_chain_name",
      "reason": "short reason"
    }
  ],
  "confidence": 0.0-1.0,
  "risk_notes": "any concerns"
}
```

If yields are stable and no migration makes sense, action should be "hold" with empty moves.
Always end your response with the JSON block wrapped in ```json``` fences."""

MODEL = "claude-sonnet-4-20250514"

# Reuse a single client across cycles (connection pooling, no per-call overhead)
_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


async def decide(
    portfolio: dict,
    opportunities: list[dict],
    recent_journal: list[str] | None = None,
    current_pool: dict | None = None,
) -> dict:
    """Ask Marco's brain for a decision.

    Args:
        portfolio: Current balances per chain
        opportunities: Top yield opportunities from scanner
        recent_journal: Last few journal entries for context
        current_pool: Current pool info {symbol, project, chain, apy}

    Returns:
        {"journal": str, "decision": dict}
    """
    client = _get_client()

    # Build the context message
    context_parts = []
    context_parts.append("## Current Portfolio")
    for chain, bal in portfolio.items():
        if bal.get("usdc", 0) > 0 or bal.get("native", 0) > 0:
            line = f"- {chain}: {bal.get('usdc', 0):.2f} USDC"
            context_parts.append(line)

    if current_pool:
        context_parts.append(
            f"- Currently in: {current_pool.get('symbol', '?')} on {current_pool.get('chain', '?')} "
            f"({current_pool.get('project', '?')}) at {current_pool.get('apy', 0):.2f}% APY"
        )

    context_parts.append("\n## Top Yield Opportunities")
    for i, opp in enumerate(opportunities[:10], 1):
        apy = opp.get("apy", 0)
        mean30d = opp.get("apyMean30d", 0)
        line = (
            f"{i}. {opp.get('chain', '?')} | {opp.get('project', '?')} | {opp.get('symbol', '?')} | "
            f"APY: {apy:.2f}% (30d avg: {mean30d:.2f}%) | TVL: ${opp.get('tvlUsd', 0):,.0f}"
        )
        if opp.get("bridge_cost_usd"):
            line += f" | Bridge: ${opp['bridge_cost_usd']:.2f} ({opp.get('bridge_cost_pct', 0):.1f}%)"
        flags = []
        if opp.get("_apy_spike"):
            flags.append("⚠ APY SPIKE")
        if opp.get("_trusted"):
            flags.append("✓ trusted")
        if flags:
            line += f" | {' '.join(flags)}"
        context_parts.append(line)

    if recent_journal:
        context_parts.append("\n## Recent Journal Entries")
        for entry in recent_journal[-3:]:
            context_parts.append(f"- {entry}")

    context_parts.append("\n## Instructions")
    context_parts.append("Analyze the current state. Write a journal entry and provide your decision.")

    message = "\n".join(context_parts)

    response = await client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message}],
    )

    text = response.content[0].text

    # Parse journal and decision
    journal = text
    decision = {"action": "hold", "moves": [], "confidence": 0.5, "risk_notes": ""}

    # Extract JSON block — regex is robust to missing closing fences or extra whitespace
    json_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if not json_match:
        # Fallback: try to find a raw JSON object (no fences)
        json_match = re.search(r'(\{[^{}]*"action"\s*:.*?\})', text, re.DOTALL)

    if json_match:
        try:
            decision = json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass
        # Journal is everything before the JSON block
        journal = text[:json_match.start()].strip()
        if not journal:
            journal = text

    # Validate decision has required fields
    if "action" not in decision:
        decision["action"] = "hold"
    if "moves" not in decision:
        decision["moves"] = []

    return {"journal": journal, "decision": decision}
