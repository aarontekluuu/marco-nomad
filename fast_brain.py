"""Fast deterministic brain — instant math for hold/migrate, Claude only for journaling.

Drop-in replacement for brain.py. Same decide() interface, same output format.
Cuts decision time from 30-90s to <5ms for clear decisions.
"""

import asyncio
import json
import os
import random
import tempfile
from datetime import datetime

# Strategy personas — each strategy has a distinct voice
STRATEGY_EMOJI = {"conservative": "🏰", "balanced": "🏜️", "aggressive": "⚡"}
STRATEGY_TITLE = {"conservative": "FORTRESS", "balanced": "NOMAD", "aggressive": "HUNTER"}

# --- CONSERVATIVE: The Fortress Builder ---
# Patient, risk-averse, detail-obsessed, speaks in terms of defense and certainty
PHRASES = {
    "conservative": {
        "hold_close_spread": [
            "Half a percent doesn't breach these walls. The fortress holds.",
            "Not enough edge to justify the risk. I build on certainty, not hope.",
            "Tempting bait. But the 30-day tells me this spread won't last.",
            "The cost of being wrong outweighs the gain. Walls stay up.",
        ],
        "hold_no_opportunity": [
            "Fortress holds. Nothing out there meets the bar.",
            "Scanned every protocol. Nothing passes the fortress test.",
            "Quiet day. The moat is deep and the yield is real. Patience.",
            "No movement worth the risk. The fortress was built for days like this.",
        ],
        "hold_comfortable": [
            "This yield is battle-tested. The 30-day confirms it. Staying.",
            "Deep TVL, audited protocol, steady APY. This is what the fortress was built for.",
            "Solid ground. No reason to chase noise when the foundation holds.",
            "The fortress earns while it sleeps. No complaints.",
        ],
        "hold_spike": [
            "{best_chain} spiking {best_apy:.0f}%? The 30-day says {mean30d:.1f}%. I don't chase mirages.",
            "Spike on {best_chain} — classic trap. The fortress waits for proven yield.",
            "Flashy numbers don't impress stone walls. {best_chain}'s spike will fade.",
        ],
        "migrate": [
            "The fortress relocates. {spread:.1f}% spread on proven ground — this is calculated, not reckless.",
            "New fortification on {to_chain}. {project} has the TVL depth I trust.",
            "Moving with conviction. Break-even in {break_even:.0f} days, then the fortress grows.",
            "The spread is fortress-grade: {spread:.1f}% on audited rails. Executing.",
        ],
        "opener_high": [
            "The fortress earns well on {chain}. {apy:.1f}% on solid ground.",
            "Walls are thick, yield is flowing. {chain} at {apy:.1f}%.",
        ],
        "opener_mid": [
            "Steady returns on {chain}. {apy:.1f}% — not flashy, but proven.",
            "The fortress ticks along at {apy:.1f}% on {chain}. Scanned {n} pools.",
        ],
        "opener_low": [
            "Yields thinning on {chain}. {apy:.1f}% is below fortress standards.",
            "{chain} at {apy:.1f}%. The fortress watches, waits for real opportunity.",
        ],
    },
    # --- BALANCED: The Wandering Nomad ---
    # Pragmatic, curious, dry humor, speaks in travel metaphors
    "balanced": {
        "hold_close_spread": [
            "Close, but the bridge tax eats the edge. Not worth the detour.",
            "Tempting trail, but the math says stay. I've been burned chasing thin spreads.",
            "Half a percent? I've walked longer roads for less. But not today.",
            "The spread whispers, but the bridge cost shouts. Staying put.",
        ],
        "hold_no_opportunity": [
            "Dry season across all four chains. The nomad rests.",
            "Scanned the whole map — nothing worth packing up for.",
            "Market's flat. Sometimes the best move is no move.",
            "Quiet out there. I'll check the horizon next cycle.",
        ],
        "hold_comfortable": [
            "Comfortable camp. Why uproot for a marginal edge?",
            "This spot earns. The 30-day confirms it's not a fluke.",
            "Happy where I am. The yield is real and the trail was long.",
            "Settled in. Good yield, decent TVL, no drama.",
        ],
        "hold_spike": [
            "{best_chain} spiking {best_apy:.0f}%? The 30-day tells the real story: {mean30d:.1f}%. I've seen this movie.",
            "Nice mirage, {best_chain}. That spike will evaporate by tomorrow.",
            "Flashy numbers on {best_chain} but the 30-day average says smoke.",
        ],
        "migrate": [
            "Time to wander. {spread:.1f}% spread — the math clears.",
            "Packing up camp. Break-even in {break_even:.0f} days, then it's pure trail profit.",
            "{from_chain} had its moment. {to_chain} is calling louder now.",
            "LI.FI routing me through. ${bridge_cost:.2f} toll for a {spread:.1f}% upgrade.",
            "{to_chain} {project} caught my eye — {target_apy:.1f}% with real TVL behind it.",
        ],
        "opener_high": [
            "Good camp on {chain}. {project} still pumping {apy:.1f}%.",
            "{chain} at {apy:.1f}% — hard to complain about this spot.",
        ],
        "opener_mid": [
            "Parked on {chain}, {apy:.1f}% ticking away. Scanned {n} pools.",
            "Another cycle on {chain}. {apy:.1f}% isn't flashy but it's honest.",
        ],
        "opener_low": [
            "{chain} yields drying up at {apy:.1f}%. Eyes on the horizon.",
            "Restless on {chain}. {apy:.1f}% barely justifies the camp.",
        ],
    },
    # --- AGGRESSIVE: The Yield Hunter ---
    # Hungry, confident, impatient, speaks in hunting/combat metaphors
    "aggressive": {
        "hold_close_spread": [
            "Too small. I'm hunting bigger game than {spread:.1f}%.",
            "That spread's a snack, not a meal. Need more alpha.",
            "Barely worth the gas. I want blood-pumping spreads, not scraps.",
            "The hunter waits for the kill shot. This ain't it.",
        ],
        "hold_no_opportunity": [
            "Nothing moving. Even the hunter has to wait sometimes.",
            "Dead market. Every chain is flat. Reloading for next cycle.",
            "The prey went underground. Yields flat everywhere. Patience, predator.",
            "Scanned everything — all quiet. The hunt continues.",
        ],
        "hold_comfortable": [
            "Locked onto good yield. But always watching for the next big one.",
            "Eating well at {apy:.1f}%. But the hunter never sleeps.",
            "This is a good kill. Feasting while I scout the next target.",
            "Strong position. But the moment something bigger appears, I'm gone.",
        ],
        "hold_spike": [
            "{best_chain} pumping {best_apy:.0f}%? Smells like bait. 30-day says {mean30d:.1f}%. Pass.",
            "Spike on {best_chain}? I've seen faster traps. The 30-day is my truth.",
            "Even the hunter knows when the prey is poisoned. {best_chain} spike is fake.",
        ],
        "migrate": [
            "That spread is fire. {spread:.1f}% — moving NOW.",
            "The hunt pays off. {to_chain} {project} at {target_apy:.1f}%. Locked in.",
            "Break-even in {break_even:.0f} days? That's a blink. Shipping it.",
            "{from_chain} is dead weight. {to_chain} is where the alpha lives now.",
            "No hesitation. ${bridge_cost:.2f} bridge for {spread:.1f}% edge? The hunter strikes.",
        ],
        "opener_high": [
            "Feasting on {chain}. {apy:.1f}% — this is what the hunt is for.",
            "The kill is good. {chain} at {apy:.1f}%. Watching for the next.",
        ],
        "opener_mid": [
            "Decent yield on {chain} at {apy:.1f}%. But the hunter wants more.",
            "{chain} holding {apy:.1f}%. Acceptable. Not exceptional. Scanning {n} pools.",
        ],
        "opener_low": [
            "{chain} at {apy:.1f}%? Starving. Need to find the next kill.",
            "Hungry on {chain}. {apy:.1f}% is beneath the hunter. Scanning hard.",
        ],
    },
}

