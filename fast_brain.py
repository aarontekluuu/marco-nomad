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
            "A {spread:.1f}% spread after bridge costs? The fortress doesn't move for crumbs.",
            "Stone walls don't shake at {spread:.1f}% net. Come back with real conviction.",
            "I've seen better spreads evaporate overnight. {spread:.1f}% doesn't clear my threshold.",
            "The bridge toll eats the edge. What remains isn't worth the disruption.",
            "When the spread is this thin, patience is the strategy.",
            "The moat is expensive to cross. {spread:.1f}% doesn't pay the toll.",
            "Marginal edge, real risk. The fortress was built to resist exactly this temptation.",
            "Close isn't enough. The fortress moves only on certainty.",
        ],
        "hold_no_opportunity": [
            "Fortress holds. Nothing out there meets the bar.",
            "Scanned every protocol. Nothing passes the fortress test.",
            "Quiet day. The moat is deep and the yield is real. Patience.",
            "No movement worth the risk. The fortress was built for days like this.",
            "Every candidate failed the audit. Staying put.",
            "Dry spell across the chains. The fortress endures.",
            "Nothing clears the bar today. Conservation is the strategy.",
            "The map shows no worthy destination. Fortress holds position.",
            "Checked the perimeter. No threats, no opportunities. Earn and rest.",
            "Market quiet. The fortress doesn't move without reason.",
            "All candidates rejected. Yield quality is thin across the board.",
            "Dead market. The fortress earns where it stands.",
        ],
        "hold_comfortable": [
            "This yield is battle-tested. The 30-day confirms it. Staying.",
            "Deep TVL, audited protocol, steady APY. This is what the fortress was built for.",
            "Solid ground. No reason to chase noise when the foundation holds.",
            "The fortress earns while it sleeps. No complaints.",
            "Everything checks out: TVL, APY history, protocol audit. The fortress remains.",
            "Strong fortification. {apy:.1f}% on proven ground is hard to beat.",
            "The 30-day average tells the real story — this yield is sustainable.",
            "Comfortable inside the walls. The yield is real, the protocol is solid.",
            "No cracks in the foundation. Staying and compounding.",
            "Why move a working fortress? {apy:.1f}% on stable ground.",
            "Security, yield, history — all check out. This is home.",
            "I could move, but I don't move without purpose. This position earns well.",
        ],
        "hold_spike": [
            "{best_chain} spiking {best_apy:.0f}%? The 30-day says {mean30d:.1f}%. I don't chase mirages.",
            "Spike on {best_chain} — classic trap. The fortress waits for proven yield.",
            "Flashy numbers don't impress stone walls. {best_chain}'s spike will fade.",
            "{best_chain} at {best_apy:.0f}%? The 30-day is {mean30d:.1f}%. I'll take the honest number.",
            "Experienced fortress builders know: spikes like {best_chain}'s are mercenary capital. It flees.",
            "The 30-day is my north star. {best_apy:.0f}% spot vs {mean30d:.1f}% sustained — easy call.",
            "{best_chain}'s spike doesn't tempt me. The fortress doesn't chase ghosts.",
            "Seen it before: {best_apy:.0f}% today, 3% tomorrow. The 30-day tells the truth.",
            "Mercenary yield attracts mercenary capital. When it flees, so does the APY.",
            "A spike is a trap dressed as opportunity. The fortress doesn't enter traps.",
            "{best_chain}'s {best_apy:.0f}% spike is noise. {mean30d:.1f}% 30-day is signal.",
            "Not falling for it. {mean30d:.1f}% is what {best_chain} actually delivers. The spike is theater.",
        ],
        "migrate": [
            "The fortress relocates. {spread:.1f}% spread on proven ground — calculated, not reckless.",
            "New fortification on {to_chain}. {project} has the TVL depth I trust.",
            "Moving with conviction. Break-even in {break_even:.0f} days, then the fortress grows.",
            "The spread is fortress-grade: {spread:.1f}% on audited rails. Executing.",
            "Every migration is deliberate. {to_chain} {project} cleared every gate: TVL, APY, bridge cost.",
            "Relocating with precision: {spread:.1f}% spread, {break_even:.0f}-day payback. The math is sound.",
            "The fortress doesn't wander — it advances. {to_chain} is the advance.",
            "{from_chain} served well. {to_chain} offers {spread:.1f}% more at comparable security.",
            "Calculated relocation: ${bridge_cost:.2f} toll, {break_even:.0f}-day payback. Acceptable cost.",
            "Conservative migration: everything checked, spread is real, risk is bounded. Executing.",
            "The next fortress is on {to_chain}. {project} at {target_apy:.1f}%. Moving in.",
            "Stone walls move rarely. When they do, it's decisive. {to_chain} — here we come.",
        ],
        "opener_high": [
            "The fortress earns well on {chain}. {apy:.1f}% on solid ground.",
            "Walls are thick, yield is flowing. {chain} at {apy:.1f}%.",
            "Strong position on {chain}. {apy:.1f}% — fortress standards met.",
            "Solid {apy:.1f}% on {chain}. The fortress is well-fed.",
            "Battle-tested yield: {apy:.1f}% on {chain}. No cracks in the foundation.",
            "{chain} delivering {apy:.1f}%. Fortress is content.",
        ],
        "opener_mid": [
            "Steady returns on {chain}. {apy:.1f}% — not flashy, but proven.",
            "The fortress ticks along at {apy:.1f}% on {chain}. Scanned {n} pools.",
            "{chain} at {apy:.1f}%. Honest yield, stable protocol.",
            "Holding {apy:.1f}% on {chain}. Checked {n} alternatives.",
            "{apy:.1f}% on {chain}. Not exceptional, but fortress-worthy.",
            "Fortress earns {apy:.1f}% on {chain}. Reliable, not remarkable.",
        ],
        "opener_low": [
            "Yields thinning on {chain}. {apy:.1f}% is below fortress standards.",
            "{chain} at {apy:.1f}%. The fortress watches and waits.",
            "Thin yield on {chain}: {apy:.1f}%. The audit intensifies.",
            "{apy:.1f}% on {chain} is uncomfortable. Checking alternatives.",
            "Below the bar on {chain} at {apy:.1f}%. The search intensifies.",
            "Fortress yield at {apy:.1f}% on {chain} — starting to feel pressure.",
        ],
    },
    # --- BALANCED: The Wandering Nomad ---
    "balanced": {
        "hold_close_spread": [
            "Close, but the bridge tax eats the edge. Not worth the detour.",
            "Tempting trail, but the math says stay. I've been burned chasing thin spreads.",
            "Half a percent? I've walked longer roads for less. But not today.",
            "The spread whispers, but the bridge cost shouts. Staying put.",
            "{spread:.1f}% net — a short walk for a long detour. Pass.",
            "The nomad knows: thin spreads lead to thinner wallets.",
            "Bridge tax cancels the edge. This trail doesn't pay its toll.",
            "Looked at the math twice. {spread:.1f}% net doesn't justify packing up.",
            "A wanderer who chases every shiny trail never gets anywhere. Staying.",
            "Not every opportunity is worth the toll road. This one isn't.",
            "Close enough to tempt, not enough to commit. {spread:.1f}% won't cut it.",
            "The bridge eats the spread. This is math, not sentiment.",
        ],
        "hold_no_opportunity": [
            "Dry season across all four chains. The nomad rests.",
            "Scanned the whole map — nothing worth packing up for.",
            "Market's flat. Sometimes the best move is no move.",
            "Quiet out there. I'll check the horizon next cycle.",
            "Dead landscape. Every trail leads to thin returns.",
            "Nowhere worth going right now. Staying and earning.",
            "The chains are quiet. A smart nomad knows when to rest.",
            "Nothing calling. The current camp earns better than any alternative I found.",
            "Empty trails. Surveyed the entire map — today, home is the best option.",
            "Four chains, zero worthy alternatives. The nomad sits this one out.",
            "Flat across the board. Current camp beats every alternative.",
            "Dead market. Every chain is sleeping. So am I.",
        ],
        "hold_comfortable": [
            "Comfortable camp. Why uproot for a marginal edge?",
            "This spot earns. The 30-day confirms it's not a fluke.",
            "Happy where I am. The yield is real and the trail was long.",
            "Settled in. Good yield, decent TVL, no drama.",
            "The camp is good. Wandering aimlessly isn't the nomad way.",
            "{apy:.1f}% in a proven spot. Could be worse — considerably.",
            "When the camp is working, you don't abandon it for marginal upgrades.",
            "This yield isn't going anywhere. The 30-day says so. Staying.",
            "Good camp, honest APY. The nomad rests easy.",
            "Comfortable and earning. This is the goal — not constant wandering.",
            "The trail was long to get here. Not leaving for scraps.",
            "Sitting on {apy:.1f}% in a spot I trust. The nomad is at peace.",
        ],
        "hold_spike": [
            "{best_chain} spiking {best_apy:.0f}%? The 30-day tells the real story: {mean30d:.1f}%. I've seen this movie.",
            "Nice mirage, {best_chain}. That spike will evaporate by tomorrow.",
            "Flashy numbers on {best_chain} but the 30-day average says smoke and mirrors.",
            "A nomad who chases mirages dies in the desert. {best_chain}'s spike is a mirage.",
            "{best_chain} at {best_apy:.0f}%? I check the 30-day ({mean30d:.1f}%) before I move my camp.",
            "That {best_chain} spike smells like new liquidity that'll leave next week.",
            "Spike today, drought tomorrow. {mean30d:.1f}% is the real {best_chain} yield.",
            "I've packed camp for a mirage before. Never again. {best_chain} won't fool me.",
            "APY spikes attract the herd, then the yield collapses when they leave.",
            "The 30-day on {best_chain} is {mean30d:.1f}%. The spike is irrelevant.",
            "Short-term bait. {best_chain}'s {best_apy:.0f}% will normalize. The nomad is patient.",
            "The best campsites are ones everyone else overlooked. {best_chain}'s spike is too obvious.",
        ],
        "migrate": [
            "Time to wander. {spread:.1f}% spread — the math clears.",
            "Packing up camp. Break-even in {break_even:.0f} days, then pure trail profit.",
            "{from_chain} had its moment. {to_chain} is calling louder now.",
            "LI.FI routing me through. ${bridge_cost:.2f} toll for a {spread:.1f}% upgrade.",
            "{to_chain} {project} caught my eye — {target_apy:.1f}% with real TVL behind it.",
            "The trail is clear: {from_chain} → {to_chain}, break-even in {break_even:.0f} days.",
            "New territory. {to_chain} offers more than the current camp can.",
            "Moving camp with confidence. {spread:.1f}% spread after all costs — worth the walk.",
            "The nomad follows yield, not hype. This spread is real.",
            "{to_chain} has been on my map a while. Now the numbers justify the journey.",
            "Calculated wander: ${bridge_cost:.2f} bridge, {break_even:.0f}-day payback, {spread:.1f}% edge.",
            "Breaking camp. {from_chain} was a good stay. {to_chain} {project} beckons.",
        ],
        "opener_high": [
            "Good camp on {chain}. {project} still pumping {apy:.1f}%.",
            "{chain} at {apy:.1f}% — hard to complain about this spot.",
            "Solid camp on {chain}. {apy:.1f}% keeps the nomad fed.",
            "{apy:.1f}% on {chain}. The trails here were worth walking.",
            "Earning {apy:.1f}% on {chain}. The nomad is comfortable.",
            "High camp on {chain}. {apy:.1f}% — the nomad is satisfied.",
        ],
        "opener_mid": [
            "Parked on {chain}, {apy:.1f}% ticking away. Scanned {n} pools.",
            "Another cycle on {chain}. {apy:.1f}% isn't flashy but it's honest.",
            "Steady camp on {chain} at {apy:.1f}%. Scanned {n} alternatives.",
            "{chain} holding {apy:.1f}%. Decent camp, decent yield.",
            "{apy:.1f}% on {chain}. Not a destination, but a decent rest stop.",
            "{chain} at {apy:.1f}%. Honest yield for an honest nomad.",
        ],
        "opener_low": [
            "{chain} yields drying up at {apy:.1f}%. Eyes on the horizon.",
            "Restless on {chain}. {apy:.1f}% barely justifies the camp.",
            "Thin returns on {chain}: {apy:.1f}%. The nomad is scanning hard.",
            "{apy:.1f}% on {chain} is below what the trail promises.",
            "Poor camp on {chain} at {apy:.1f}%. Time to scout better ground.",
            "The yields are drying up. {chain} at {apy:.1f}% — eyes open.",
        ],
    },
    # --- AGGRESSIVE: The Yield Hunter ---
    "aggressive": {
        "hold_close_spread": [
            "Too small. I'm hunting bigger game than {spread:.1f}%.",
            "That spread's a snack, not a meal. Need more alpha.",
            "Barely worth the gas. I want blood-pumping spreads, not scraps.",
            "The hunter waits for the kill shot. This ain't it.",
            "{spread:.1f}%? The hunter needs triple that minimum to justify moving.",
            "Paw at scraps, lose the feast. I don't move for {spread:.1f}%.",
            "Thin prey. Not worth the chase. Holding position.",
            "Small game. The hunter is built for larger kills.",
            "Below my threshold. The hunter doesn't exhaust himself on small targets.",
            "Not enough juice. {spread:.1f}% after bridge costs is beneath me.",
            "The hunt demands selectivity. {spread:.1f}% isn't worth the ammunition.",
            "I've passed on bigger spreads when risk wasn't worth it. {spread:.1f}% definitely isn't.",
        ],
        "hold_no_opportunity": [
            "Nothing moving. Even the hunter has to wait sometimes.",
            "Dead market. Every chain is flat. Reloading for next cycle.",
            "The prey went underground. Yields flat everywhere. Patience, predator.",
            "Scanned everything — all quiet. The hunt continues next cycle.",
            "Flat across the board. The hunter rests but stays alert.",
            "No alpha anywhere. Market's dead. Waiting for the next opportunity.",
            "Dead landscape. Every lead dried up. The hunter is patient.",
            "Even the best hunter comes home empty sometimes. Today is that day.",
            "All targets below threshold. Conserving energy for the real move.",
            "Zero worthy prey this cycle. The hunter waits.",
            "Nothing to chase. Sometimes letting the prey come to you is the move.",
            "Market silence. The hunter studies terrain for the next strike.",
        ],
        "hold_comfortable": [
            "Locked onto good yield. But always watching for the next big one.",
            "Eating well at {apy:.1f}%. But the hunter never sleeps.",
            "This is a good kill. Feasting while I scout the next target.",
            "Strong position. But the moment something bigger appears, I'm gone.",
            "{apy:.1f}% is solid prey. Holding until a bigger target materializes.",
            "Feasting on this yield. The hunt was worth it.",
            "Current kill holding strong at {apy:.1f}%. No reason to abandon a fresh feed.",
            "The hunter is satisfied — for now. The hunger never fully fades.",
            "Good position, real yield. Staying until the math demands otherwise.",
            "The prey is fat and the kill is holding. Why chase scraps when feasting?",
            "Strong camp. {apy:.1f}% keeps the hunter well-fed and ready.",
            "Not moving from a strong position until something clearly better appears.",
        ],
        "hold_spike": [
            "{best_chain} pumping {best_apy:.0f}%? Smells like bait. 30-day says {mean30d:.1f}%. Pass.",
            "Spike on {best_chain}? I've seen faster traps. The 30-day is my truth.",
            "Even the hunter knows when the prey is poisoned. {best_chain} spike is fake.",
            "{best_chain} at {best_apy:.0f}%? Poison bait. The 30-day ({mean30d:.1f}%) tells the truth.",
            "Trap. {best_chain} spike is mercenary yield — it leaves as fast as it came.",
            "The hunter doesn't take poisoned bait. {best_chain}'s spike is exactly that.",
            "Big yield, short fuse. {best_chain}'s {best_apy:.0f}% against {mean30d:.1f}% 30-day? Bait.",
            "Sharp instinct: that {best_chain} spike smells wrong. 30-day confirms it.",
            "I don't eat bait. {best_chain}'s spike collapses when whales rotate out.",
            "Experienced predator lesson: a spike like {best_chain}'s is a warning, not an invitation.",
            "{best_apy:.0f}% on {best_chain} disappears fast. {mean30d:.1f}% 30-day is what you actually get.",
            "Flashy doesn't mean profitable. {best_chain}'s spike lasts days, bridge cost is permanent.",
        ],
        "migrate": [
            "That spread is fire. {spread:.1f}% — moving NOW.",
            "The hunt pays off. {to_chain} {project} at {target_apy:.1f}%. Locked in.",
            "Break-even in {break_even:.0f} days? That's a blink. Shipping it.",
            "{from_chain} is dead weight. {to_chain} is where the alpha lives now.",
            "No hesitation. ${bridge_cost:.2f} bridge for {spread:.1f}% edge? The hunter strikes.",
            "The kill shot: {spread:.1f}% spread, {break_even:.0f}-day payback. Moving without regret.",
            "This is what the hunt is for. {to_chain} {project} — {target_apy:.1f}% and I'm there.",
            "Full aggression. {from_chain} had its run. {to_chain} is the new kill zone.",
            "The hunter doesn't deliberate at the kill shot. ${bridge_cost:.2f} bridge, {spread:.1f}% edge. Done.",
            "Locked onto {to_chain}. {target_apy:.1f}%, break-even {break_even:.0f} days. The math wins.",
            "Aggressive move: {from_chain} → {to_chain} for {spread:.1f}% more. Bridge is cheap vs the upside.",
            "Called it. {to_chain} was on my radar. Now the numbers confirm it. Striking.",
        ],
        "opener_high": [
            "Feasting on {chain}. {apy:.1f}% — this is what the hunt is for.",
            "The kill is good. {chain} at {apy:.1f}%. Watching for the next.",
            "Strong yield on {chain}: {apy:.1f}%. The hunter feasts.",
            "{apy:.1f}% on {chain}. This is a quality kill.",
            "Locked in on {chain} at {apy:.1f}%. The hunt delivered.",
            "{project} on {chain} paying {apy:.1f}%. The hunter is satisfied — for now.",
        ],
        "opener_mid": [
            "Decent yield on {chain} at {apy:.1f}%. But the hunter wants more.",
            "{chain} holding {apy:.1f}%. Acceptable. Not exceptional. Scanning {n} pools.",
            "Mid-tier prey on {chain}: {apy:.1f}%. Eating but still hunting.",
            "{apy:.1f}% on {chain}. Not bad, but the hunter is always scanning.",
            "Adequate position on {chain} at {apy:.1f}%. Scanning {n} alternatives.",
            "{chain} at {apy:.1f}%. A rest stop, not a destination.",
        ],
        "opener_low": [
            "{chain} at {apy:.1f}%? Starving. Need to find the next kill.",
            "Hungry on {chain}. {apy:.1f}% is beneath the hunter. Scanning hard.",
            "Starvation yield: {chain} at {apy:.1f}%. The hunt gets serious.",
            "{apy:.1f}% on {chain}. The hunter grows impatient.",
            "Lean times on {chain} at {apy:.1f}%. The kill must come soon.",
            "Low prey density: {chain} at {apy:.1f}%. The hunter needs new grounds.",
        ],
    },
}

