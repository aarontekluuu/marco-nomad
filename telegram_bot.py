"""Marco's Telegram interface — journal + commands.

Security:
- Chat ID allowlist: only the configured chat can interact
- Rate limiting: /migrate cooldown prevents DoS of the agent loop
- Input sanitization: all user-facing output escaped, no raw rendering
- Message length capping: Telegram 4096 char limit enforced
- No prompt injection surface: bot commands are fixed, no freeform AI input
"""

import asyncio
import html
import logging
import time

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logger = logging.getLogger(__name__)

# Telegram message limit
MAX_MESSAGE_LENGTH = 4000  # Leave 96 chars buffer for Telegram's 4096 limit
# Rate limit for /migrate command (seconds)
MIGRATE_COOLDOWN_SECONDS = 300  # 5 minutes between manual triggers


def _escape(text: str) -> str:
    """Escape text for safe Telegram HTML rendering. Prevents markup injection."""
    return html.escape(str(text), quote=True)


def _truncate(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    """Truncate text to fit Telegram message limits."""
    if len(text) <= limit:
        return text
    return text[:limit - 20] + "\n\n…(truncated)"


class MarcoBot:
    """Telegram bot that shows Marco's decisions and accepts commands.

    Security model:
    - Only responds to messages from the configured chat_id
    - All output is HTML-escaped to prevent injection
    - /migrate is rate-limited to prevent agent loop abuse
    - No freeform text is passed to any AI model (no prompt injection surface)
    """

    def __init__(self, token: str, chat_id: str, agent=None):
        self.token = token
        self.chat_id = str(chat_id).strip()
        self.agent = agent  # Reference to the main agent loop
        self._last_migrate_ts: float = 0  # Rate limit tracker
        self.app = Application.builder().token(token).build()
        self._setup_handlers()

    def _setup_handlers(self):
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("journal", self._cmd_journal))
        self.app.add_handler(CommandHandler("migrate", self._cmd_migrate))
        self.app.add_handler(CommandHandler("portfolio", self._cmd_portfolio))
        self.app.add_handler(CommandHandler("scan", self._cmd_scan))
        self.app.add_handler(CommandHandler("fund", self._cmd_fund))
        self.app.add_handler(CommandHandler("help", self._cmd_start))
        # Catch-all: ignore non-command messages silently (no prompt injection surface)
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._ignore))

    def _is_authorized(self, update: Update) -> bool:
        """Check if the message is from the authorized chat. Prevents unauthorized access."""
        if not update.effective_chat:
            return False
        return str(update.effective_chat.id) == self.chat_id

    async def _reject_unauthorized(self, update: Update) -> bool:
        """If unauthorized, log and silently ignore. Returns True if rejected."""
        if self._is_authorized(update):
            return False
        logger.warning(
            f"Unauthorized access attempt from chat_id={update.effective_chat.id} "
            f"user={update.effective_user.username if update.effective_user else '?'}"
        )
        return True

    async def _ignore(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Silently ignore non-command text. No AI processing = no prompt injection."""
        pass

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        await update.message.reply_text(
            "🏜️ <b>Marco the Nomad</b>\n\n"
            "I roam EVM chains hunting the best stablecoin yields, "
            "migrating capital via LI.FI when the math works.\n\n"
            "<b>Commands:</b>\n"
            "/status — current position + pool\n"
            "/portfolio — detailed balances\n"
            "/journal — recent decisions\n"
            "/scan — live yield scanner\n"
            "/fund — deposit address + safety info\n"
            "/migrate — force evaluation cycle\n"
            "/help — this message",
            parse_mode="HTML",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        from wallet import load_state
        from yield_scanner import CHAIN_MAP
        state = load_state()
        chain = state.get("current_chain", "?")
        chain_name = CHAIN_MAP.get(chain, f"Chain {chain}")
        token = state.get("current_token", "USDC")
        position = state.get("position_usd", 0)
        pool = state.get("current_pool")
        migrations = len(state.get("migrations", []))

        lines = [
            "📍 <b>Marco's Position</b>\n",
            f"Chain: <b>{_escape(chain_name)}</b>",
            f"Token: <b>{_escape(token)}</b>",
            f"Position: <b>${position:.2f}</b>",
        ]
        if pool:
            lines.append(
                f"Pool: {_escape(pool.get('symbol', '?'))} on "
                f"{_escape(pool.get('project', '?'))} — "
                f"<b>{pool.get('apy', 0):.1f}% APY</b>"
            )
        lines.append(f"Migrations: {migrations}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_journal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        import json
        from pathlib import Path
        journal_file = Path(__file__).parent / "journal.json"
        entries = []
        if journal_file.exists():
            try:
                raw = json.loads(journal_file.read_text())
                # Validate structure: must be a list of strings
                if isinstance(raw, list):
                    entries = [str(e) for e in raw[-5:]]
            except (json.JSONDecodeError, OSError):
                pass
        if not entries:
            await update.message.reply_text("No journal entries yet. I'm still scouting.")
            return

        lines = ["📖 <b>Marco's Journal</b>\n"]
        for entry in entries:
            # Strip timestamp prefix for cleaner display
            text = entry
            if text.startswith("["):
                bracket_end = text.find("]")
                if bracket_end > 0:
                    ts = text[1:bracket_end][:16]  # Trim to YYYY-MM-DDTHH:MM
                    text = text[bracket_end + 2:]
                    lines.append(f"<i>{_escape(ts)}</i>")
            # Strip risk notes (internal)
            if " [RISK:" in text:
                text = text[:text.rfind(" [RISK:")]
            lines.append(f"{_escape(text)}\n")

        msg = _truncate("\n".join(lines))
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_migrate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return

        # Rate limit: prevent spamming the agent loop
        now = time.time()
        elapsed = now - self._last_migrate_ts
        if elapsed < MIGRATE_COOLDOWN_SECONDS:
            remaining = int(MIGRATE_COOLDOWN_SECONDS - elapsed)
            await update.message.reply_text(
                f"⏳ Cooldown: wait {remaining}s before triggering another cycle."
            )
            return

        self._last_migrate_ts = now
        await update.message.reply_text("⚡ Running evaluation cycle now...")
        if self.agent and self.agent.get("trigger_cycle"):
            self.agent["trigger_cycle"].set()

    async def _cmd_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        from wallet import load_state
        from yield_scanner import CHAIN_MAP
        state = load_state()
        chain = state.get("current_chain", "?")
        chain_name = CHAIN_MAP.get(chain, f"Chain {chain}")
        token = state.get("current_token", "USDC")
        position = state.get("position_usd", 0)
        pool = state.get("current_pool")
        migrations = state.get("migrations", [])
        total_cost = sum(m.get("cost_usd", 0) for m in migrations)

        lines = [
            "💰 <b>Marco's Portfolio</b>\n",
            f"Chain: <b>{_escape(chain_name)}</b>",
            f"Token: <b>{_escape(token)}</b>",
            f"Position: <b>${position:.2f}</b>",
        ]
        if pool:
            lines.append(
                f"Pool: {_escape(pool.get('symbol', '?'))} on "
                f"{_escape(pool.get('chain', '?'))} "
                f"({_escape(pool.get('project', '?'))})"
            )
            lines.append(f"APY: <b>{pool.get('apy', 0):.2f}%</b>")
        lines.append(f"\nMigrations: <b>{len(migrations)}</b>")
        lines.append(f"Total bridge/swap costs: <b>${total_cost:.2f}</b>")
        if migrations:
            lines.append(f"Avg cost per move: ${total_cost / len(migrations):.2f}")

        # Show last 3 migrations
        if migrations:
            lines.append("\n<b>Recent moves:</b>")
            for m in migrations[-3:]:
                move_type = m.get("type", "bridge")
                emoji = "🔄" if move_type == "swap" else "🌉"
                from_tok = m.get("from_token", "?")
                to_tok = m.get("to_token", "?")
                lines.append(
                    f"{emoji} {_escape(m.get('pool_symbol', '?'))} — "
                    f"{m.get('pool_apy', 0):.1f}% — "
                    f"${m.get('cost_usd', 0):.2f} "
                    f"({from_tok}→{to_tok})"
                )

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Live yield scan — shows top opportunities without triggering a move."""
        if await self._reject_unauthorized(update):
            return
        await update.message.reply_text("📡 Scanning yields across chains...")

        try:
            import httpx
            from yield_scanner import scan_yields

            async with httpx.AsyncClient() as client:
                pools = await scan_yields(client, chains=[8453, 42161, 10, 137])

            if not pools:
                await update.message.reply_text("No pools found matching criteria.")
                return

            lines = ["📡 <b>Top Yields</b>\n"]
            for i, p in enumerate(pools[:10], 1):
                trusted = "✓" if p.get("_trusted") else ""
                spike = "⚠" if p.get("_apy_spike") else ""
                lines.append(
                    f"{i}. <b>{_escape(p.get('chain', '?'))}</b> | "
                    f"{_escape(p.get('project', '?'))} | "
                    f"{_escape(p.get('symbol', '?'))}\n"
                    f"   APY: <b>{p.get('apy', 0):.2f}%</b> "
                    f"(30d: {p.get('apyMean30d', 0):.2f}%) "
                    f"TVL: ${p.get('tvlUsd', 0):,.0f} {trusted}{spike}"
                )

            msg = _truncate("\n".join(lines))
            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Scan failed: {_escape(str(e))}")

    async def _cmd_fund(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show deposit address and safety information for funding the agent wallet."""
        if await self._reject_unauthorized(update):
            return
        from wallet import load_state, USDC
        from yield_scanner import CHAIN_MAP
        state = load_state()
        addr = state.get("address", "")
        chain = state.get("current_chain", 8453)
        chain_name = CHAIN_MAP.get(chain, f"Chain {chain}")
        token = state.get("current_token", "USDC")

        if not addr:
            await update.message.reply_text(
                "⚠️ No wallet address configured.\n"
                "Set WALLET_ADDRESS in .env or run in LIVE mode to auto-derive from private key."
            )
            return

        usdc_addr = USDC.get(chain)
        lines = [
            "🏦 <b>Fund Marco's Wallet</b>\n",
            f"<b>Deposit address:</b>",
            f"<code>{_escape(addr)}</code>\n",
            f"<b>Current chain:</b> {_escape(chain_name)} (ID: {chain})",
            f"<b>Current token:</b> {_escape(token)}",
            "",
            "⚠️ <b>Safety checklist:</b>",
            f"• Only send <b>{_escape(token)}</b> on <b>{_escape(chain_name)}</b>",
            "• Double-check the address before sending",
            "• Start with a small test amount",
            "• Marco moves his <b>entire</b> position — don't over-fund",
            "• Never share your private key with anyone",
        ]
        if usdc_addr:
            lines.append(f"\n<b>{_escape(token)} contract:</b>")
            lines.append(f"<code>{_escape(usdc_addr)}</code>")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def send_journal(self, entry: str):
        """Post a journal entry to the chat. All content escaped."""
        try:
            bot = self.app.bot
            msg = _truncate(f"📖 <b>Marco's Journal</b>\n\n{_escape(entry)}")
            await bot.send_message(
                chat_id=self.chat_id, text=msg, parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send journal: {e}")

    async def send_migration(self, journal: str, decision: dict):
        """Post a migration decision to the chat. All content escaped."""
        moves = decision.get("moves", [])
        move_lines = []
        for m in moves:
            move_lines.append(
                f"  {_escape(m.get('from_chain', '?'))} → "
                f"{_escape(m.get('to_chain', '?'))} — "
                f"{_escape(m.get('reason', ''))}"
            )

        confidence = decision.get("confidence", 0)
        action = decision.get("action", "hold")
        risk = decision.get("risk_notes", "")

        emoji = "🏃" if action == "migrate" else "⏸️"
        lines = [
            f"{emoji} <b>Marco's Decision: {_escape(action.upper())}</b>\n",
            f"{_escape(journal)}\n",
            f"<b>Moves:</b>{chr(10).join(move_lines) if move_lines else ' None'}",
            f"Confidence: <b>{confidence:.0%}</b>",
        ]
        if risk:
            lines.append(f"Risk: {_escape(risk)}")

        try:
            bot = self.app.bot
            msg = _truncate("\n".join(lines))
            await bot.send_message(
                chat_id=self.chat_id, text=msg, parse_mode="HTML",
            )
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
