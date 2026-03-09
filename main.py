"""Marco the Nomad — autonomous cross-chain yield agent."""

import asyncio
import logging
import os

import httpx
from dotenv import load_dotenv

from brain import decide
from lifi import get_quote, calc_bridge_cost, format_quote
from telegram_bot import MarcoBot
from wallet import load_state, save_state, record_migration, format_state
from yield_scanner import scan_yields, CHAIN_MAP, CHAIN_MAP_REVERSE

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("marco")

CYCLE_INTERVAL = int(os.getenv("CYCLE_INTERVAL", "300"))  # 5 minutes default
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"  # Safe by default


async def run_cycle(client: httpx.AsyncClient, agent_state: dict, bot: MarcoBot | None):
    """Run one evaluation cycle."""
    state = load_state()
    log.info(f"Cycle start | {format_state(state)}")

    # 1. Scan yields
    log.info("Scanning yields...")
    opportunities = await scan_yields(client, min_tvl=100_000, min_apy=2.0)
    log.info(f"Found {len(opportunities)} opportunities")

    if not opportunities:
        log.info("No opportunities found. Holding.")
        return

    # 2. Build portfolio context for Marco's brain
    portfolio = {
        CHAIN_MAP.get(state["current_chain"], f"chain-{state['current_chain']}"): {
            "usdc": state["position_usd"],
        }
    }

    # 3. Get LI.FI quotes for top opportunities on different chains
    current_chain = state["current_chain"]
    quotes = {}
    for opp in opportunities[:5]:
        opp_chain_id = CHAIN_MAP_REVERSE.get(opp["chain"])
        if opp_chain_id and opp_chain_id != current_chain:
            try:
                quote = await get_quote(
                    client,
                    from_chain=current_chain,
                    to_chain=opp_chain_id,
                    from_token="USDC",
                    to_token="USDC",
                    from_amount=str(int(state["position_usd"] * 1e6)),
                    from_address=state["address"],
                )
                cost = calc_bridge_cost(quote)
                quotes[opp["chain"]] = cost
                log.info(f"Quote {CHAIN_MAP.get(current_chain)} → {opp['chain']}: {format_quote(quote)}")
            except Exception as e:
                log.warning(f"Failed to get quote for {opp['chain']}: {e}")

    # 4. Add bridge costs to opportunities context
    for opp in opportunities:
        if opp["chain"] in quotes:
            opp["bridge_cost_usd"] = quotes[opp["chain"]]["total_cost_usd"]

    # 5. Ask Marco's brain
    log.info("Consulting Marco's brain...")
    result = await decide(
        portfolio=portfolio,
        opportunities=opportunities,
        recent_journal=agent_state.get("journal", [])[-3:],
    )

    journal_entry = result["journal"]
    decision = result["decision"]

    log.info(f"Decision: {decision['action']} | Confidence: {decision.get('confidence', 0):.0%}")
    log.info(f"Journal: {journal_entry[:200]}")

    # Save journal
    agent_state.setdefault("journal", []).append(journal_entry)

    # 6. Execute moves (record state changes, no real tx for MVP)
    if decision["action"] in ("migrate", "rebalance"):
        for move in decision.get("moves", []):
            to_chain_id = CHAIN_MAP_REVERSE.get(move["to_chain"])
            if to_chain_id and to_chain_id != current_chain:
                # Find the target pool
                target_pool = next(
                    (o for o in opportunities if o["chain"] == move["to_chain"]),
                    {"symbol": "?", "project": "?", "apy": 0, "chain": move["to_chain"]},
                )
                cost = quotes.get(move["to_chain"], {}).get("total_cost_usd", 0)
                record_migration(state, current_chain, to_chain_id, target_pool, cost, move["reason"])
                log.info(f"Migrated to {move['to_chain']} (chain {to_chain_id})")

        # Send to Telegram
        if bot:
            await bot.send_migration(journal_entry, decision)
    else:
        if bot:
            await bot.send_journal(journal_entry)

    log.info("Cycle complete.")


async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    agent_state = {"journal": [], "trigger_cycle": asyncio.Event()}

    bot = None
    if token and chat_id:
        bot = MarcoBot(token, chat_id, agent=agent_state)
        await bot.start()
        log.info("Telegram bot started")

    async with httpx.AsyncClient() as client:
        log.info("Marco the Nomad is awake.")
        log.info(f"Position: {format_state(load_state())}")
        log.info(f"Cycle interval: {CYCLE_INTERVAL}s")

        try:
            while True:
                try:
                    await run_cycle(client, agent_state, bot)
                except Exception as e:
                    log.error(f"Cycle error: {e}", exc_info=True)

                # Wait for interval or manual trigger
                try:
                    await asyncio.wait_for(
                        agent_state["trigger_cycle"].wait(),
                        timeout=CYCLE_INTERVAL,
                    )
                    agent_state["trigger_cycle"].clear()
                    log.info("Manual trigger received")
                except asyncio.TimeoutError:
                    pass  # Normal interval elapsed
        finally:
            if bot:
                await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
