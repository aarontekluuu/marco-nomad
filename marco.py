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
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.005"))  # 0.5% default — tight for stablecoins
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.6"))

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


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


async def run_cycle():
    """Run one decision cycle."""
    state = wallet.load_state()
    log(f"Current state:\n{wallet.format_state(state)}")

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
        position_usd = state.get("position_usd", POSITION_SIZE)

        bridge_quotes = {}  # chain_name -> {cost, quote, cost_pct}

        # Build quote tasks for parallel execution
        async def _fetch_quote(pool_chain_name, target_chain_id):
            from_token = wallet.USDC.get(current_chain)
            to_token = wallet.USDC.get(target_chain_id)
            if not from_token or not to_token:
                return
            amount_wei = str(int(position_usd * 10**wallet.USDC_DECIMALS))
            try:
                quote = await lifi.get_quote(
                    client, current_chain, target_chain_id, from_token, to_token,
                    amount_wei, wallet_addr, slippage=SLIPPAGE, api_key=LIFI_API_KEY,
                )
                cost = lifi.calc_bridge_cost(quote)
                cost_pct = (cost["total_cost_usd"] / position_usd * 100) if position_usd > 0 else 0
                bridge_quotes[pool_chain_name] = {"cost": cost, "quote": quote, "cost_pct": cost_pct}
            except Exception as e:
                log(f"  -> {pool_chain_name}: quote failed ({e})")

        # Fetch all quotes in parallel
        quote_tasks = []
        seen_chains = set()
        for pool in candidates[:10]:
            target_chain = CHAIN_MAP_REVERSE.get(pool.get("chain"))
            if not target_chain or target_chain == current_chain or target_chain in seen_chains:
                continue
            seen_chains.add(target_chain)
            quote_tasks.append(_fetch_quote(pool["chain"], target_chain))

        if quote_tasks:
            await asyncio.gather(*quote_tasks)

        # Log results
        for chain_name, qd in bridge_quotes.items():
            cost = qd["cost"]
            log(f"  -> {chain_name}: ${cost['total_cost_usd']:.2f} ({qd['cost_pct']:.2f}% of position) via {cost['bridge']}")
            if qd["cost_pct"] > MAX_BRIDGE_COST_PCT:
                log(f"     WARNING: exceeds {MAX_BRIDGE_COST_PCT}% threshold")

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

        # 4. Enrich candidates with bridge cost data for brain
        for pool in candidates:
            chain_name = pool.get("chain", "")
            if chain_name in bridge_quotes:
                pool["bridge_cost_usd"] = bridge_quotes[chain_name]["cost"]["total_cost_usd"]
                pool["bridge_cost_pct"] = bridge_quotes[chain_name]["cost_pct"]
                pool["bridge_tool"] = bridge_quotes[chain_name]["cost"]["bridge"]

        # 5. Build portfolio view using actual position size
        chain_name = CHAIN_MAP.get(current_chain, f"Chain {current_chain}")
        portfolio = {chain_name: {"usdc": position_usd, "native": 0}}

        # 6. Ask Marco's brain
        log("Marco is thinking...")
        journal_entries = load_journal()
        result = await brain.decide(
            portfolio, candidates, journal_entries[-3:],
            current_pool=state.get("current_pool"),
            bridge_cost_cap_pct=MAX_BRIDGE_COST_PCT,
        )

        journal_text = result["journal"]
        decision = result["decision"]

        confidence = decision.get("confidence", 0.5)
        risk_notes = decision.get("risk_notes", "")
        log(f"Decision: {decision.get('action', 'hold').upper()} (confidence: {confidence:.0%})")
        if risk_notes:
            log(f"Risk notes: {risk_notes}")
        log(f"Journal: {journal_text[:200]}...")

        # 7. Record journal entry (include risk notes if present)
        entry = f"[{datetime.now().isoformat()}] {journal_text}"
        if risk_notes:
            entry += f" [RISK: {risk_notes}]"
        journal_entries.append(entry)
        save_journal(journal_entries)

        # 8. Execute moves if migrating (gate on confidence)
        if decision.get("action") == "migrate" and decision.get("moves") and confidence < MIN_CONFIDENCE:
            log(f"  BLOCKED: confidence {confidence:.0%} < {MIN_CONFIDENCE:.0%} threshold — holding instead")
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

                # Enforce bridge cost threshold
                quote_data = bridge_quotes.get(to_chain_name)
                if quote_data and quote_data["cost_pct"] > MAX_BRIDGE_COST_PCT:
                    log(f"  BLOCKED: {to_chain_name} bridge cost {quote_data['cost_pct']:.2f}% exceeds {MAX_BRIDGE_COST_PCT}% limit")
                    continue

                cost_usd = quote_data["cost"]["total_cost_usd"] if quote_data else 0

                # Full position move — wallet model is single-chain
                move_usd = position_usd

                # Safety check: min balance and migration cooldown
                allowed, block_reason = wallet.can_migrate(state, cost_usd)
                if not allowed:
                    log(f"  SAFETY BLOCK: {block_reason}")
                    continue

                if DEMO_MODE:
                    log(f"  [DEMO] Would migrate to {to_chain_name} via LI.FI (cost: ${cost_usd:.2f})")
                else:
                    # Re-fetch quote right before execution — the original was fetched
                    # before brain.decide() which adds 5-30s of staleness
                    log(f"  Re-fetching fresh quote for {to_chain_name}...")
                    from_token = wallet.USDC.get(current_chain)
                    to_token = wallet.USDC.get(target_chain_id)
                    amount_wei = str(int(move_usd * 10**wallet.USDC_DECIMALS))
                    try:
                        fresh_quote = await lifi.get_quote(
                            client, current_chain, target_chain_id,
                            from_token, to_token, amount_wei, wallet_addr,
                            slippage=SLIPPAGE, api_key=LIFI_API_KEY,
                        )
                        fresh_cost = lifi.calc_bridge_cost(fresh_quote)
                        fresh_cost_pct = (fresh_cost["total_cost_usd"] / move_usd * 100) if move_usd > 0 else 0
                        if fresh_cost_pct > MAX_BRIDGE_COST_PCT:
                            log(f"  BLOCKED: fresh quote cost {fresh_cost_pct:.2f}% exceeds limit")
                            continue
                        cost_usd = fresh_cost["total_cost_usd"]
                        log(f"  Fresh quote: ${cost_usd:.2f} ({fresh_cost.get('spread_usd', 0):.2f} spread) via {fresh_cost['bridge']}")
                    except Exception as e:
                        log(f"  Fresh quote failed ({e}), aborting migration to {to_chain_name}")
                        continue

                    # Execute the bridge transaction on-chain
                    private_key = os.getenv("WALLET_PRIVATE_KEY")
                    rpc_url = lifi.RPC_URLS.get(current_chain)
                    if not private_key or not rpc_url:
                        log(f"  ABORT: Missing WALLET_PRIVATE_KEY or RPC URL for chain {current_chain}")
                        continue
                    log(f"  Executing bridge to {to_chain_name}...")
                    try:
                        tx_result = await lifi.execute_quote(
                            fresh_quote, private_key, rpc_url,
                            poll_status_client=client, api_key=LIFI_API_KEY,
                        )
                        log(f"  TX: {tx_result['tx_hash']} status={tx_result['status']}")
                        if tx_result["status"] == "FAILED":
                            log(f"  Bridge TX FAILED — not recording migration")
                            continue
                    except Exception as e:
                        log(f"  Bridge execution failed: {e}")
                        continue

                    # Verify received amount on destination chain
                    dest_rpc = lifi.RPC_URLS.get(target_chain_id)
                    if dest_rpc and tx_result["status"] == "DONE":
                        actual = await asyncio.to_thread(
                            wallet.check_onchain_balance,
                            target_chain_id, wallet_addr, dest_rpc,
                        )
                        if actual is not None:
                            expected_min = fresh_cost["to_amount_min"]
                            if actual < expected_min * 0.95:
                                log(f"  WARNING: received ${actual:.2f} < expected min ${expected_min:.2f}")
                            else:
                                log(f"  Verified: ${actual:.2f} received on {to_chain_name}")
                            # Use actual balance — more accurate than estimate
                            state["position_usd"] = round(actual, 2)
                            cost_usd = round(move_usd - actual, 2)
                        else:
                            # Can't verify — fall back to estimated cost
                            state["position_usd"] = round(state["position_usd"] - cost_usd, 2)
                    else:
                        state["position_usd"] = round(state["position_usd"] - cost_usd, 2)

                if DEMO_MODE:
                    # DEMO: simulate cost deduction
                    state["position_usd"] = round(state["position_usd"] - cost_usd, 2)
                wallet.record_migration(
                    state, current_chain, target_chain_id, target_pool,
                    cost_usd, move.get("reason", journal_text[:100]),
                )
                log(f"  Migration recorded: -> {to_chain_name} (cost: ${cost_usd:.2f}, position now: ${state['position_usd']:.2f})")

        # 9. Notify
        telegram_msg = (
            f"*Marco's Journal*\n"
            f"_{datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n"
            f"{journal_text}\n\n"
            f"Action: *{decision.get('action', 'hold').upper()}*"
        )
        await send_telegram(client, telegram_msg)


async def main():
    mode = "DEMO (simulated)" if DEMO_MODE else "LIVE (real execution)"
    log(f"Marco the Nomad waking up... Mode: {mode}")
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