CLAUDE_CLI = os.getenv("CLAUDE_CLI_PATH", os.path.expanduser("~/.local/bin/claude"))


def _effective_apy(opp: dict) -> float:
    """Return apyMean30d if spike detected, else spot apy."""
    if opp.get("_apy_spike"):
        return opp.get("apyMean30d", opp.get("apy", 0))
    return opp.get("apy", 0)


def _score_opportunity(
    opp: dict,
    current_apy: float,
    position_usd: float,
    expected_hold_days: int,
    min_spread_pct: float,
) -> dict:
    """Score a single opportunity. Returns scoring dict."""
    eff_apy = _effective_apy(opp)
    spread = eff_apy - current_apy
    bridge_cost = opp.get("bridge_cost_usd", 0) or 0
    bridge_pct = opp.get("bridge_cost_pct", 0) or 0

    # Free rebalance (same chain, same token, different pool)
    if opp.get("_move_type") == "rebalance":
        bridge_cost = 0
        bridge_pct = 0

    daily_gain = (spread / 100) * position_usd / 365 if spread > 0 else 0
    break_even_days = bridge_cost / daily_gain if daily_gain > 0 else float("inf")
    net_payoff = daily_gain * expected_hold_days - bridge_cost

    # Trust boost
    if opp.get("_trusted"):
        net_payoff *= 1.1

    # LP pair penalty (impermanent loss risk)
    if opp.get("_multi_asset"):
        net_payoff *= 0.9

    # Yield trend adjustments (from historical DB)
    # Use additive adjustments so negative payoffs aren't inverted by multiplication
    trend = opp.get("_trend")
    if trend and net_payoff != 0:
        trend_adj = 0.0
        if trend.get("is_rising"):
            trend_adj += abs(net_payoff) * 0.1  # Rising APY = bonus
        elif trend.get("slope", 0) < -0.5:
            trend_adj -= abs(net_payoff) * 0.2  # Falling fast = penalty
        if trend.get("volatility", 0) > 25:
            trend_adj -= abs(net_payoff) * 0.1  # Volatile = risky
        if trend.get("tvl_change_pct", 0) < -15:
            trend_adj -= abs(net_payoff) * 0.15  # Capital fleeing
        net_payoff += trend_adj

    return {
        "opp": opp,
        "effective_apy": eff_apy,
        "spread": spread,
        "bridge_cost": bridge_cost,
        "bridge_pct": bridge_pct,
        "daily_gain": daily_gain,
        "break_even_days": break_even_days,
        "net_payoff": net_payoff,
        "min_spread_met": spread >= min_spread_pct,
    }