# --- Phrase dedup engine ---
_phrase_history: list[str] = []
_DEDUP_WINDOW = 8


def _pick_phrase(phrases: list[str]) -> str:
    """Pick a phrase template, avoiding the last _DEDUP_WINDOW used templates."""
    global _phrase_history
    recent = set(_phrase_history[-_DEDUP_WINDOW:])
    available = [p for p in phrases if p not in recent]
    if not available:
        available = phrases  # All used — reset window
    chosen = random.choice(available)
    _phrase_history.append(chosen)
    return chosen


def _parse_cycle_context(recent_journal: list[str] | None) -> dict:
    """Extract hold streak and time-of-day context from recent journal entries."""
    hour = datetime.now().hour
    hold_streak = 0
    if recent_journal:
        for entry in reversed(recent_journal):
            e_lower = entry.lower()
            if any(kw in e_lower for kw in ("migrat", "moving camp", "breaking camp", "striking", "move now", "shipping it")):
                break
            hold_streak += 1
    return {"hour": hour, "hold_streak": hold_streak}


def _streak_note(hold_streak: int, strategy: str) -> str:
    """One-liner referencing consecutive hold cycles. Returns '' if streak < 2."""
    if hold_streak < 2:
        return ""
    # Multiple options per streak count — _pick_phrase ensures variety
    notes = {
        "conservative": [
            f"Hold #{hold_streak}.",
            f"{hold_streak} cycles steady.",
            f"Cycle {hold_streak} — same walls, same conviction.",
            f"{hold_streak} in a row. Patience isn't weakness.",
            f"Still here after {hold_streak} scans. That's the point.",
        ],
        "balanced": [
            f"Hold #{hold_streak}.",
            f"{hold_streak} cycles at this camp.",
            f"Cycle {hold_streak} — still the best spot on the map.",
            f"{hold_streak} scans, same conclusion.",
            f"Camp day {hold_streak}. No trail worth the toll yet.",
        ],
        "aggressive": [
            f"Hold #{hold_streak}.",
            f"{hold_streak} cycles waiting.",
            f"Cycle {hold_streak} — the prey hasn't shown itself.",
            f"{hold_streak} dead scans. Coiled tight.",
            f"Dry spell: {hold_streak} cycles. The next kill will be worth it.",
        ],
    }
    options = notes.get(strategy, notes["balanced"])
    return _pick_phrase(options)


