"""Marco the Nomad - main agent loop.

An autonomous cross-chain yield nomad that migrates capital
across chains, chasing optimal yields while factoring in bridge
costs via LI.FI. Built for the LI.FI Vibeathon.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

import brain
import lifi
import wallet
import yield_scanner
from yield_scanner import CHAIN_MAP, CHAIN_MAP_REVERSE

load_dotenv()

# Config
SCAN_CHAINS = [int(c) for c in os.getenv("SCAN_CHAINS", "8453,42161,10,137").split(",")]
MIN_TVL = float(os.getenv("MIN_TVL_USD", "500000"))
MIN_APY = float(os.getenv("MIN_APY", "3.0"))
MAX_BRIDGE_COST_PCT = float(os.getenv("MAX_BRIDGE_COST_PCT", "2.0"))
POSITION_SIZE = float(os.getenv("POSITION_SIZE_USD", "100"))
LOOP_INTERVAL = int(os.getenv("LOOP_INTERVAL", "3600"))
LIFI_API_KEY = os.getenv("LIFI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

JOURNAL_FILE = Path(__file__).parent / "journal.json"


def load_journal() -> list[str]:
    if JOURNAL_FILE.exists():
        return json.loads(JOURNAL_FILE.read_text())
    return []


def save_journal(entries: list[str]):
    JOURNAL_FILE.write_text(json.dumps(entries, indent=2))


async def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as e:
            log(f"Telegram send failed: {e}")


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


async def run_cycle():
    """Run one decision cycle."""
    state = wallet.load_state()
    log(f"Current state:\n{wallet.format_state(state)}")

    async with httpx.AsyncClient() as client:
        # 1. Scan yields
        log("Scanning yields...")
        candidates = await yield_scanner.scan_yields(
            client, chains=SCAN_CHAINS, min_tvl=MIN_TVL, min_apy=MIN_APY
        )

        if not candidates:
            log("No yields found matching criteria.")
            return

        log(f"Found {len(candidates)} opportunities. Top 5:")
        for p in candidates[:5]:
            log(f"  {yield_scanner.format_pool(p)}")

        # 2. Get bridge costs for cross-chain moves
        log("Getting bridge quotes...")
        wallet_addr = state.get("address") or "0x0000000000000000000000000000000000000001"
        current_chain = state["current_chain"]

        for pool in candidates[:5]:
            target_chain = CHAIN_MAP_REVERSE.get(pool.get("chain"))
            if not target_chain or target_chain == current_chain:
                continue
            from_token = wallet.USDC.get(current_chain)
            to_token = wallet.USDC.get(target_chain)
            if not from_token or not to_token:
                continue
            amount_wei = str(int(POSITION_SIZE * 10**wallet.USDC_DECIMALS))
            try:
                quote = await lifi.get_quote(
                    client, current_chain, target_chain, from_token, to_token,
                    amount_wei, wallet_addr, api_key=LIFI_API_KEY,
                )
                cost = lifi.calc_bridge_cost(quote)
                log(f"  -> {pool['chain']}: ${cost['total_cost_usd']:.2f}")
            except Exception as e:
                log(f"  -> {pool['chain']}: quote failed ({e})")

        # 3. Build portfolio view for brain
        chain_name = CHAIN_MAP.get(current_chain, f"Chain {current_chain}")
        portfolio = {chain_name: {"usdc": POSITION_SIZE, "native": 0}}

        # 4. Ask Marco's brain
        log("Marco is thinking...")
        journal_entries = load_journal()
        result = await brain.decide(portfolio, candidates, journal_entries[-3:])

        journal_text = result["journal"]
        decision = result["decision"]

        log(f"Decision: {decision.get('action', 'hold').upper()}")
        log(f"Journal: {journal_text[:200]}...")

        # 5. Record journal entry
        journal_entries.append(f"[{datetime.now().isoformat()}] {journal_text}")
        save_journal(journal_entries)

        # 6. Execute moves if migrating
        if decision.get("action") == "migrate" and decision.get("moves"):
            for move in decision["moves"]:
                to_chain_name = move.get("to_chain", "")
                target_chain_id = CHAIN_MAP_REVERSE.get(to_chain_name)
                # Find matching pool
                target_pool = next(
                    (p for p in candidates if p.get("chain") == to_chain_name), None
                )
                if target_pool and target_chain_id:
                    wallet.record_migration(
                        state, current_chain, target_chain_id, target_pool,
                        0, move.get("reason", journal_text[:100]),
                    )
                    log(f"Migration recorded: -> {to_chain_name}")

        # 7. Notify
        telegram_msg = (
            f"*Marco's Journal*\n"
            f"_{datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n"
            f"{journal_text}\n\n"
            f"Action: *{decision.get('action', 'hold').upper()}*"
        )
        await send_telegram(telegram_msg)


async def main():
    log("Marco the Nomad waking up...")
    log(f"Chains: {SCAN_CHAINS} | Min TVL: ${MIN_TVL:,.0f} | Min APY: {MIN_APY}%")

    if "--once" in sys.argv:
        await run_cycle()
        return

    while True:
        try:
            await run_cycle()
        except Exception as e:
            log(f"Cycle error: {e}")
            import traceback
            traceback.print_exc()
        log(f"Sleeping {LOOP_INTERVAL}s...")
        await asyncio.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
