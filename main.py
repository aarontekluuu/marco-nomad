"""Marco the Nomad — primary entry point with Telegram bot + autonomous loop.

Usage: python main.py [--once]

Runs the agent loop with Telegram bot as the primary interface.
Supports /pause, /resume, /wallet, and all other commands.
"""

import asyncio
import logging
import os

from dotenv import load_dotenv

from marco import run_cycle, _banner, log as marco_log
from telegram_bot import MarcoBot
from wallet import load_state, load_wallet, format_state

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

CYCLE_INTERVAL = int(os.getenv("LOOP_INTERVAL", "900"))  # 15min default for autonomous mode
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"


async def main():
    _banner()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    agent_state = {
        "journal": [],
        "trigger_cycle": asyncio.Event(),
        "paused": False,
    }

    bot = None
    if token and chat_id:
        bot = MarcoBot(token, chat_id, agent=agent_state)
        await bot.start()
        marco_log("Telegram bot started")
    else:
        marco_log("No Telegram credentials — running without bot")

    mode = "DEMO (simulated)" if DEMO_MODE else "LIVE (real transactions)"
    marco_log(f"Mode: {mode} | Cycle: {CYCLE_INTERVAL}s ({CYCLE_INTERVAL // 60}min)")

    # Show wallet info
    wallet_info = load_wallet()
    if wallet_info:
        addr = wallet_info[0]
        marco_log(f"Wallet: {addr[:10]}...{addr[-6:]}")
    else:
        marco_log("No wallet configured — use /wallet in Telegram or set WALLET_PRIVATE_KEY")

    marco_log(f"Position: {format_state(load_state())}")

    if "--once" in __import__("sys").argv:
        await run_cycle()
        if bot:
            await bot.stop()
        return

    try:
        while True:
            # Check pause state
            if agent_state["paused"]:
                marco_log("⏸️ Paused — waiting for /resume")
                while agent_state["paused"]:
                    await asyncio.sleep(5)
                marco_log("▶️ Resumed")

            try:
                await run_cycle()
                # Push cycle summary to Telegram
                if bot:
                    state = load_state()
                    pool = state.get("current_pool")
                    pool_str = (
                        f"{pool.get('symbol', '?')} on {pool.get('project', '?')} — "
                        f"{pool.get('apy', 0):.1f}% APY"
                    ) if pool else "No pool"
                    await bot.send_journal(
                        f"Cycle complete. Position: ${state['position_usd']:.2f} "
                        f"{state.get('current_token', 'USDC')}. {pool_str}."
                    )
            except Exception as e:
                marco_log(f"Cycle error: {e}")
                import traceback
                traceback.print_exc()

            # Wait for interval or manual Telegram trigger
            next_min = CYCLE_INTERVAL // 60
            marco_log(f"─── Sleeping {next_min}m until next cycle ────────────")
            try:
                await asyncio.wait_for(
                    agent_state["trigger_cycle"].wait(),
                    timeout=CYCLE_INTERVAL,
                )
                agent_state["trigger_cycle"].clear()
                marco_log("Manual trigger received via Telegram")
            except asyncio.TimeoutError:
                pass
    finally:
        if bot:
            await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
