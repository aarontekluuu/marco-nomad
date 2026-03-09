"""LI.FI API module - cross-chain quotes, cost calc, route execution."""

import httpx

BASE = "https://li.quest/v1"
NATIVE = "0x0000000000000000000000000000000000000000"


def _headers(api_key: str | None = None) -> dict:
    h = {}
    if api_key:
        h["x-lifi-api-key"] = api_key
    return h


async def get_quote(
    client: httpx.AsyncClient,
    from_chain: int,
    to_chain: int,
    from_token: str,
    to_token: str,
    from_amount: str,
    from_address: str,
    slippage: float = 0.005,
    api_key: str | None = None,
) -> dict:
    """Get a cross-chain swap/bridge quote. Slippage default 0.5% (stablecoin-safe)."""
    params = {
        "fromChain": from_chain,
        "toChain": to_chain,
        "fromToken": from_token,
        "toToken": to_token,
        "fromAmount": from_amount,
        "fromAddress": from_address,
        "slippage": slippage,
        "order": "RECOMMENDED",
    }
    resp = await client.get(f"{BASE}/quote", headers=_headers(api_key), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


async def check_status(
    client: httpx.AsyncClient,
    tx_hash: str,
    from_chain: int | None = None,
    to_chain: int | None = None,
    bridge: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Check cross-chain transaction status."""
    params = {"txHash": tx_hash}
    if from_chain:
        params["fromChain"] = from_chain
    if to_chain:
        params["toChain"] = to_chain
    if bridge:
        params["bridge"] = bridge
    resp = await client.get(f"{BASE}/status", headers=_headers(api_key), params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def calc_bridge_cost(quote: dict) -> dict:
    """Calculate total bridge cost from a quote response."""
    estimate = quote.get("estimate", {})
    fee_costs = estimate.get("feeCosts", [])
    gas_costs = estimate.get("gasCosts", [])

    total_fee_usd = sum(float(f.get("amountUSD", 0)) for f in fee_costs)
    total_gas_usd = sum(float(g.get("amountUSD", 0)) for g in gas_costs)
    total_cost_usd = total_fee_usd + total_gas_usd

    from_amount = int(estimate.get("fromAmount", 0))
    to_amount = int(estimate.get("toAmount", 0))
    to_amount_min = int(estimate.get("toAmountMin", 0))

    from_decimals = quote.get("action", {}).get("fromToken", {}).get("decimals", 18)
    to_decimals = quote.get("action", {}).get("toToken", {}).get("decimals", 18)

    from_human = from_amount / (10 ** from_decimals) if from_amount else 0
    to_human = to_amount / (10 ** to_decimals) if to_amount else 0
    to_min_human = to_amount_min / (10 ** to_decimals) if to_amount_min else 0

    # True cost includes fees, gas, AND the spread (fromAmount - toAmount)
    # feeCosts/gasCosts only capture explicit charges, not bridge spread
    spread_loss = (from_human - to_human) if from_human and to_human else 0
    # Use the higher of (fees+gas) vs spread as the real cost —
    # spread already includes fees baked into the exchange rate
    effective_cost_usd = max(total_cost_usd, spread_loss) if spread_loss > 0 else total_cost_usd

    return {
        "fee_usd": total_fee_usd,
        "gas_usd": total_gas_usd,
        "spread_usd": max(spread_loss, 0),
        "total_cost_usd": effective_cost_usd,
        "from_amount": from_human,
        "to_amount": to_human,
        "to_amount_min": to_min_human,
        "slippage_amount": to_human - to_min_human,
        "duration_seconds": estimate.get("executionDuration", 0),
        "bridge": quote.get("tool", "unknown"),
    }


ERC20_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]

def _parse_int(val) -> int:
    """Parse an int from hex string (0x...) or decimal string."""
    if isinstance(val, int):
        return val
    s = str(val)
    if s.startswith("0x") or s.startswith("0X"):
        return int(s, 16)
    return int(s)


QUOTE_MAX_AGE_SECONDS = 60  # Re-fetch quote if older than this


async def execute_quote(
    quote: dict,
    private_key: str,
    rpc_url: str,
    poll_status_client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
    quote_fetched_at: float | None = None,
    refetch_fn=None,
) -> dict:
    """Sign and send a quote's transactionRequest on-chain.

    Returns {"tx_hash": str, "status": str, "receipt": dict | None}.

    Args:
        quote_fetched_at: Unix timestamp when quote was fetched. If stale, refetch.
        refetch_fn: async callable() -> dict that returns a fresh quote. Required if
                    quote_fetched_at is provided and quote might be stale.
    """
    import asyncio
    import time
    from web3 import Web3

    # Guard: re-fetch stale quotes to avoid executing with outdated routes/gas
    if quote_fetched_at and refetch_fn:
        age = time.time() - quote_fetched_at
        if age > QUOTE_MAX_AGE_SECONDS:
            quote = await refetch_fn()

    w3 = Web3(Web3.HTTPProvider(rpc_url))

    # Helper: run sync web3 calls in thread to avoid blocking the event loop
    async def _call(fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    tx = quote["transactionRequest"]
    sender = Web3.to_checksum_address(tx["from"])
    from_amount = int(quote["action"]["fromAmount"])

    # SECURITY: Validate TX destination is a known LI.FI contract
    tx_to = Web3.to_checksum_address(tx["to"])
    chain_id = tx.get("chainId", await _call(lambda: w3.eth.chain_id))
    known_diamond = LIFI_DIAMOND.get(chain_id)
    if known_diamond and tx_to.lower() != known_diamond.lower():
        raise ValueError(
            f"TX destination {tx_to} does not match known LI.FI diamond "
            f"{known_diamond} on chain {chain_id}. Refusing to sign — possible API compromise."
        )

    # 1. Check existing allowance before approving (saves gas on repeat bridges)
    approval_addr = quote["estimate"].get("approvalAddress")
    from_token = quote["action"]["fromToken"]["address"]
    if approval_addr and from_token != NATIVE:
        erc20 = w3.eth.contract(
            address=Web3.to_checksum_address(from_token),
            abi=ERC20_ABI,
        )
        current_allowance = await _call(
            erc20.functions.allowance(
                sender, Web3.to_checksum_address(approval_addr)
            ).call
        )

        if current_allowance < from_amount:
            # Approve exact amount — infinite approval is a security risk if router is compromised
            nonce = await _call(w3.eth.get_transaction_count, sender, "pending")
            approve_build = {"from": sender, "nonce": nonce}
            # Use EIP-1559 for approval TX too (same MEV protection as bridge TX)
            try:
                latest = await _call(w3.eth.get_block, "latest")
                if hasattr(latest, "baseFeePerGas") and latest.baseFeePerGas:
                    approve_build["maxFeePerGas"] = latest.baseFeePerGas * 2
                    approve_build["maxPriorityFeePerGas"] = w3.to_wei(0.1, "gwei")
            except Exception:
                pass
            approve_tx = erc20.functions.approve(
                Web3.to_checksum_address(approval_addr),
                from_amount,
            ).build_transaction(approve_build)
            signed = w3.eth.account.sign_transaction(approve_tx, private_key)
            await _call(w3.eth.send_raw_transaction, signed.raw_transaction)
            approve_receipt = await _call(
                w3.eth.wait_for_transaction_receipt, signed.hash, timeout=120
            )
            if approve_receipt.status != 1:
                raise RuntimeError(
                    f"ERC20 approval TX reverted (hash: {signed.hash.hex()}). "
                    f"Cannot proceed with bridge — token may block re-approval without resetting to 0."
                )

    # 2. Build TX with EIP-1559 gas if available, fallback to legacy
    nonce = await _call(w3.eth.get_transaction_count, sender, "pending")
    send_tx = {
        "from": sender,
        "to": Web3.to_checksum_address(tx["to"]),
        "data": tx["data"],
        "value": _parse_int(tx.get("value", 0)),
        "gas": _parse_int(tx.get("gasLimit", tx.get("gas", 500000))),
        "nonce": nonce,
        "chainId": tx.get("chainId", await _call(lambda: w3.eth.chain_id)),
    }

    # Prefer EIP-1559 (reduces MEV exposure via base fee mechanism)
    try:
        latest = await _call(w3.eth.get_block, "latest")
        if hasattr(latest, "baseFeePerGas") and latest.baseFeePerGas:
            send_tx["maxFeePerGas"] = latest.baseFeePerGas * 2
            send_tx["maxPriorityFeePerGas"] = w3.to_wei(0.1, "gwei")
        elif "gasPrice" in tx:
            send_tx["gasPrice"] = _parse_int(tx["gasPrice"])
    except Exception:
        if "gasPrice" in tx:
            send_tx["gasPrice"] = _parse_int(tx["gasPrice"])

    # 3. Simulate TX before signing (catches reverts without spending gas)
    sim_tx = {k: v for k, v in send_tx.items() if k not in ("nonce", "chainId")}
    try:
        await _call(w3.eth.call, sim_tx, "latest")
    except Exception as e:
        raise RuntimeError(
            f"TX simulation reverted — would fail on-chain and waste gas. Error: {e}"
        )

    # 4. Sign and send
    signed = w3.eth.account.sign_transaction(send_tx, private_key)
    tx_hash = await _call(w3.eth.send_raw_transaction, signed.raw_transaction)
    tx_hash_hex = tx_hash.hex()

    # 5. Wait for on-chain confirmation
    receipt = await _call(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=300)
    if receipt.status != 1:
        return {"tx_hash": tx_hash_hex, "status": "FAILED", "receipt": dict(receipt)}

    # 6. Poll LI.FI bridge status with exponential backoff
    result = {"tx_hash": tx_hash_hex, "status": "PENDING", "receipt": dict(receipt)}

    if poll_status_client:
        bridge = quote.get("tool", "")
        from_chain = quote.get("action", {}).get("fromChainId")
        to_chain = quote.get("action", {}).get("toChainId")

        delay = 5  # Start at 5s, cap at 30s
        consecutive_errors = 0
        elapsed = 0
        max_poll_seconds = 600  # 10 min max for cross-chain bridges

        while elapsed < max_poll_seconds:
            await asyncio.sleep(delay)
            elapsed += delay
            try:
                status = await check_status(
                    poll_status_client, tx_hash_hex,
                    from_chain=from_chain, to_chain=to_chain,
                    bridge=bridge, api_key=api_key,
                )
                consecutive_errors = 0
                bridge_status = status.get("status", "PENDING")
                if bridge_status == "DONE":
                    result["status"] = "DONE"
                    break
                elif bridge_status == "FAILED":
                    result["status"] = "FAILED"
                    break
            except Exception:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    break  # Stop polling but don't mark failed — TX may still land
            delay = min(delay * 2, 30)  # Backoff: 5 -> 10 -> 20 -> 30s cap

    return result


# Known LI.FI router/diamond contract addresses per chain
# Validate TX destination against these to prevent signing malicious TXs
# Source: https://docs.li.fi/smart-contracts/deployments
# LI.FI uses CREATE2 deterministic deployment — same address on all EVM chains
_LIFI_DIAMOND_ADDR = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"
LIFI_DIAMOND = {chain_id: _LIFI_DIAMOND_ADDR for chain_id in [
    1, 8453, 42161, 10, 137, 56, 43114,  # Original 7
    250, 324, 59144, 534352,              # Fantom, zkSync, Linea, Scroll
]}

RPC_URLS = {
    1: "https://eth.llamarpc.com",
    8453: "https://mainnet.base.org",
    42161: "https://arb1.arbitrum.io/rpc",
    10: "https://mainnet.optimism.io",
    137: "https://polygon-rpc.com",
    56: "https://bsc-dataseed.binance.org",
    43114: "https://api.avax.network/ext/bc/C/rpc",
}