def _calc_confidence(
    net_payoff: float,
    threshold: float,
    risk_score: float,
    is_trusted: bool,
    spread_pct: float,
    trend: dict | None = None,
) -> float:
    """Deterministic confidence from signal strength."""
    if net_payoff <= 0:
        return 0.0
    # Base confidence from how far above threshold (caps at 0.5)
    base = min(net_payoff / (threshold * 2), 0.5) if threshold > 0 else 0.5
    # Risk score contribution (0-0.3)
    risk_contrib = (risk_score / 100) * 0.3 if risk_score else 0.15
    # Trust bonus
    trust = 0.1 if is_trusted else 0.0
    # Spread clarity
    spread_bonus = 0.1 if spread_pct > 5.0 else 0.0
    # Trend bonus/penalty
    trend_adj = 0.0
    if trend:
        if trend.get("is_stable"):
            trend_adj += 0.05
        if trend.get("is_rising"):
            trend_adj += 0.05
        if trend.get("slope", 0) < -0.5:
            trend_adj -= 0.1
    return min(max(base + risk_contrib + trust + spread_bonus + trend_adj, 0.0), 1.0)


def _check_limits(opportunities: list[dict], limits: list[dict]) -> dict | None:
    """Check if any opportunity matches an active limit order."""
    for limit in limits:
        for opp in opportunities:
            if (
                opp.get("chain", "").lower() == limit.get("chain", "").lower()
                and _effective_apy(opp) >= limit.get("min_apy", float("inf"))
            ):
                return opp
    return None