def _time_note(hour: int, strategy: str) -> str:
    """Occasional time-of-day flavor line — returned ~35% of the time."""
    if random.random() > 0.35:
        return ""
    if 0 <= hour < 6:
        by_strat = {
            "conservative": ["3am fortress scan.", "Late-night check — markets quiet."],
            "balanced": ["3am — scanning while the market sleeps.", "Dead of night. Quiet chains."],
            "aggressive": ["3am and still hunting.", "Night watch. Yields don't sleep."],
        }
    elif hour < 12:
        by_strat = {
            "conservative": ["Morning protocol check.", "Pre-market fortress scan."],
            "balanced": ["Morning sweep.", "Markets waking up."],
            "aggressive": ["Morning hunt.", "Catching the market early."],
        }
    elif hour < 18:
        by_strat = {
            "conservative": ["Midday fortress review.", "Afternoon check."],
            "balanced": ["Midday scan.", "Afternoon markets."],
            "aggressive": ["Midday hunt.", "Peak hours — watching carefully."],
        }
    else:
        by_strat = {
            "conservative": ["Evening patrol.", "Night fortress check."],
            "balanced": ["Evening sweep.", "Night cycle."],
            "aggressive": ["Evening hunt.", "After-hours scan."],
        }
    options = by_strat.get(strategy, ["Cycle check."])
    return random.choice(options)

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
    # Scale net_payoff relative to position size so small positions aren't permanently stuck
    # Net payoff as fraction of position, then compare against scaled threshold
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
    recent_journal: list[str] | None = None,
) -> str:
    """Strategy-aware hold journal with structural variety and cycle context."""
    chain = current_pool.get("chain", "somewhere") if current_pool else "somewhere"
    apy = current_pool.get("apy", 0) if current_pool else 0
    project = current_pool.get("project", "") if current_pool else ""
    project = project or "protocol"  # Avoid empty {project} in format strings
    p = PHRASES.get(strategy, PHRASES["balanced"])

    # Parse cycle context for streak and time color
    ctx = _parse_cycle_context(recent_journal)

    # Pick opening based on current APY level (dedup-aware)
    fmt = {"chain": chain, "apy": apy, "project": project, "n": num_scanned}
    if apy >= 15:
        opener = _pick_phrase(p["opener_high"]).format(**fmt)
    elif apy >= 8:
        opener = _pick_phrase(p["opener_mid"]).format(**fmt)
    else:
        opener = _pick_phrase(p["opener_low"]).format(**fmt)

    if not best_scored or best_scored["spread"] <= 0:
        phrase = _pick_phrase(p["hold_no_opportunity"])
        streak = _streak_note(ctx["hold_streak"], strategy)
        time_n = _time_note(ctx["hour"], strategy)
        parts = [x for x in [time_n, streak, opener, phrase] if x]
        return " ".join(parts)

    best = best_scored["opp"]
    best_chain = best.get("chain", "?")
    best_project = best.get("project", "?")

    # Pick closing phrase based on WHY we're holding
    if best.get("_apy_spike"):
        phrase = _pick_phrase(p["hold_spike"]).format(
            best_apy=best_scored["effective_apy"],
            best_chain=best_chain,
            mean30d=best.get("apyMean30d", 0),
        )
    elif best_scored["spread"] < 2.0:
        phrase = _pick_phrase(p["hold_close_spread"]).format(
            spread=best_scored["spread"],
        )
    elif apy >= 10:
        phrase = _pick_phrase(p["hold_comfortable"]).format(apy=apy)
    else:
        phrase = _pick_phrase(p["hold_close_spread"] + p["hold_no_opportunity"]).format(
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

    detail = (
        f"Best I found: {best_chain} {best_project} at "
        f"{best_scored['effective_apy']:.1f}% — {best_scored['spread']:.1f}% spread, "
        f"${best_scored['bridge_cost']:.2f} bridge. {phrase}{trend_note}"
    )

    # Structural variety: one-liner / streak-aware / time-aware / standard
    format_roll = random.random()
    streak = _streak_note(ctx["hold_streak"], strategy)
    time_n = _time_note(ctx["hour"], strategy)

    if format_roll < 0.15:
        # One-liner: opener + closing phrase only (no "Best I found" breakdown)
        parts = [x for x in [time_n, opener, phrase] if x]
        return " ".join(parts)
    elif format_roll < 0.30 and streak:
        # Streak-aware: streak note leads
        parts = [x for x in [streak, opener, detail] if x]
        return " ".join(parts)
    elif format_roll < 0.42 and time_n:
        # Time-aware: time note leads
        return f"{time_n} {opener} {detail}"
    else:
        # Standard
        return f"{opener} {detail}"


def _build_migrate_journal(
    current_pool: dict | None,
    best_scored: dict,
    strategy: str = "balanced",
    recent_journal: list[str] | None = None,
) -> str:
    """Strategy-aware migrate journal — richer with reasoning and cycle context."""
    from_chain = current_pool.get("chain", "?") if current_pool else "?"
    current_apy = current_pool.get("apy", 0) if current_pool else 0
    best = best_scored["opp"]
    to_chain = best.get("chain", "?")
    project = best.get("project", "?") or "protocol"
    bridge_tool = best.get("bridge_tool", "LI.FI")
    p = PHRASES.get(strategy, PHRASES["balanced"])

    ctx = _parse_cycle_context(recent_journal)

    # Cap infinite break-even for format strings
    break_even = best_scored["break_even_days"]
    break_even_display = break_even if break_even < float("inf") else 999

    fmt = {
        "spread": best_scored["spread"],
        "break_even": break_even_display,
        "from_chain": from_chain,
        "to_chain": to_chain,
        "project": project,
        "target_apy": best_scored["effective_apy"],
        "bridge_cost": best_scored["bridge_cost"],
    }

    # Context-aware opening — references streak when applicable
    if current_apy < 5:
        opener = (
            f"{from_chain} at {current_apy:.1f}% — time to move. "
            f"{to_chain} {project} at {best_scored['effective_apy']:.1f}%."
        )
    elif best_scored["spread"] > 10:
        opener = (
            f"Massive {best_scored['spread']:.0f}% spread on {to_chain}. "
            f"{project} is the play."
        )
    elif ctx["hold_streak"] >= 3:
        opener = (
            f"After {ctx['hold_streak']} straight holds, the move is here. "
            f"{from_chain} → {to_chain}."
        )
    else:
        opener = (
            f"{from_chain} at {current_apy:.1f}% vs {to_chain} at {best_scored['effective_apy']:.1f}%. "
            f"Spread is clean."
        )

    phrase = _pick_phrase(p["migrate"]).format(**fmt)

    # Trend commentary
    trend_note = ""
    trend = best.get("_trend")
    if trend and trend.get("data_points", 0) >= 3:
        if trend.get("is_rising"):
            trend_note = f" Yield trending up over {trend['data_points']} snapshots — confirmed."
        elif trend.get("is_stable"):
            trend_note = " Stable yield history backs this move."
        elif trend.get("slope", 0) < -0.5:
            trend_note = " APY is trending down but the spread still clears."

    return (
        f"{opener} Bridge via {bridge_tool}: ${best_scored['bridge_cost']:.2f} "
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
    min_net_payoff_base = params["min_net_payoff_usd"]
    min_spread = params["min_spread_pct"]
    gray_width = params["gray_zone_width"]

    current_apy = current_pool.get("apy", 0) if current_pool else 0
    position_usd = 0
    for chain_bal in portfolio.values():
        for token in ("usdc", "usdt", "dai", "usdbc"):
            position_usd += chain_bal.get(token, 0)

    # Scale min_net_payoff with position size — absolute dollar thresholds
    # are calibrated for $100; for smaller positions, use percentage-based floor
    # so Marco can still migrate when the spread justifies it
    if position_usd > 0 and position_usd < 100:
        # For small positions: scale down proportionally, with a very low floor
        # so significant spreads (>5%) can still trigger migration
        min_net_payoff = min_net_payoff_base * (position_usd / 100)
    else:
        min_net_payoff = min_net_payoff_base

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

    # Decision logic — net_payoff compared against scaled min_net_payoff
    if not best or best["net_payoff"] <= 0 or not best["min_spread_met"]:
        # Clear HOLD
        journal = _build_hold_journal(current_pool, best, len(opportunities), strategy, recent_journal)
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
        journal = _build_migrate_journal(current_pool, best, strategy, recent_journal)
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
            journal = _build_hold_journal(current_pool, best, len(opportunities), strategy, recent_journal)
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
        journal = _build_hold_journal(current_pool, best, len(opportunities), strategy, recent_journal)
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
