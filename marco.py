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
import protocols
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
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.005"))  # 0.5% default — tight for stablecoins
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.6"))

WEBHOOK_URL = os.getenv("WEBHOOK_URL")

JOURNAL_FILE = Path(__file__).parent / "journal.json"
MAX_JOURNAL_ENTRIES = 100


def load_journal() -> list[str]:
    if JOURNAL_FILE.exists():
        return json.loads(JOURNAL_FILE.read_text())
    return []


def save_journal(entries: list[str]):
    # Cap journal size — keep most recent entries
    if len(entries) > MAX_JOURNAL_ENTRIES:
        entries = entries[-MAX_JOURNAL_ENTRIES:]
    # Atomic write: tmp file + rename (matches wallet.save_state pattern)
    import tempfile
    data = json.dumps(entries, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=JOURNAL_FILE.parent, suffix=".tmp")
    try:
        os.write(fd, data.encode())
        os.close(fd)
        fd = -1
        os.replace(tmp_path, JOURNAL_FILE)
    except Exception:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


async def send_telegram(client: httpx.AsyncClient, message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log(f"Telegram send failed: {e}")


async def send_webhook(client: httpx.AsyncClient, payload: dict):
    if not WEBHOOK_URL:
        return
    try:
        await client.post(WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        log(f"Webhook failed: {e}")


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def _banner():
    """Print startup banner for demo/video recording."""
    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║        🏜️  Marco the Nomad  🏜️          ║")
    print("  ║   Autonomous Cross-Chain Yield Agent     ║")
    print("  ║   Powered by LI.FI + Claude + DefiLlama ║")
    print("  ╚══════════════════════════════════════════╝")
    print()


_cycle_lock = asyncio.Lock()


async def run_cycle():
    """Run one decision cycle. Lock prevents concurrent execution from Telegram triggers."""
    if _cycle_lock.locked():
        log("Cycle already running — skipping triggered cycle")
        return
    async with _cycle_lock:
        await _run_cycle_inner()


async def _run_cycle_inner():
    """Inner cycle logic (always called under _cycle_lock)."""
    print()
    log("─── Cycle Start ───────────────────────────────")
    state = wallet.load_state()
    log(f"Position: ${state['position_usd']:.2f} {state.get('current_token', 'USDC')} on {CHAIN_MAP.get(state['current_chain'], '?')}")
    pool = state.get("current_pool")
    if pool:
        log(f"Pool: {pool.get('symbol','?')} ({pool.get('project','?')}) — {pool.get('apy',0):.1f}% APY")

    # AAR-100: Load strategy profile and apply to scan params
    strategy_name = wallet.get_strategy(state)
    strategy = wallet.STRATEGY_PROFILES.get(strategy_name, wallet.STRATEGY_PROFILES["balanced"])
    scan_min_tvl = strategy.get("min_tvl", MIN_TVL)
    scan_min_apy = strategy.get("min_apy", MIN_APY)
    bridge_cost_cap = strategy.get("max_bridge_cost_pct", MAX_BRIDGE_COST_PCT)
    confidence_gate = strategy.get("min_confidence", MIN_CONFIDENCE)
    trusted_only = strategy.get("trusted_only", False)
    log(f"Strategy: {strategy_name}")

    # SAFETY: Check for pending TX from a previous crash
    pending = state.get("_pending_tx")
    if pending:
        log(f"  WARNING: Found pending TX from {pending.get('timestamp', '?')}.")
        log(f"  Previous bridge may have completed — reconciliation will verify on-chain balance.")
        # Don't clear it here — let reconciliation handle it

    # Check for drift alerts from previous cycle
    drift_alert = state.get("_drift_alert")
    if drift_alert:
        log(f"  ALERT: Unresolved balance drift from {drift_alert.get('timestamp', '?')}: "
            f"on-chain ${drift_alert.get('on_chain', '?')} vs tracked ${drift_alert.get('tracked', '?')}")

    # Reconcile tracked balance with on-chain reality (LIVE mode only)
    # Run in thread to avoid blocking the event loop (web3 calls are sync)
    if not DEMO_MODE:
        rpc_url = lifi.RPC_URLS.get(state["current_chain"])
        if rpc_url:
            drift = await asyncio.to_thread(wallet.reconcile_balance, state, rpc_url)
            if drift is not None and abs(drift) > 0.01:
                log(f"Balance reconciled: drift was ${drift:+.2f} (now ${state['position_usd']:.2f})")

    async with httpx.AsyncClient() as client:
        # 1. Scan yields
        log("Scanning yields...")
        candidates = await yield_scanner.scan_yields(
            client, chains=SCAN_CHAINS, min_tvl=scan_min_tvl, min_apy=scan_min_apy
        )
        # AAR-100: Filter to trusted protocols only if strategy requires it
        if trusted_only:
            candidates = [p for p in candidates if p.get("_trusted")]

        try:
            from yield_db import record_snapshot
            record_snapshot(candidates)
        except Exception:
            pass  # Non-critical

        if not candidates:
            log("No yields found matching criteria.")
            return

        log(f"Found {len(candidates)} opportunities. Top 5:")
        for p in candidates[:5]:
            log(f"  {yield_scanner.format_pool(p)}")

        # 2. Get bridge/swap quotes for all potential moves
        log("Getting bridge + swap quotes...")
        wallet_addr = state.get("address") or "0x0000000000000000000000000000000000000001"
        current_chain = state["current_chain"]
        current_token = state.get("current_token", "USDC")
        position_usd = state.get("position_usd", POSITION_SIZE)

        bridge_quotes = {}  # quote_key -> {cost, quote, cost_pct, type}

        # AAR-95: Adaptive slippage based on position vs pool liquidity
        avg_tvl = sum(p.get("tvlUsd", 0) for p in candidates[:5]) / max(len(candidates[:5]), 1)
        adaptive_slip = wallet.calc_adaptive_slippage({"tvlUsd": avg_tvl}, position_usd)
        effective_slippage = adaptive_slip if adaptive_slip != SLIPPAGE else SLIPPAGE

        # Resolve current token address + decimals
        current_stable = wallet.STABLECOINS.get((current_chain, current_token))
        current_token_addr = current_stable["address"] if current_stable else wallet.USDC.get(current_chain)
        current_token_decimals = current_stable["decimals"] if current_stable else wallet.USDC_DECIMALS

        async def _fetch_quote(quote_key, from_chain, to_chain, from_token, to_token, from_decimals, move_type):
            amount_wei = str(int(position_usd * 10**from_decimals))
            try:
                # AAR-90: Multi-route bridge comparison — try multiple routes for bridges
                if move_type == "bridge":
                    quotes = await lifi.get_quotes_multi(
                        client, from_chain, to_chain, from_token, to_token,
                        amount_wei, wallet_addr, slippage=effective_slippage, api_key=LIFI_API_KEY,
                    )
                    if quotes:
                        # Pick cheapest route
                        best = min(quotes, key=lambda q: lifi.calc_bridge_cost(q)["total_cost_usd"])
                        cost = lifi.calc_bridge_cost(best)
                        cost_pct = (cost["total_cost_usd"] / position_usd * 100) if position_usd > 0 else 0
                        bridge_quotes[quote_key] = {"cost": cost, "quote": best, "cost_pct": cost_pct, "type": move_type}
                        return
                # Fall through to single-quote logic (swaps, or if multi failed for bridges)
                quote = await lifi.get_quote(
                    client, from_chain, to_chain, from_token, to_token,
                    amount_wei, wallet_addr, slippage=effective_slippage, api_key=LIFI_API_KEY,
                )
                cost = lifi.calc_bridge_cost(quote)
                cost_pct = (cost["total_cost_usd"] / position_usd * 100) if position_usd > 0 else 0
                bridge_quotes[quote_key] = {
                    "cost": cost, "quote": quote, "cost_pct": cost_pct, "type": move_type,
                }
            except Exception as e:
                log(f"  -> {quote_key}: quote failed ({e})")

        # Build quote tasks — bridges (cross-chain) + swaps (same-chain, different token)
        quote_tasks = []
        seen_bridges = set()  # chain IDs we've already quoted
        seen_swaps = set()    # (chain_id, token_symbol) pairs we've already quoted

        for pool in candidates[:10]:
            pool_chain_name = pool.get("chain", "")
            target_chain = CHAIN_MAP_REVERSE.get(pool_chain_name)
            if not target_chain:
                continue

            pool_token = wallet._infer_pool_token(pool)

            if target_chain != current_chain:
                # Cross-chain bridge: current token → USDC on target (always land in USDC)
                if target_chain not in seen_bridges:
                    seen_bridges.add(target_chain)
                    to_token = wallet.USDC.get(target_chain)
                    if current_token_addr and to_token:
                        quote_tasks.append(_fetch_quote(
                            pool_chain_name, current_chain, target_chain,
                            current_token_addr, to_token, current_token_decimals, "bridge",
                        ))
            elif pool_token != current_token:
                # Same-chain swap: current token → pool's token via LI.FI
                swap_key = (target_chain, pool_token)
                if swap_key not in seen_swaps:
                    seen_swaps.add(swap_key)
                    target_stable = wallet.STABLECOINS.get((target_chain, pool_token))
                    if current_token_addr and target_stable:
                        quote_key = f"{pool_chain_name} swap {current_token}→{pool_token}"
                        quote_tasks.append(_fetch_quote(
                            quote_key, current_chain, current_chain,
                            current_token_addr, target_stable["address"],
                            current_token_decimals, "swap",
                        ))

        if quote_tasks:
            await asyncio.gather(*quote_tasks)

        # Log results
        for chain_name, qd in bridge_quotes.items():
            cost = qd["cost"]
            log(f"  -> {chain_name}: ${cost['total_cost_usd']:.2f} ({qd['cost_pct']:.2f}% of position) via {cost['bridge']}")
            if qd["cost_pct"] > bridge_cost_cap:
                log(f"     WARNING: exceeds {bridge_cost_cap}% threshold")

        # 3. Refresh current pool APY from live data (stored APY is from migration time)
        # Search ALL pools (not just filtered candidates) because the current pool's
        # APY might have dropped below MIN_APY — exactly when we need accurate data
        current_pool = state.get("current_pool")
        if current_pool:
            all_pools = yield_scanner._pool_cache  # Already fetched by scan_yields
            pool_id = current_pool.get("pool_id")
            live_match = next(
                (p for p in all_pools if p.get("pool") == pool_id),
                None,
            ) if pool_id else None
            # Fallback: match by symbol + project + chain
            if not live_match:
                live_match = next(
                    (p for p in all_pools
                     if p.get("symbol") == current_pool.get("symbol")
                     and p.get("project") == current_pool.get("project")
                     and p.get("chain") == current_pool.get("chain")),
                    None,
                )
            if live_match:
                old_apy = current_pool.get("apy", 0)
                new_apy = live_match.get("apy", 0)
                if abs(old_apy - new_apy) > 0.1:
                    log(f"  Current pool APY updated: {old_apy:.2f}% -> {new_apy:.2f}% (live)")
                current_pool["apy"] = new_apy
                state["current_pool"] = current_pool

        # 4. Enrich candidates with bridge/swap cost data for brain
        current_chain_name = CHAIN_MAP.get(current_chain, "")
        for pool in candidates:
            chain_name = pool.get("chain", "")
            pool_token = wallet._infer_pool_token(pool)

            if chain_name == current_chain_name and pool_token == current_token:
                # Same-chain, same token — no move needed, zero cost
                pool["bridge_cost_usd"] = 0.0
                pool["bridge_cost_pct"] = 0.0
                pool["bridge_tool"] = "same-chain (no bridge)"
                pool["_move_type"] = "rebalance"
            elif chain_name == current_chain_name and pool_token != current_token:
                # Same-chain swap via LI.FI
                swap_key = f"{chain_name} swap {current_token}→{pool_token}"
                if swap_key in bridge_quotes:
                    pool["bridge_cost_usd"] = bridge_quotes[swap_key]["cost"]["total_cost_usd"]
                    pool["bridge_cost_pct"] = bridge_quotes[swap_key]["cost_pct"]
                    pool["bridge_tool"] = bridge_quotes[swap_key]["cost"]["bridge"]
                    pool["_move_type"] = "swap"
                else:
                    pool["_move_type"] = "swap"
            elif chain_name in bridge_quotes:
                pool["bridge_cost_usd"] = bridge_quotes[chain_name]["cost"]["total_cost_usd"]
                pool["bridge_cost_pct"] = bridge_quotes[chain_name]["cost_pct"]
                pool["bridge_tool"] = bridge_quotes[chain_name]["cost"]["bridge"]
                pool["_move_type"] = "bridge"

        # 5. Build portfolio view using actual position size
        chain_name = CHAIN_MAP.get(current_chain, f"Chain {current_chain}")
        portfolio = {chain_name: {current_token.lower(): position_usd, "native": 0}}

        # 6. Ask Marco's brain
        log("─── Marco is thinking... ─────────────────────")
        journal_entries = load_journal()
        result = await brain.decide(
            portfolio, candidates, journal_entries[-3:],
            current_pool=state.get("current_pool"),
            bridge_cost_cap_pct=bridge_cost_cap,
        )

        journal_text = result["journal"]
        decision = result["decision"]

        confidence = decision.get("confidence", 0.5)
        risk_notes = decision.get("risk_notes", "")
        action = decision.get("action", "hold").upper()
        action_icon = "🏃" if action == "MIGRATE" else "⏸️"
        log(f"─── Decision: {action_icon} {action} (confidence: {confidence:.0%}) ──")
        if risk_notes:
            log(f"Risk: {risk_notes}")
        log(f"Journal: {journal_text[:200]}")

        # 7. Record journal entry (include risk notes if present)
        entry = f"[{datetime.now().isoformat()}] {journal_text}"
        if risk_notes:
            entry += f" [RISK: {risk_notes}]"
        journal_entries.append(entry)
        save_journal(journal_entries)

        # 8. Execute moves if migrating (gate on confidence)
        if decision.get("action") == "migrate" and decision.get("moves") and confidence < confidence_gate:
            log(f"  BLOCKED: confidence {confidence:.0%} < {confidence_gate:.0%} threshold — holding instead")
        elif decision.get("action") == "migrate" and decision.get("moves"):
            # Single-position model: only execute the first move
            for move in decision["moves"][:1]:
                to_chain_name = move.get("to_chain", "")
                target_chain_id = CHAIN_MAP_REVERSE.get(to_chain_name)

                # Fuzzy match: brain might output "Arbitrum One" instead of "Arbitrum"
                if not target_chain_id:
                    to_lower = to_chain_name.lower()
                    for known_name, chain_id in CHAIN_MAP_REVERSE.items():
                        if to_lower in known_name.lower() or known_name.lower() in to_lower:
                            target_chain_id = chain_id
                            to_chain_name = known_name  # Normalize to exact name
                            log(f"  Fuzzy matched '{move.get('to_chain')}' -> '{known_name}'")
                            break

                target_pool = next(
                    (p for p in candidates if p.get("chain") == to_chain_name), None
                )
                if not target_pool or not target_chain_id:
                    log(f"  Skip {to_chain_name}: no pool or chain ID found")
                    continue

                # Determine move type and find the right quote
                target_pool_token = wallet._infer_pool_token(target_pool)
                is_same_chain = target_chain_id == current_chain
                is_swap = is_same_chain and target_pool_token != current_token

                if is_swap:
                    swap_key = f"{to_chain_name} swap {current_token}→{target_pool_token}"
                    quote_data = bridge_quotes.get(swap_key)
                else:
                    quote_data = bridge_quotes.get(to_chain_name)

                # Enforce bridge/swap cost threshold
                if quote_data and quote_data["cost_pct"] > bridge_cost_cap:
                    log(f"  BLOCKED: {to_chain_name} cost {quote_data['cost_pct']:.2f}% exceeds {bridge_cost_cap}% limit")
                    continue

                cost_usd = quote_data["cost"]["total_cost_usd"] if quote_data else 0

                # Full position move — wallet model is single-chain
                move_usd = position_usd

                # Pool deposit engine: withdraw from current pool before migrating
                protocols.ensure_loaded()
                deposited_pool = state.get("_deposited_pool")
                if deposited_pool and not DEMO_MODE:
                    adapter = protocols.get_adapter(deposited_pool.get("protocol", ""))
                    if adapter:
                        log(f"  Withdrawing from {deposited_pool['protocol']} pool before migration...")
                        try:
                            rpc_url = lifi.RPC_URLS.get(current_chain)
                            from web3 import Web3
                            w3 = Web3(Web3.HTTPProvider(rpc_url))
                            wallet_info = wallet.load_wallet()
                            if wallet_info:
                                wd_result = await adapter.withdraw(
                                    w3, None,  # None = withdraw all
                                    current_token_addr, wallet_addr,
                                    wallet_info[1], current_chain,
                                )
                                log(f"  Withdrawn: TX {wd_result.tx_hash[:16]}...")
                                state.pop("_deposited_pool", None)
                                wallet.save_state(state)
                                # Update position from on-chain balance after withdraw
                                actual = await asyncio.to_thread(
                                    wallet.check_onchain_balance,
                                    current_chain, wallet_addr, rpc_url, token=current_token,
                                )
                                if actual is not None:
                                    state["position_usd"] = round(actual, 2)
                                    position_usd = state["position_usd"]
                                    move_usd = position_usd
                                    wallet.save_state(state)
                                    log(f"  Post-withdraw balance: ${actual:.2f}")
                        except Exception as e:
                            log(f"  WARNING: Pool withdrawal failed: {e} — continuing with bridge")

                # Safety check: min balance and migration cooldown
                allowed, block_reason = wallet.can_migrate(state, cost_usd)
                if not allowed:
                    log(f"  SAFETY BLOCK: {block_reason}")
                    continue

                # AAR-85: TWAP execution — split large positions into chunks
                use_twap = (
                    wallet.TWAP_ENABLED
                    and move_usd > wallet.TWAP_MIN_POSITION_USD
                    and not DEMO_MODE
                )

                if DEMO_MODE:
                    log(f"  [DEMO] Would migrate to {to_chain_name} via LI.FI (cost: ${cost_usd:.2f})")
                    if wallet.TWAP_ENABLED and move_usd > wallet.TWAP_MIN_POSITION_USD:
                        log(f"  [DEMO] TWAP: would split into {wallet.TWAP_CHUNKS} chunks over {wallet.TWAP_CHUNKS * wallet.TWAP_INTERVAL_SECONDS}s")
                else:
                    # Re-fetch quote right before execution — the original was fetched
                    # before brain.decide() which adds 5-30s of staleness
                    move_label = "swap" if is_swap else "bridge"
                    log(f"  Re-fetching fresh {move_label} quote for {to_chain_name}...")

                    # Resolve correct from/to token addresses for this move type
                    exec_from_token = current_token_addr
                    if is_swap:
                        target_stable = wallet.STABLECOINS.get((target_chain_id, target_pool_token))
                        exec_to_token = target_stable["address"] if target_stable else None
                        exec_to_chain = current_chain  # Same-chain swap
                    else:
                        exec_to_token = wallet.USDC.get(target_chain_id)
                        exec_to_chain = target_chain_id

                    if not exec_from_token or not exec_to_token:
                        log(f"  ABORT: Cannot resolve token addresses for {move_label}")
                        continue

                    amount_wei = str(int(move_usd * 10**current_token_decimals))
                    try:
                        fresh_quote = await lifi.get_quote(
                            client, current_chain, exec_to_chain,
                            exec_from_token, exec_to_token, amount_wei, wallet_addr,
                            slippage=effective_slippage, api_key=LIFI_API_KEY,
                        )
                        fresh_cost = lifi.calc_bridge_cost(fresh_quote)
                        fresh_cost_pct = (fresh_cost["total_cost_usd"] / move_usd * 100) if move_usd > 0 else 0
                        if fresh_cost_pct > bridge_cost_cap:
                            log(f"  BLOCKED: fresh quote cost {fresh_cost_pct:.2f}% exceeds limit")
                            continue
                        # AAR-83: Gas estimate vs position size validation
                        # Abort if gas alone exceeds 5% of position
                        gas_usd = fresh_cost.get("gas_usd", 0)
                        if gas_usd > position_usd * 0.05:
                            log(f"  ABORT: gas cost ${gas_usd:.4f} > 5% of position ${position_usd:.2f} (${position_usd * 0.05:.4f} limit)")
                            continue

                        cost_usd = fresh_cost["total_cost_usd"]
                        log(f"  Fresh quote: ${cost_usd:.2f} ({fresh_cost.get('spread_usd', 0):.2f} spread) via {fresh_cost['bridge']}")
                    except Exception as e:
                        log(f"  Fresh quote failed ({e}), aborting migration to {to_chain_name}")
                        continue

                    # AAR-80: Pre-TX on-chain balance verification
                    # Prevents executing when tracked state diverges from reality
                    pre_tx_rpc = lifi.RPC_URLS.get(current_chain)
                    if pre_tx_rpc:
                        on_chain_bal = await asyncio.to_thread(
                            wallet.check_onchain_balance,
                            current_chain, wallet_addr, pre_tx_rpc,
                            token=current_token,
                        )
                        if on_chain_bal is not None and on_chain_bal < position_usd * 0.90:
                            log(f"  ABORT: on-chain balance ${on_chain_bal:.2f} < 90% of position ${position_usd:.2f} — state diverged")
                            continue

                    # Execute the bridge transaction on-chain
                    wallet_info = wallet.load_wallet()
                    private_key = wallet_info[1] if wallet_info else None
                    rpc_url = lifi.RPC_URLS.get(current_chain)
                    if not private_key or not rpc_url:
                        log(f"  ABORT: Missing WALLET_PRIVATE_KEY or RPC URL for chain {current_chain}")
                        continue
                    # SECURITY: Verify key matches wallet before every TX
                    match_ok, match_msg = wallet.check_wallet_address_match(state, private_key)
                    if not match_ok:
                        log(f"  ABORT: {match_msg}")
                        continue
                    log(f"  Executing {move_label} to {to_chain_name}...")

                    # AAR-89: Gas-aware execution — check gas price before TX
                    gas_ok, gas_gwei = wallet.check_gas_price(current_chain, rpc_url)
                    if not gas_ok:
                        log(f"  ABORT: Gas price {gas_gwei:.2f} gwei exceeds threshold — delaying migration")
                        state.pop("_pending_tx", None)
                        wallet.save_state(state)
                        continue

                    # AAR-85: TWAP execution — split into chunks for large positions
                    if use_twap:
                        num_chunks = wallet.TWAP_CHUNKS
                        chunk_amount = move_usd / num_chunks
                        total_twap_cost = 0.0
                        twap_failed = False
                        log(f"  TWAP: splitting ${move_usd:.2f} into {num_chunks} chunks of ${chunk_amount:.2f}")

                        for chunk_i in range(num_chunks):
                            log(f"  TWAP chunk {chunk_i + 1}/{num_chunks}: ${chunk_amount:.2f}")
                            # Get fresh quote for this chunk
                            chunk_wei = str(int(chunk_amount * 10**current_token_decimals))
                            try:
                                chunk_quote = await lifi.get_quote(
                                    client, current_chain, exec_to_chain,
                                    exec_from_token, exec_to_token, chunk_wei, wallet_addr,
                                    slippage=effective_slippage, api_key=LIFI_API_KEY,
                                )
                                chunk_cost_info = lifi.calc_bridge_cost(chunk_quote)
                                chunk_cost = chunk_cost_info["total_cost_usd"]
                            except Exception as e:
                                log(f"  TWAP chunk {chunk_i + 1} quote failed: {e} — stopping TWAP")
                                twap_failed = True
                                break

                            # Record pending and execute
                            state["_pending_tx"] = {
                                "target_chain": target_chain_id,
                                "target_pool_token": target_pool_token,
                                "estimated_cost": chunk_cost,
                                "timestamp": datetime.now().isoformat(),
                                "twap_chunk": chunk_i + 1,
                            }
                            wallet.save_state(state)

                            try:
                                chunk_result = await lifi.execute_quote(
                                    chunk_quote, private_key, rpc_url,
                                    poll_status_client=client, api_key=LIFI_API_KEY,
                                )
                                log(f"  TWAP chunk {chunk_i + 1} TX: {chunk_result['tx_hash']} status={chunk_result['status']}")
                                if chunk_result["status"] == "FAILED":
                                    log(f"  TWAP chunk {chunk_i + 1} FAILED — stopping TWAP")
                                    twap_failed = True
                                    state.pop("_pending_tx", None)
                                    wallet.save_state(state)
                                    break
                            except Exception as e:
                                log(f"  TWAP chunk {chunk_i + 1} execution failed: {e} — stopping TWAP")
                                twap_failed = True
                                state.pop("_pending_tx", None)
                                wallet.save_state(state)
                                break

                            total_twap_cost += chunk_cost
                            state["position_usd"] = round(state["position_usd"] - chunk_cost, 2)
                            state.pop("_pending_tx", None)
                            wallet.save_state(state)
                            log(f"  TWAP chunk {chunk_i + 1} complete (cost: ${chunk_cost:.2f})")

                            # Wait between chunks (except after the last one)
                            if chunk_i < num_chunks - 1:
                                log(f"  TWAP: waiting {wallet.TWAP_INTERVAL_SECONDS}s before next chunk...")
                                await asyncio.sleep(wallet.TWAP_INTERVAL_SECONDS)

                        cost_usd = total_twap_cost
                        if twap_failed:
                            log(f"  TWAP stopped early — completed {chunk_i}/{num_chunks} chunks, total cost: ${total_twap_cost:.2f}")
                            # Still record partial migration below

                    else:
                        # Standard (non-TWAP) execution
                        # SAFETY: Record pending TX in state BEFORE execution.
                        # If process crashes mid-bridge, this prevents double-spend on restart.
                        state["_pending_tx"] = {
                            "target_chain": target_chain_id,
                            "target_pool_token": target_pool_token,
                            "estimated_cost": cost_usd,
                            "timestamp": datetime.now().isoformat(),
                        }
                        wallet.save_state(state)

                        try:
                            tx_result = await lifi.execute_quote(
                                fresh_quote, private_key, rpc_url,
                                poll_status_client=client, api_key=LIFI_API_KEY,
                            )
                            log(f"  TX: {tx_result['tx_hash']} status={tx_result['status']}")
                            if tx_result["status"] == "FAILED":
                                log(f"  Bridge TX FAILED — not recording migration")
                                state.pop("_pending_tx", None)
                                wallet.save_state(state)
                                continue
                        except Exception as e:
                            log(f"  Bridge execution failed: {e}")
                            state.pop("_pending_tx", None)
                            wallet.save_state(state)
                            continue

                        # Handle PENDING status (polling timed out — bridge may still land)
                        if tx_result["status"] == "PENDING":
                            log(f"  WARNING: Bridge status still PENDING after polling timeout. TX: {tx_result['tx_hash']}")
                            log(f"  Funds may be in transit. Recording migration with estimated cost — reconcile will correct next cycle.")
                            # Record with estimate — reconcile_balance will fix on next cycle
                            state["position_usd"] = round(state["position_usd"] - cost_usd, 2)

                        # Verify received amount on destination chain (only for DONE status)
                        elif tx_result["status"] == "DONE":
                            dest_rpc = lifi.RPC_URLS.get(target_chain_id)
                            if dest_rpc:
                                # Check balance of the token we're landing in
                                dest_token = target_pool_token if is_swap else "USDC"
                                actual = await asyncio.to_thread(
                                    wallet.check_onchain_balance,
                                    target_chain_id, wallet_addr, dest_rpc,
                                    token=dest_token,
                                )
                                if actual is not None:
                                    expected_min = fresh_cost["to_amount_min"]
                                    if actual < expected_min * 0.95:
                                        log(f"  WARNING: received ${actual:.2f} < expected min ${expected_min:.2f}")
                                    else:
                                        log(f"  Verified: ${actual:.2f} received on {to_chain_name}")
                                    state["position_usd"] = round(actual, 2)
                                    cost_usd = round(move_usd - actual, 2)
                                else:
                                    state["position_usd"] = round(state["position_usd"] - cost_usd, 2)
                            else:
                                state["position_usd"] = round(state["position_usd"] - cost_usd, 2)

                if DEMO_MODE:
                    # DEMO: simulate cost deduction
                    state["position_usd"] = round(state["position_usd"] - cost_usd, 2)
                state.pop("_pending_tx", None)  # Clear pending flag
                wallet.record_migration(
                    state, current_chain, target_chain_id, target_pool,
                    cost_usd, move.get("reason", journal_text[:100]),
                )
                log(f"  Migration recorded: -> {to_chain_name} (cost: ${cost_usd:.2f}, position now: ${state['position_usd']:.2f})")

                # Pool deposit engine: deposit into new pool after migration
                target_project = (target_pool.get("project") or "").lower()
                adapter = protocols.get_adapter(target_project)
                if adapter and adapter.supports_chain(target_chain_id) and not DEMO_MODE:
                    log(f"  Depositing into {target_project} pool on {to_chain_name}...")
                    try:
                        dest_rpc = lifi.RPC_URLS.get(target_chain_id)
                        from web3 import Web3
                        w3 = Web3(Web3.HTTPProvider(dest_rpc))
                        wallet_info = wallet.load_wallet()
                        if wallet_info:
                            dest_token = target_pool_token if is_swap else "USDC"
                            dest_stable = wallet.STABLECOINS.get((target_chain_id, dest_token))
                            dest_token_addr = dest_stable["address"] if dest_stable else wallet.USDC.get(target_chain_id)
                            dest_decimals = dest_stable["decimals"] if dest_stable else wallet.USDC_DECIMALS
                            deposit_amount_raw = int(state["position_usd"] * 10**dest_decimals)
                            dep_result = await adapter.deposit(
                                w3, deposit_amount_raw, dest_token_addr,
                                wallet_addr, wallet_info[1], target_chain_id,
                            )
                            log(f"  Deposited ${dep_result.amount_deposited:.2f} into {target_project}: TX {dep_result.tx_hash[:16]}...")
                            state["_deposited_pool"] = {
                                "protocol": target_project,
                                "pool_address": adapter.POOL_ADDRESSES.get(target_chain_id, ""),
                                "receipt_token": dep_result.receipt_token,
                                "deposited_amount": dep_result.amount_deposited,
                                "deposited_at": datetime.now().isoformat(),
                                "chain_id": target_chain_id,
                            }
                            wallet.save_state(state)
                    except Exception as e:
                        log(f"  WARNING: Pool deposit failed: {e} — funds are on-chain but not deposited")
                elif DEMO_MODE and adapter and adapter.supports_chain(target_chain_id):
                    log(f"  [DEMO] Would deposit into {target_project} pool on {to_chain_name}")

        # 9. Notify
        telegram_msg = (
            f"*Marco's Journal*\n"
            f"_{datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n"
            f"{journal_text}\n\n"
            f"Action: *{decision.get('action', 'hold').upper()}*"
        )
        await send_telegram(client, telegram_msg)

        # 10. Webhook notification
        await send_webhook(client, {
            "timestamp": datetime.now().isoformat(),
            "action": decision.get("action", "hold"),
            "confidence": decision.get("confidence", 0.5),
            "journal": journal_text,
            "position_usd": state.get("position_usd", 0),
            "chain": CHAIN_MAP.get(state.get("current_chain", 8453), "?"),
        })


async def main():
    _banner()
    mode = "DEMO (simulated)" if DEMO_MODE else "LIVE (real transactions)"
    chain_names = [CHAIN_MAP.get(c, str(c)) for c in SCAN_CHAINS]
    log(f"Mode: {mode}")
    log(f"Scanning: {', '.join(chain_names)}")
    log(f"Filters: TVL ≥ ${MIN_TVL:,.0f} | APY ≥ {MIN_APY}% | Bridge cap: {MAX_BRIDGE_COST_PCT}%")
    log(f"Slippage: {SLIPPAGE*100:.1f}% | Confidence gate: {MIN_CONFIDENCE:.0%} | Cycle: {LOOP_INTERVAL}s")

    # SECURITY: Validate wallet configuration at startup (LIVE mode only)
    if not DEMO_MODE:
        # Try loading wallet from ~/.marco/, ~/.conway/, or WALLET_PRIVATE_KEY env
        wallet_info = wallet.load_wallet()
        if not wallet_info:
            log("FATAL: No wallet found. Run with Telegram bot (/wallet) or set WALLET_PRIVATE_KEY.")
            sys.exit(1)
        wallet_addr, private_key = wallet_info
        state = wallet.load_state()
        match_ok, match_msg = wallet.check_wallet_address_match(state, private_key)
        if not match_ok:
            log(f"FATAL: {match_msg}")
            sys.exit(1)
        log(f"Wallet: {wallet_addr[:10]}...{wallet_addr[-6:]}")

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
        next_min = LOOP_INTERVAL // 60
        log(f"─── Sleeping {next_min}m until next cycle ────────────")
        await asyncio.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