def _build_hold_journal(
    current_pool: dict | None,
    best_scored: dict | None,
    num_scanned: int,
    strategy: str = "balanced",
) -> str:
    """Strategy-aware hold journal — persona changes with risk profile."""
    chain = current_pool.get("chain", "somewhere") if current_pool else "somewhere"
    apy = current_pool.get("apy", 0) if current_pool else 0
    project = current_pool.get("project", "") if current_pool else ""
    p = PHRASES.get(strategy, PHRASES["balanced"])

    # Pick opening based on current APY level
    fmt = {"chain": chain, "apy": apy, "project": project, "n": num_scanned}
    if apy >= 15:
        opener = random.choice(p["opener_high"]).format(**fmt)
    elif apy >= 8:
        opener = random.choice(p["opener_mid"]).format(**fmt)
    else:
        opener = random.choice(p["opener_low"]).format(**fmt)

    if not best_scored or best_scored["spread"] <= 0:
        phrase = random.choice(p["hold_no_opportunity"])
        return f"{opener} {phrase}"

    best = best_scored["opp"]
    best_chain = best.get("chain", "?")
    best_project = best.get("project", "?")

    # Pick closing phrase based on WHY we're holding
    if best.get("_apy_spike"):
        phrase = random.choice(p["hold_spike"]).format(
            best_apy=best_scored["effective_apy"],
            best_chain=best_chain,
            mean30d=best.get("apyMean30d", 0),
        )
    elif best_scored["spread"] < 2.0:
        phrase = random.choice(p["hold_close_spread"]).format(
            spread=best_scored["spread"],
        )
    elif apy >= 10:
        phrase = random.choice(p["hold_comfortable"]).format(apy=apy)
    else:
        phrase = random.choice(p["hold_close_spread"] + p["hold_no_opportunity"]).format(
            spread=best_scored.get("spread", 0),
        )

    # Trend commentary
    trend_note = ""
    trend = best.get("_trend")
    if trend and trend.get("data_points", 0) >= 3:
        if trend.get("slope", 0) < -0.5:
            trend_note = " APY falling fast over the week — risky."
        elif trend.get("is_rising"):
            trend_note = " Momentum is real — APY trending up."
        elif trend.get("volatility", 0) > 25:
            trend_note = " Volatile yield — could vanish tomorrow."

    return (
        f"{opener} Best I found: {best_chain} {best_project} at "
        f"{best_scored['effective_apy']:.1f}% — {best_scored['spread']:.1f}% spread, "
        f"${best_scored['bridge_cost']:.2f} bridge. {phrase}{trend_note}"
    )


def _build_migrate_journal(
    current_pool: dict | None,
    best_scored: dict,
    strategy: str = "balanced",
) -> str:
    """Strategy-aware migrate journal."""
    from_chain = current_pool.get("chain", "?") if current_pool else "?"
    current_apy = current_pool.get("apy", 0) if current_pool else 0
    best = best_scored["opp"]
    to_chain = best.get("chain", "?")
    project = best.get("project", "?")
    bridge_tool = best.get("bridge_tool", "LI.FI")
    p = PHRASES.get(strategy, PHRASES["balanced"])

    fmt = {
        "spread": best_scored["spread"],
        "break_even": best_scored["break_even_days"],
        "from_chain": from_chain,
        "to_chain": to_chain,
        "project": project,
        "target_apy": best_scored["effective_apy"],
        "bridge_cost": best_scored["bridge_cost"],
    }

    # Context-aware opening
    if current_apy < 5:
        opener = f"{from_chain} at {current_apy:.1f}% — can't stay. {to_chain} offering {best_scored['effective_apy']:.1f}%."
    elif best_scored["spread"] > 10:
        opener = f"Massive {best_scored['spread']:.0f}% spread opened. {to_chain} {project} is the play."
    else:
        opener = f"{from_chain} at {current_apy:.1f}% vs {to_chain} at {best_scored['effective_apy']:.1f}%. Spread is clean."

    phrase = random.choice(p["migrate"]).format(**fmt)

    # Trend commentary
    trend_note = ""
    trend = best.get("_trend")
    if trend and trend.get("data_points", 0) >= 3:
        if trend.get("is_rising"):
            trend_note = f" Yield climbing for {trend['data_points']} snapshots — confirmed."
        elif trend.get("is_stable"):
            trend_note = " Stable history backs this up."

    return (
        f"{opener} Bridge via LI.FI ({bridge_tool}): ${best_scored['bridge_cost']:.2f} "
        f"({best_scored['bridge_pct']:.1f}%). {phrase}{trend_note}"
    )


