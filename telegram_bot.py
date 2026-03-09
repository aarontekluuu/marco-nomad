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

from telegram import Update, BotCommand
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
        self._pending_kill_ts: float = 0  # Double-tap confirmation for /kill
        self.app = Application.builder().token(token).build()
        self._setup_handlers()

    def _setup_handlers(self):
        # Core commands (7)
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("help", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("scan", self._cmd_scan))
        self.app.add_handler(CommandHandler("journal", self._cmd_journal))
        self.app.add_handler(CommandHandler("fund", self._cmd_fund))
        self.app.add_handler(CommandHandler("withdraw", self._cmd_withdraw))
        self.app.add_handler(CommandHandler("strategy", self._cmd_strategy))
        self.app.add_handler(CommandHandler("kill", self._cmd_kill))
        # Power commands (via /more)
        self.app.add_handler(CommandHandler("more", self._cmd_more))
        self.app.add_handler(CommandHandler("quote", self._cmd_quote))
        self.app.add_handler(CommandHandler("migrate", self._cmd_migrate))
        self.app.add_handler(CommandHandler("limits", self._cmd_limits))
        self.app.add_handler(CommandHandler("limit", self._cmd_limit))
        self.app.add_handler(CommandHandler("cancel_limit", self._cmd_cancel_limit))
        self.app.add_handler(CommandHandler("dca", self._cmd_dca))
        self.app.add_handler(CommandHandler("balances", self._cmd_balances))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("stoploss", self._cmd_stoploss))
        self.app.add_handler(CommandHandler("wallet", self._cmd_wallet))
        # Legacy aliases (still work, just not in main menu)
        self.app.add_handler(CommandHandler("portfolio", self._cmd_status))
        self.app.add_handler(CommandHandler("pnl", self._cmd_status))
        self.app.add_handler(CommandHandler("withdraw_pool", self._cmd_withdraw))
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

    # ---------------------------------------------------------------
    # CORE COMMANDS
    # ---------------------------------------------------------------

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        await update.message.reply_text(
            "🏜️ <b>Marco the Nomad</b>\n\n"
            "Autonomous cross-chain yield agent. "
            "Roaming EVM chains, chasing stablecoin yields via LI.FI.\n\n"
            "<b>Commands:</b>\n"
            "/status — position, P&amp;L, pool, recent moves\n"
            "/scan — live yield opportunities\n"
            "/journal — recent decisions in Marco's voice\n"
            "/fund — deposit address\n"
            "/withdraw — send funds to your wallet\n"
            "/strategy — conservative / balanced / aggressive\n"
            "/kill — emergency stop\n\n"
            "/more — power commands (quotes, limits, DCA, balances)",
            parse_mode="HTML",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Unified status: position + pool + P&L + recent moves. Replaces /portfolio and /pnl."""
        if await self._reject_unauthorized(update):
            return
        from wallet import load_state, calc_pnl
        from yield_scanner import CHAIN_MAP
        state = load_state()

        chain = state.get("current_chain", "?")
        chain_name = CHAIN_MAP.get(chain, f"Chain {chain}")
        token = state.get("current_token", "USDC")
        position = state.get("position_usd", 0)
        pool = state.get("current_pool")
        migrations = state.get("migrations", [])
        deposited = state.get("_deposited_pool")

        # Position block
        lines = ["📍 <b>Marco's Status</b>\n"]
        lines.append(f"<b>{_escape(chain_name)}</b> — ${position:.2f} {_escape(token)}")

        if pool:
            lines.append(
                f"Pool: {_escape(pool.get('symbol', '?'))} on "
                f"{_escape(pool.get('project', '?'))} — "
                f"<b>{pool.get('apy', 0):.1f}% APY</b>"
            )
        if deposited:
            lines.append(f"💰 Deposited in <b>{_escape(deposited.get('protocol', '?'))}</b>")

        # P&L block
        try:
            pnl = calc_pnl(state)
            emoji = "📈" if pnl["net_pnl"] >= 0 else "📉"
            lines.append(
                f"\n{emoji} P&amp;L: <b>${pnl['net_pnl']:+.2f}</b> "
                f"({pnl['roi_pct']:+.1f}%)"
            )
            if pnl["total_costs"] > 0:
                lines.append(
                    f"Yield: ${pnl['gross_yield']:.2f} | "
                    f"Costs: ${pnl['total_costs']:.2f}"
                )
        except Exception:
            pass

        # Recent moves
        if migrations:
            total_cost = sum(m.get("cost_usd", 0) for m in migrations)
            lines.append(f"\n🔄 <b>{len(migrations)} moves</b> (${total_cost:.2f} total cost)")
            for m in migrations[-3:]:
                from_chain = m.get("from_chain")
                to_chain = m.get("to_chain")
                from_name = CHAIN_MAP.get(from_chain, from_chain) if isinstance(from_chain, int) else (from_chain or "?")
                to_name = CHAIN_MAP.get(to_chain, to_chain) if isinstance(to_chain, int) else (to_chain or "?")
                move_type = m.get("type", "bridge")
                emoji_m = "🔄" if move_type == "swap" else "🌉"
                lines.append(
                    f"  {emoji_m} {_escape(str(from_name))}→{_escape(str(to_name))} "
                    f"{m.get('pool_apy', 0):.1f}% — ${m.get('cost_usd', 0):.2f}"
                )

        # Agent state
        if self.agent:
            paused = self.agent.get("paused", False)
            lines.append(f"\n{'⏸️ Paused' if paused else '▶️ Running'}")

        await update.message.reply_text(
            _truncate("\n".join(lines)), parse_mode="HTML"
        )

    async def _cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Live yield scan — shows top opportunities without triggering a move."""
        if await self._reject_unauthorized(update):
            return
        await update.message.reply_text("📡 Scanning yields across chains...")

        try:
            import httpx
            from yield_scanner import scan_yields
            from wallet import load_state

            state = load_state()
            current_pool = state.get("current_pool")
            current_apy = current_pool.get("apy", 0) if current_pool else 0

            async with httpx.AsyncClient() as client:
                pools = await scan_yields(client, chains=[8453, 42161, 10, 137])

            if not pools:
                await update.message.reply_text("No pools found matching criteria.")
                return

            lines = ["📡 <b>Top Yields</b>"]
            if current_pool:
                lines.append(
                    f"You: {_escape(current_pool.get('chain', '?'))} "
                    f"{_escape(current_pool.get('project', '?'))} @ "
                    f"<b>{current_apy:.1f}%</b>\n"
                )

            for i, p in enumerate(pools[:10], 1):
                trusted = " ✓" if p.get("_trusted") else ""
                spike = " ⚠️SPIKE" if p.get("_apy_spike") else ""
                collapse = " 📉" if p.get("_apy_collapse") else ""
                spread = p.get("apy", 0) - current_apy
                spread_str = f" (+{spread:.1f}%)" if spread > 0 and current_apy > 0 else ""

                # DefiLlama pool link
                pool_id = p.get("pool", "")
                project_name = _escape(p.get("project", "?"))
                if pool_id:
                    project_name = f'<a href="https://defillama.com/yields/pool/{pool_id}">{project_name}</a>'

                lines.append(
                    f"<b>{i}. {_escape(p.get('chain', '?'))}</b> | "
                    f"{project_name}\n"
                    f"   {_escape(p.get('symbol', '?'))} — "
                    f"<b>{p.get('apy', 0):.1f}%</b> "
                    f"(30d: {p.get('apyMean30d', 0):.1f}%){spread_str}\n"
                    f"   TVL: ${p.get('tvlUsd', 0):,.0f}{trusted}{spike}{collapse}"
                )

            msg = _truncate("\n".join(lines))
            await update.message.reply_text(
                msg, parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception as e:
            await update.message.reply_text(f"Scan failed: {_escape(str(e))}")

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
                if isinstance(raw, list):
                    entries = [str(e) for e in raw[-3:]]  # Last 3 for conciseness
            except (json.JSONDecodeError, OSError):
                pass
        if not entries:
            await update.message.reply_text("No journal entries yet. I'm still scouting.")
            return

        lines = ["📖 <b>Marco's Journal</b>\n"]
        for entry in entries:
            text = entry
            if text.startswith("["):
                bracket_end = text.find("]")
                if bracket_end > 0:
                    ts = text[1:bracket_end][:16]
                    text = text[bracket_end + 2:]
                    lines.append(f"<i>{_escape(ts)}</i>")
            if " [RISK:" in text:
                text = text[:text.rfind(" [RISK:")]
            lines.append(f"{_escape(text)}\n")

        msg = _truncate("\n".join(lines))
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_fund(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show deposit address and safety information."""
        if await self._reject_unauthorized(update):
            return
        from wallet import load_state, load_wallet, create_wallet, USDC
        from yield_scanner import CHAIN_MAP
        state = load_state()
        addr = state.get("address", "")
        chain = state.get("current_chain", 8453)
        chain_name = CHAIN_MAP.get(chain, f"Chain {chain}")
        token = state.get("current_token", "USDC")

        if not addr:
            # Try to load or create wallet
            wallet_info = load_wallet()
            if wallet_info:
                addr = wallet_info[0]
            else:
                try:
                    addr, _ = create_wallet()
                    state["address"] = addr
                    from wallet import save_state
                    save_state(state)
                except Exception as e:
                    await update.message.reply_text(f"No wallet. Creation failed: {_escape(str(e))}")
                    return

        lines = [
            "🏦 <b>Fund Marco</b>\n",
            f"Send <b>{_escape(token)}</b> on <b>{_escape(chain_name)}</b> to:\n",
            f"<code>{_escape(addr)}</code>\n",
            "⚠️ Only send stablecoins on the correct chain.",
            "Start with a small test amount.",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_withdraw(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Smart withdraw: auto-exits pool if needed, then sends to owner.

        Usage: /withdraw <address> <amount|all>
        """
        if await self._reject_unauthorized(update):
            return
        from wallet import load_state, load_wallet, withdraw, OWNER_ADDRESSES

        if not OWNER_ADDRESSES:
            await update.message.reply_text(
                "⚠️ No OWNER_ADDRESSES configured in .env.\n"
                "Set OWNER_ADDRESSES=0x... before withdrawing."
            )
            return

        args = context.args
        if not args or len(args) < 2:
            approved = ", ".join(f"<code>{_escape(a[:10])}...</code>" for a in OWNER_ADDRESSES)
            await update.message.reply_text(
                "💸 <b>Withdraw Funds</b>\n\n"
                f"Usage: <code>/withdraw &lt;address&gt; &lt;amount|all&gt;</code>\n\n"
                f"Approved addresses: {approved}\n"
                "Amount in USD (e.g. 5.00) or 'all' for full balance\n\n"
                "If deposited in a pool, Marco will exit automatically first.",
                parse_mode="HTML",
            )
            return

        to_address = args[0].strip()
        if args[1].strip().lower() == "all":
            amount = -1
        else:
            try:
                amount = float(args[1])
            except ValueError:
                await update.message.reply_text(f"Invalid amount: {_escape(args[1])}")
                return

        wallet_info = load_wallet()
        if not wallet_info:
            await update.message.reply_text("⚠️ No wallet configured.")
            return

        state = load_state()
        _, private_key = wallet_info

        # Auto-exit pool if deposited
        deposited = state.get("_deposited_pool")
        if deposited:
            protocol = deposited.get("protocol", "?")
            chain_id = deposited.get("chain_id")
            await update.message.reply_text(
                f"💰 Exiting {_escape(protocol)} pool first..."
            )
            try:
                import protocols
                from lifi import RPC_URLS
                from web3 import Web3
                from wallet import save_state, check_onchain_balance, STABLECOINS, USDC as USDC_MAP, USDC_DECIMALS

                protocols.ensure_loaded()
                adapter = protocols.get_adapter(protocol)
                if not adapter:
                    await update.message.reply_text(f"❌ No adapter for {_escape(protocol)}. Use /more → manual pool exit.")
                    return

                rpc_url = RPC_URLS.get(chain_id)
                w3 = Web3(Web3.HTTPProvider(rpc_url))
                wallet_addr = wallet_info[0]
                current_token = state.get("current_token", "USDC")
                stable_info = STABLECOINS.get((chain_id, current_token))
                token_addr = stable_info["address"] if stable_info else USDC_MAP.get(chain_id)

                await adapter.withdraw(w3, None, token_addr, wallet_addr, private_key, chain_id)
                state.pop("_deposited_pool", None)
                actual = await asyncio.to_thread(
                    check_onchain_balance, chain_id, wallet_addr, rpc_url, token=current_token
                )
                if actual is not None:
                    state["position_usd"] = round(actual, 2)
                save_state(state)
                await update.message.reply_text(f"✅ Exited {_escape(protocol)}. Balance: ${state['position_usd']:.2f}")
                # Reload state after pool exit
                state = load_state()
            except Exception as e:
                await update.message.reply_text(f"❌ Pool exit failed: {_escape(str(e))}")
                return

        amount_label = "all funds" if amount == -1 else f"${amount:.2f}"
        await update.message.reply_text(
            f"💸 Sending {amount_label} to {_escape(to_address[:10])}...{_escape(to_address[-6:])}..."
        )

        try:
            result = await asyncio.to_thread(
                withdraw, state, to_address, amount, private_key,
            )
            await update.message.reply_text(
                f"✅ <b>Sent ${result['amount']:.2f} {_escape(result['token'])}</b>\n"
                f"To: <code>{_escape(result['to'])}</code>\n"
                f"TX: <code>{_escape(result['tx_hash'])}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Withdrawal failed: {_escape(str(e))}")

    async def _cmd_strategy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View or switch strategy profile. Usage: /strategy [profile]"""
        if await self._reject_unauthorized(update):
            return
        from wallet import load_state, get_strategy, set_strategy, STRATEGY_PROFILES
        state = load_state()
        args = context.args
        if not args:
            current = get_strategy(state)
            lines = ["📋 <b>Strategy</b>\n"]
            for name, profile in STRATEGY_PROFILES.items():
                marker = " ◀" if name == current else ""
                lines.append(
                    f"<b>{_escape(name)}</b>{marker}\n"
                    f"  {_escape(profile['description'])}\n"
                    f"  TVL ≥ ${profile['min_tvl']:,.0f} | APY ≥ {profile['min_apy']}% | "
                    f"Bridge cap: {profile['max_bridge_cost_pct']}%"
                )
            lines.append(f"\n<code>/strategy conservative|balanced|aggressive</code>")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return
        profile = args[0].lower().strip()
        try:
            set_strategy(state, profile)
            desc = STRATEGY_PROFILES[profile]["description"]
            await update.message.reply_text(
                f"✅ Strategy: <b>{_escape(profile)}</b>\n{_escape(desc)}",
                parse_mode="HTML",
            )
        except ValueError as e:
            await update.message.reply_text(str(e))

    async def _cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Emergency kill switch: pause agent + revoke all ERC20 approvals.
        Requires double-tap confirmation.
        """
        if await self._reject_unauthorized(update):
            return

        now = time.time()
        elapsed = now - self._pending_kill_ts

        if elapsed > 30:
            self._pending_kill_ts = now
            await update.message.reply_text(
                "🚨 <b>EMERGENCY KILL</b>\n\n"
                "This will:\n"
                "• Pause the agent immediately\n"
                "• Revoke ALL ERC20 approvals to LI.FI\n\n"
                "⚠️ <b>Send /kill again within 30s to confirm.</b>",
                parse_mode="HTML",
            )
            return

        # Second tap — execute
        self._pending_kill_ts = 0

        if self.agent and "paused" in self.agent:
            self.agent["paused"] = True

        await update.message.reply_text("🚨 Kill confirmed. Pausing and revoking approvals...")

        from wallet import load_state, load_wallet, STABLECOINS
        from lifi import LIFI_DIAMOND, RPC_URLS, ERC20_ABI

        state = load_state()
        chain_id = state.get("current_chain", 8453)
        diamond = LIFI_DIAMOND.get(chain_id)
        rpc_url = RPC_URLS.get(chain_id)
        wallet_info = load_wallet()

        if not diamond or not rpc_url or not wallet_info:
            await update.message.reply_text(
                "⏸️ Agent paused.\n⚠️ Could not revoke: missing config."
            )
            return

        wallet_addr, private_key = wallet_info
        tokens_to_revoke = [
            (symbol, info["address"])
            for (cid, symbol), info in STABLECOINS.items()
            if cid == chain_id
        ]

        revoked = []
        errors = []
        for symbol, token_addr in tokens_to_revoke:
            try:
                result = await asyncio.to_thread(
                    self._revoke_approval, token_addr, diamond, wallet_addr, private_key, rpc_url, chain_id,
                )
                if result:
                    revoked.append(symbol)
            except Exception as e:
                errors.append(f"{symbol}: {e}")

        lines = ["🚨 <b>KILL EXECUTED</b>\n", "⏸️ Agent paused."]
        if revoked:
            lines.append(f"✅ Revoked: {', '.join(revoked)}")
        if errors:
            for err in errors:
                lines.append(f"❌ {_escape(err)}")
        if not revoked and not errors:
            lines.append("ℹ️ No approvals to revoke.")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    # ---------------------------------------------------------------
    # POWER COMMANDS (accessed via /more)
    # ---------------------------------------------------------------

    async def _cmd_more(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show power commands menu."""
        if await self._reject_unauthorized(update):
            return
        await update.message.reply_text(
            "⚡ <b>Power Commands</b>\n\n"
            "<b>Trading:</b>\n"
            "/quote &lt;chain&gt; — bridge cost estimate\n"
            "/migrate — force evaluation cycle\n"
            "/limit &lt;chain&gt; &lt;apy&gt; — set limit order\n"
            "/limits — view active limits\n"
            "/cancel_limit &lt;index&gt; — remove limit\n"
            "/dca &lt;chain&gt; — dollar-cost average into chain\n"
            "/stoploss — stop-loss settings\n\n"
            "<b>Info:</b>\n"
            "/balances — multi-chain USDC balances\n"
            "/wallet — view/create wallet\n\n"
            "<b>Control:</b>\n"
            "/pause — pause agent loop\n"
            "/resume — resume agent loop",
            parse_mode="HTML",
        )

    async def _cmd_quote(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Get a LI.FI bridge/swap quote. Usage: /quote <chain_name>"""
        if await self._reject_unauthorized(update):
            return
        from wallet import load_state, USDC
        from yield_scanner import CHAIN_MAP, CHAIN_MAP_REVERSE

        args = context.args
        if not args:
            await update.message.reply_text(
                "💱 Usage: <code>/quote &lt;chain&gt;</code>\n"
                "Example: <code>/quote Optimism</code>",
                parse_mode="HTML",
            )
            return

        target_name = " ".join(args).strip().title()
        target_chain = CHAIN_MAP_REVERSE.get(target_name)
        if target_chain is None:
            await update.message.reply_text(
                f"Unknown chain: {_escape(target_name)}\n"
                f"Try: Base, Optimism, Arbitrum, Polygon"
            )
            return

        state = load_state()
        current_chain = state.get("current_chain", 8453)
        position = state.get("position_usd", 0)
        current_token = state.get("current_token", "USDC")

        if target_chain == current_chain:
            await update.message.reply_text("Already on that chain.")
            return

        from_token_addr = USDC.get(current_chain)
        to_token_addr = USDC.get(target_chain)
        if not from_token_addr or not to_token_addr:
            await update.message.reply_text("USDC not supported on one of those chains.")
            return

        await update.message.reply_text(
            f"💱 Getting quote: {_escape(CHAIN_MAP.get(current_chain, '?'))} → {_escape(target_name)}..."
        )

        try:
            import httpx
            from lifi import get_quote, calc_bridge_cost

            amount_raw = str(int(position * 1e6))
            addr = state.get("address", "0x" + "0" * 40)

            async with httpx.AsyncClient() as client:
                quote = await get_quote(
                    client,
                    from_chain=current_chain, to_chain=target_chain,
                    from_token=from_token_addr, to_token=to_token_addr,
                    from_amount=amount_raw, from_address=addr,
                )
            cost = calc_bridge_cost(quote)
            cost_pct = (cost["total_cost_usd"] / position * 100) if position else 0
            duration = cost.get("duration_seconds", 0)
            dur_str = f"{duration}s" if duration < 60 else f"{duration // 60}m {duration % 60}s"

            lines = [
                f"💱 <b>{_escape(CHAIN_MAP.get(current_chain, '?'))} → {_escape(target_name)}</b>\n",
                f"Send: <b>${position:.2f}</b> {_escape(current_token)}",
                f"Receive: <b>${cost['to_amount']:.2f}</b>",
                f"Cost: <b>${cost['total_cost_usd']:.4f}</b> ({cost_pct:.2f}%)",
                f"Bridge: {_escape(cost['bridge'])} | ~{dur_str}",
            ]
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Quote failed: {_escape(str(e))}")

    async def _cmd_migrate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        now = time.time()
        elapsed = now - self._last_migrate_ts
        if elapsed < MIGRATE_COOLDOWN_SECONDS:
            remaining = int(MIGRATE_COOLDOWN_SECONDS - elapsed)
            await update.message.reply_text(f"⏳ Cooldown: wait {remaining}s.")
            return
        self._last_migrate_ts = now
        await update.message.reply_text("⚡ Running evaluation cycle...")
        if self.agent and self.agent.get("trigger_cycle"):
            self.agent["trigger_cycle"].set()

    async def _cmd_limits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        from wallet import load_state, get_limits
        state = load_state()
        limits = get_limits(state)
        if not limits:
            await update.message.reply_text("No limit orders. Use <code>/limit Base 8.0</code> to add one.", parse_mode="HTML")
            return
        lines = ["📋 <b>Limit Orders</b>\n"]
        for i, lim in enumerate(limits):
            desc = f" — {_escape(lim.get('description', ''))}" if lim.get("description") else ""
            lines.append(
                f"{i}. <b>{_escape(lim['chain'])}</b> ≥ {lim['min_apy']:.1f}%{desc}"
            )
        lines.append(f"\n<code>/cancel_limit &lt;index&gt;</code> to remove.")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_limit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        from wallet import load_state, add_limit
        args = context.args
        if not args or len(args) < 2:
            await update.message.reply_text(
                "Usage: <code>/limit Base 8.0</code>", parse_mode="HTML"
            )
            return
        chain = args[0].strip().title()
        try:
            min_apy = float(args[1])
        except ValueError:
            await update.message.reply_text(f"Invalid APY: {_escape(args[1])}")
            return
        description = " ".join(args[2:]) if len(args) > 2 else ""
        state = load_state()
        add_limit(state, chain, min_apy, description)
        await update.message.reply_text(
            f"✅ Limit: migrate to <b>{_escape(chain)}</b> when APY ≥ {min_apy:.1f}%",
            parse_mode="HTML",
        )

    async def _cmd_cancel_limit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        from wallet import load_state, remove_limit
        args = context.args
        if not args:
            await update.message.reply_text("Usage: /cancel_limit <index>")
            return
        try:
            index = int(args[0])
        except ValueError:
            await update.message.reply_text(f"Invalid index: {_escape(args[0])}")
            return
        state = load_state()
        limits = state.get("_limits", [])
        if index < 0 or index >= len(limits):
            await update.message.reply_text(f"Invalid index. Use /limits to see current orders.")
            return
        remove_limit(state, index)
        await update.message.reply_text(f"✅ Limit {index} removed.")

    async def _cmd_dca(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        from wallet import load_state, get_dca_state, start_dca, DCA_ENABLED, DCA_CHUNKS

        if not DCA_ENABLED:
            await update.message.reply_text("DCA mode disabled. Set DCA_ENABLED=true in .env.")
            return

        state = load_state()
        dca = get_dca_state(state)

        if dca:
            await update.message.reply_text(
                f"📊 <b>DCA Active</b>\n\n"
                f"Target: <b>{_escape(dca.get('target_chain', '?'))}</b>\n"
                f"Progress: {dca.get('completed_chunks', 0)}/{dca.get('total_chunks', DCA_CHUNKS)}",
                parse_mode="HTML",
            )
            return

        args = context.args
        if not args:
            await update.message.reply_text(
                f"Usage: <code>/dca &lt;chain&gt;</code>\n"
                f"Splits migration into {DCA_CHUNKS} chunks.",
                parse_mode="HTML",
            )
            return

        target_chain = " ".join(args).strip().title()
        start_dca(state, target_chain)
        await update.message.reply_text(
            f"📊 DCA started → <b>{_escape(target_chain)}</b> in {DCA_CHUNKS} chunks.",
            parse_mode="HTML",
        )

    async def _cmd_balances(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        from wallet import load_state, check_all_chain_balances
        state = load_state()
        addr = state.get("address", "")
        if not addr:
            await update.message.reply_text("No wallet configured.")
            return
        await update.message.reply_text("Checking balances...")
        try:
            balances = await asyncio.to_thread(check_all_chain_balances, addr)
            if not balances:
                await update.message.reply_text("No USDC found on any chain.")
                return
            lines = ["💰 <b>Multi-Chain Balances</b>\n"]
            total = 0.0
            for chain_name, bal in sorted(balances.items(), key=lambda x: -x[1]):
                lines.append(f"  <b>{_escape(chain_name)}</b>: ${bal:.2f}")
                total += bal
            lines.append(f"\n  <b>Total</b>: ${total:.2f}")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Failed: {_escape(str(e))}")

    async def _cmd_stoploss(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        from wallet import STOP_LOSS_ENABLED, STOP_LOSS_CYCLES, load_state
        state = load_state()
        count = state.get("_stop_loss_count", 0)
        status = "enabled" if STOP_LOSS_ENABLED else "disabled"
        await update.message.reply_text(
            f"🛑 <b>Stop-Loss</b>\n\n"
            f"Status: <b>{status}</b>\n"
            f"Trigger after: {STOP_LOSS_CYCLES} cycles below threshold\n"
            f"Current count: {count}",
            parse_mode="HTML",
        )

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        if self.agent and "paused" in self.agent:
            if self.agent["paused"]:
                await update.message.reply_text("⏸️ Already paused.")
                return
            self.agent["paused"] = True
            await update.message.reply_text("⏸️ Paused. Use /resume to restart.")
        else:
            await update.message.reply_text("⚠️ Agent loop not connected.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        if self.agent and "paused" in self.agent:
            if not self.agent["paused"]:
                await update.message.reply_text("▶️ Already running.")
                return
            self.agent["paused"] = False
            await update.message.reply_text("▶️ Resumed. Next cycle on schedule.")
        else:
            await update.message.reply_text("⚠️ Agent loop not connected.")

    async def _cmd_wallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._reject_unauthorized(update):
            return
        from wallet import load_wallet, create_wallet, load_state
        from yield_scanner import CHAIN_MAP

        wallet_info = load_wallet()
        if wallet_info:
            addr, _ = wallet_info
            state = load_state()
            chain = state.get("current_chain", 8453)
            chain_name = CHAIN_MAP.get(chain, f"Chain {chain}")
            await update.message.reply_text(
                f"🔑 <b>Wallet</b>\n\n"
                f"<code>{_escape(addr)}</code>\n"
                f"{_escape(chain_name)} — ${state.get('position_usd', 0):.2f}",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("Creating wallet...")
            try:
                addr, _ = create_wallet()
                state = load_state()
                state["address"] = addr
                from wallet import save_state
                save_state(state)
                await update.message.reply_text(
                    f"🔑 <b>Wallet Created</b>\n\n"
                    f"<code>{_escape(addr)}</code>\n\n"
                    "Send USDC on Base to fund Marco.",
                    parse_mode="HTML",
                )
            except Exception as e:
                await update.message.reply_text(f"Failed: {_escape(str(e))}")

    # ---------------------------------------------------------------
    # INTERNAL
    # ---------------------------------------------------------------

    @staticmethod
    def _revoke_approval(
        token_addr: str, spender: str, owner: str, private_key: str, rpc_url: str, chain_id: int,
    ) -> bool:
        """Revoke ERC20 approval by approving 0."""
        from web3 import Web3
        from lifi import ERC20_ABI

        w3 = Web3(Web3.HTTPProvider(rpc_url))
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI,
        )
        owner_cs = Web3.to_checksum_address(owner)
        spender_cs = Web3.to_checksum_address(spender)

        current = contract.functions.allowance(owner_cs, spender_cs).call()
        if current == 0:
            return False

        nonce = w3.eth.get_transaction_count(owner_cs, "pending")
        tx = contract.functions.approve(spender_cs, 0).build_transaction({
            "from": owner_cs, "nonce": nonce, "chainId": chain_id,
        })
        try:
            latest = w3.eth.get_block("latest")
            if hasattr(latest, "baseFeePerGas") and latest.baseFeePerGas:
                tx["maxFeePerGas"] = latest.baseFeePerGas * 2
                tx["maxPriorityFeePerGas"] = w3.to_wei(0.1, "gwei")
        except Exception:
            pass

        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            raise RuntimeError(f"Revoke TX reverted: {tx_hash.hex()}")
        return True

    async def send_journal(self, entry: str):
        """Post a journal entry to the chat."""
        try:
            bot = self.app.bot
            msg = _truncate(f"📖 {_escape(entry)}")
            await bot.send_message(chat_id=self.chat_id, text=msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to send journal: {e}")

    async def send_migration(self, journal: str, decision: dict):
        """Post a migration decision to the chat."""
        moves = decision.get("moves", [])
        confidence = decision.get("confidence", 0)
        action = decision.get("action", "hold")
        risk = decision.get("risk_notes", "")

        emoji = "🏃" if action == "migrate" else "⏸️"
        lines = [
            f"{emoji} <b>{_escape(action.upper())}</b> ({confidence:.0%})\n",
            f"{_escape(journal)}",
        ]
        for m in moves:
            lines.append(
                f"\n  → {_escape(m.get('from_chain', '?'))} → "
                f"{_escape(m.get('to_chain', '?'))} — "
                f"{_escape(m.get('reason', ''))}"
            )
        if risk:
            lines.append(f"\n⚠️ {_escape(risk)}")

        try:
            bot = self.app.bot
            msg = _truncate("\n".join(lines))
            await bot.send_message(chat_id=self.chat_id, text=msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to send migration: {e}")

    async def start(self):
        """Start the bot (non-blocking, runs alongside agent loop)."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        try:
            await self.app.bot.set_my_commands([
                BotCommand("status", "Position, P&L, recent moves"),
                BotCommand("scan", "Live yield opportunities"),
                BotCommand("journal", "Recent decisions"),
                BotCommand("fund", "Deposit address"),
                BotCommand("withdraw", "Send funds to your wallet"),
                BotCommand("strategy", "Risk profile"),
                BotCommand("kill", "Emergency stop"),
                BotCommand("more", "Power commands"),
            ])
            logger.info("Telegram command menu updated")
        except Exception as e:
            logger.warning(f"Failed to set bot commands: {e}")

    async def stop(self):
        """Stop the bot."""
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
