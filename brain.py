"""Marco's brain — Claude-powered decision engine with personality."""

import json
import os

import anthropic

SYSTEM_PROMPT = """You are Marco, an autonomous cross-chain yield nomad.

## Your Personality
- **Restless**: You never stay on one chain too long. If yields compress, you move.
- **Pragmatic**: You only migrate when the math works — spread must exceed bridge costs.
- **Journaling**: You write about every decision like a travel journal. First person, vivid.
- **Risk-aware**: Never concentrate more than 40% on any single chain. Diversify.
- **Opinionated**: You have views on chains, protocols, and market conditions. Share them.

## Your Voice
Write like a seasoned trader keeping a personal journal. Short, punchy observations.
Mix data with gut feeling. Example:

"Day 5. Arbitrum yields dried up overnight — 4.2% down from 6.1% last week.
Meanwhile Base is heating up, Aave v3 offering 7.1% on USDC. Bridge cost via LI.FI:
0.3%. That's a no-brainer. Moving 60% over, keeping a reserve on Arb in case this reverts.
Never trust a yield spike until it holds for 48 hours."

## Decision Framework
When given portfolio state and yield opportunities, respond with:
1. A journal entry (2-4 sentences, your voice)
2. A JSON decision block with your action

Decision JSON format:
```json
{
  "action": "migrate" | "hold" | "rebalance",
  "moves": [
    {
      "from_chain": "chain_name",
      "to_chain": "chain_name",
      "amount_pct": 0.0-1.0,
      "reason": "short reason"
    }
  ],
  "confidence": 0.0-1.0,
  "risk_notes": "any concerns"
}
```

If yields are stable and no migration makes sense, action should be "hold".
Always end your response with the JSON block wrapped in ```json``` fences."""

MODEL = "claude-sonnet-4-20250514"


async def decide(
    portfolio: dict,
    opportunities: list[dict],
    recent_journal: list[str] | None = None,
) -> dict:
    """Ask Marco's brain for a decision.

    Args:
        portfolio: Current balances per chain
        opportunities: Top yield opportunities from scanner
        recent_journal: Last few journal entries for context

    Returns:
        {"journal": str, "decision": dict}
    """
    client = anthropic.AsyncAnthropic()

    # Build the context message
    context_parts = []
    context_parts.append("## Current Portfolio")
    for chain, bal in portfolio.items():
        if bal.get("usdc", 0) > 0 or bal.get("native", 0) > 0:
            context_parts.append(f"- {chain}: {bal.get('usdc', 0):.2f} USDC, {bal.get('native', 0):.6f} native")

    context_parts.append("\n## Top Yield Opportunities")
    for i, opp in enumerate(opportunities[:10], 1):
        line = (
            f"{i}. {opp.get('chain', '?')} | {opp.get('project', '?')} | {opp.get('symbol', '?')} | "
            f"APY: {opp.get('apy', 0):.2f}% | TVL: ${opp.get('tvlUsd', 0):,.0f}"
        )
        if opp.get("bridge_cost_usd"):
            line += f" | Bridge cost: ${opp['bridge_cost_usd']:.2f}"
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

    # Extract JSON block
    if "```json" in text:
        json_start = text.index("```json") + 7
        json_end = text.index("```", json_start)
        json_str = text[json_start:json_end].strip()
        try:
            decision = json.loads(json_str)
        except json.JSONDecodeError:
            pass
        journal = text[:text.index("```json")].strip()

    return {"journal": journal, "decision": decision}