def _request_claude_journal_async(context: str, decision_summary: str):
    """Fire-and-forget background Claude call for richer journal. Non-blocking."""

    async def _call():
        try:
            prompt = (
                "Write a 2-3 sentence journal entry in Marco's voice (a cross-chain yield nomad). "
                "First person, punchy, mentions LI.FI when bridging. "
                f"Facts: {context}. Decision: {decision_summary}."
            )
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    CLAUDE_CLI, "-p", prompt,
                    "--output-format", "text",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                ),
                timeout=60,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            # Could update journal.json here if desired
            return stdout.decode().strip()
        except Exception:
            pass  # Background — never crash the cycle

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_call())
    except RuntimeError:
        pass  # No event loop — skip async journal


def _get_strategy_params() -> dict:
    """Load FastBrain thresholds from the active strategy profile."""
    try:
        from wallet import load_state, get_strategy, STRATEGY_PROFILES
        state = load_state()
        profile_name = get_strategy(state)
        profile = STRATEGY_PROFILES.get(profile_name, STRATEGY_PROFILES["balanced"])
    except Exception:
        profile_name = "balanced"
        profile = {}

    return {
        "strategy": profile_name,
        "expected_hold_days": profile.get("expected_hold_days", 7),
        "min_net_payoff_usd": profile.get("min_net_payoff_usd", 0.20),
        "min_spread_pct": profile.get("min_spread_pct", 1.5),
        "gray_zone_width": profile.get("gray_zone_width", 0.4),
    }


async def decide(
    portfolio: dict,
    opportunities: list[dict],
    recent_journal: list[str] | None = None,
    current_pool: dict | None = None,
    bridge_cost_cap_pct: float = 2.0,
) -> dict:
    """Drop-in replacement for brain.decide(). Returns {"journal": str, "decision": dict}."""
    params = _get_strategy_params()
    strategy = params["strategy"]
    expected_hold_days = params["expected_hold_days"]
    min_net_payoff = params["min_net_payoff_usd"]
    min_spread = params["min_spread_pct"]
    gray_width = params["gray_zone_width"]

    current_apy = current_pool.get("apy", 0) if current_pool else 0
    position_usd = 0
    for chain_bal in portfolio.values():
        for token in ("usdc", "usdt", "dai", "usdbc"):
            position_usd += chain_bal.get(token, 0)

    # Score all opportunities
    scored = []
    for opp in opportunities[:10]:
        # Skip if bridge cost exceeds cap
        if (opp.get("bridge_cost_pct") or 0) > bridge_cost_cap_pct:
            continue
        s = _score_opportunity(opp, current_apy, position_usd, expected_hold_days, min_spread)
        scored.append(s)

    # Sort by net payoff descending
    scored.sort(key=lambda x: x["net_payoff"], reverse=True)
    best = scored[0] if scored else None

    # Check limit orders
    limits = []
    try:
        from wallet import load_state, get_limits
        limits = get_limits(load_state())
    except Exception:
        pass

    limit_match = _check_limits(opportunities[:10], limits) if limits else None
    if limit_match and best:
        # Boost the limit-matching opportunity
        for s in scored:
            if s["opp"] is limit_match:
                s["net_payoff"] *= 1.5
                break
        scored.sort(key=lambda x: x["net_payoff"], reverse=True)
        best = scored[0]

    # Decision logic
    if not best or best["net_payoff"] <= 0 or not best["min_spread_met"]:
        # Clear HOLD
        journal = _build_hold_journal(current_pool, best, len(opportunities), strategy)
        return {
            "journal": journal,
            "decision": {
                "action": "hold",
                "moves": [],
                "confidence": 0.5,
                "risk_notes": "",
            },
        }

    # Threshold zones
    threshold = min_net_payoff
    gray_lower = threshold * (1 - gray_width)
    gray_upper = threshold * (1 + gray_width)

    if best["net_payoff"] > gray_upper:
        # Clear MIGRATE
        best_opp = best["opp"]
        confidence = _calc_confidence(
            best["net_payoff"],
            threshold,
            best_opp.get("_risk_score", 50),
            best_opp.get("_trusted", False),
            best["spread"],
            trend=best_opp.get("_trend"),
        )
        journal = _build_migrate_journal(current_pool, best, strategy)
        move = {
            "from_chain": current_pool.get("chain", "?") if current_pool else "?",
            "to_chain": best_opp.get("chain", "?"),
            "reason": (
                f"{best['spread']:.1f}% spread, ${best['bridge_cost']:.2f} bridge, "
                f"break-even {best['break_even_days']:.0f}d"
            ),
        }
        # Fire async journal for richer version
        _request_claude_journal_async(
            f"{move['from_chain']}→{move['to_chain']}, spread {best['spread']:.1f}%, "
            f"bridge ${best['bridge_cost']:.2f}",
            "migrate",
        )
        return {
            "journal": journal,
            "decision": {
                "action": "migrate",
                "moves": [move],
                "confidence": confidence,
                "risk_notes": f"break-even {best['break_even_days']:.0f}d"
                if best["break_even_days"] < float("inf")
                else "",
            },
        }

    elif best["net_payoff"] >= gray_lower:
        # Gray zone — consult Claude for this decision only
        try:
            result = await _consult_claude_gray_zone(
                current_pool, best, scored[:3], position_usd
            )
            return result
        except Exception:
            # Claude unavailable — conservative hold
            journal = _build_hold_journal(current_pool, best, len(opportunities), strategy)
            return {
                "journal": journal + " (gray zone — Claude offline, defaulting to hold)",
                "decision": {
                    "action": "hold",
                    "moves": [],
                    "confidence": 0.4,
                    "risk_notes": "gray zone, Claude unavailable",
                },
            }
    else:
        # Below gray zone — clear HOLD
        journal = _build_hold_journal(current_pool, best, len(opportunities), strategy)
        return {
            "journal": journal,
            "decision": {
                "action": "hold",
                "moves": [],
                "confidence": 0.5,
                "risk_notes": "",
            },
        }


