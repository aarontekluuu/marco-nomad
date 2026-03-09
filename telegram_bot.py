"""Marco's Telegram interface — journal + commands."""

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logger = logging.getLogger(__name__)


class MarcoBot:
    """Telegram bot that shows Marco's decisions and accepts commands."""

    def __init__(self, token: str, chat_id: str, agent=None):
        self.token = token
        self.chat_id = chat_id
        self.agent = agent  # Reference to the main agent loop
        self.app = Application.builder().token(token).build()
        self._setup_handlers()

    def _setup_handlers(self):
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("journal", self._cmd_journal))
        self.app.add_handler(CommandHandler("migrate", self._cmd_migrate))
        self.app.add_handler(CommandHandler("portfolio", self._cmd_portfolio))

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "I'm Marco, a cross-chain yield nomad.\n\n"
            "I roam EVM chains hunting the best yields, "
            "migrating capital via LI.FI when the math works.\n\n"
            "Commands:\n"
            "/status — where I am now\n"
            "/journal — recent decisions\n"
            "/migrate — force an evaluation cycle\n"
            "/portfolio — balances per chain"
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.agent:
            await update.message.reply_text("Agent not connected.")
            return
        from wallet import load_state, format_state
        state = load_state()
        await update.message.reply_text(f"📍 Marco's Position\n\n{format_state(state)}")

    async def _cmd_journal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        import json
        from pathlib import Path
        journal_file = Path(__file__).parent / "journal.json"
        entries = []
        if journal_file.exists():
            try:
                entries = json.loads(journal_file.read_text())[-5:]
            except (json.JSONDecodeError, OSError):
                pass
        if not entries:
            await update.message.reply_text("No journal entries yet. I'm still scouting.")
            return
        text = "\n\n---\n\n".join(entries)
        await update.message.reply_text(f"Marco's Journal\n\n{text}")

    async def _cmd_migrate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Running evaluation cycle now...")
        if self.agent and self.agent.get("trigger_cycle"):
            self.agent["trigger_cycle"].set()

    async def _cmd_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        from wallet import load_state
        state = load_state()
        chain = state.get("current_chain", "?")
        position = state.get("position_usd", 0)
        pool = state.get("current_pool")
        migrations = len(state.get("migrations", []))

        text = f"Marco's Portfolio\n\n"
        text += f"Chain: {chain}\n"
        text += f"Position: ${position:.2f} USDC\n"
        if pool:
            text += f"Pool: {pool['symbol']} on {pool['chain']} ({pool['project']})\n"
            text += f"APY: {pool.get('apy', 0):.2f}%\n"
        text += f"Total migrations: {migrations}"

        await update.message.reply_text(text)

    async def send_journal(self, entry: str):
        """Post a journal entry to the chat."""
        try:
            bot = self.app.bot
            await bot.send_message(chat_id=self.chat_id, text=f"Marco's Journal\n\n{entry}")
        except Exception as e:
            logger.error(f"Failed to send journal: {e}")

    async def send_migration(self, journal: str, decision: dict):
        """Post a migration decision to the chat."""
        moves = decision.get("moves", [])
        move_text = ""
        for m in moves:
            move_text += f"\n  {m.get('from_chain', '?')} → {m.get('to_chain', '?')} — {m.get('reason', '')}"

        confidence = decision.get("confidence", 0)
        action = decision.get("action", "hold")
        risk = decision.get("risk_notes", "")

        text = (
            f"Marco's Decision: {action.upper()}\n\n"
            f"{journal}\n\n"
            f"Moves:{move_text if move_text else ' None'}\n"
            f"Confidence: {confidence:.0%}\n"
        )
        if risk:
            text += f"Risk: {risk}"

        try:
            bot = self.app.bot
            await bot.send_message(chat_id=self.chat_id, text=text)
        except Exception as e:
            logger.error(f"Failed to send migration: {e}")

    async def start(self):
        """Start the bot (non-blocking, runs alongside agent loop)."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()

    async def stop(self):
        """Stop the bot."""
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
