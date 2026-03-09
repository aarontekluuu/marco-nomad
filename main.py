"""Marco the Nomad — alternative entry point with Telegram bot integration.

For the main agent loop, use: python marco.py [--once]
This file adds the Telegram bot as a parallel listener.
"""

import asyncio
import logging
import os

from dotenv import load_dotenv

from marco import run_cycle, log as marco_log
from telegram_bot import MarcoBot
from wallet import load_state, format_state

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

CYCLE_INTERVAL = int(os.getenv("LOOP_INTERVAL", "3600"))
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"


async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    agent_state = {"journal": [], "trigger_cycle": asyncio.Event()}

    bot = None
    if token and chat_id:
        bot = MarcoBot(token, chat_id, agent=agent_state)
        await bot.start()
        marco_log("Telegram bot started")
    else:
        marco_log("No Telegram credentials — running without bot")

    mode = "DEMO (simulated)" if DEMO_MODE else "LIVE (real execution)"
    marco_log(f"Marco the Nomad is awake. Mode: {mode}")
    marco_log(f"Position: {format_state(load_state())}")

    try:
        while True:
            try:
                await run_cycle()
            except Exception as e:
                marco_log(f"Cycle error: {e}")
                import traceback
                traceback.print_exc()

            # Wait for interval or manual Telegram trigger
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