async def _consult_claude_gray_zone(
    current_pool: dict | None,
    best_scored: dict,
    top_3: list[dict],
    position_usd: float,
) -> dict:
    """Short Claude CLI call for ambiguous decisions. 5-15s instead of 30-90s."""
    best = best_scored["opp"]
    current_chain = current_pool.get("chain", "?") if current_pool else "?"
    current_apy = current_pool.get("apy", 0) if current_pool else 0

    options_text = "\n".join(
        f"- {s['opp'].get('chain','?')} {s['opp'].get('project','?')} "
        f"at {s['effective_apy']:.1f}% (spread: {s['spread']:.1f}%, "
        f"bridge: ${s['bridge_cost']:.2f}, break-even: {s['break_even_days']:.0f}d, "
        f"net payoff: ${s['net_payoff']:.3f})"
        for s in top_3
        if s["net_payoff"] > 0
    )

    prompt = (
        f"You are Marco, a cross-chain yield nomad. Quick decision needed.\n"
        f"Current: {current_chain} at {current_apy:.1f}% APY, ${position_usd:.2f} position.\n"
        f"Options:\n{options_text}\n\n"
        f"The math is close — is it worth moving? Reply with a 1-2 sentence journal entry "
        f"then a JSON block: {{\"action\": \"migrate\"|\"hold\", \"moves\": [...], "
        f"\"confidence\": 0.0-1.0, \"risk_notes\": \"...\"}}"
    )

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    proc = await asyncio.wait_for(
        asyncio.create_subprocess_exec(
            CLAUDE_CLI, "-p", prompt,
            "--output-format", "text",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        ),
        timeout=30,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    text = stdout.decode().strip()

    # Parse same way as brain.py
    import re

    journal = text
    decision = {"action": "hold", "moves": [], "confidence": 0.5, "risk_notes": "gray zone"}

    json_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if not json_match:
        json_match = re.search(r'(\{.*"action"\s*:.*\})', text, re.DOTALL)
    if json_match:
        try:
            decision = json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass
        journal = text[: json_match.start()].strip() or text

    if "action" not in decision:
        decision["action"] = "hold"
    if "moves" not in decision:
        decision["moves"] = []

    return {"journal": journal, "decision": decision}
