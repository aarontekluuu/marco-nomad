"""LI.FI API module - cross-chain quotes, cost calc, route execution."""

import httpx

BASE = "https://li.quest/v1"
NATIVE = "0x0000000000000000000000000000000000000000"


def _headers(api_key: str | None = None) -> dict:
    h = {}
    if api_key:
        h["x-lifi-api-key"] = api_key
    return h


async def get_chains(client: httpx.AsyncClient, api_key: str | None = None) -> list[dict]:
    """List all supported chains."""
    resp = await client.get(f"{BASE}/chains", headers=_headers(api_key), timeout=15)
    resp.raise_for_status()
    return resp.json()["chains"]


async def get_tokens(
    client: httpx.AsyncClient,
    chain_ids: list[int] | None = None,
    api_key: str | None = None,
) -> dict:
    """List supported tokens, optionally filtered by chain."""
    params = {}
    if chain_ids:
        params["chains"] = ",".join(str(c) for c in chain_ids)
    resp = await client.get(f"{BASE}/tokens", headers=_headers(api_key), params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()["tokens"]


async def get_quote(
    client: httpx.AsyncClient,
    from_chain: int,
    to_chain: int,
    from_token: str,
    to_token: str,
    from_amount: str,
    from_address: str,
    slippage: float = 0.03,
    api_key: str | None = None,
) -> dict:
    """Get a cross-chain swap/bridge quote."""
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


async def get_routes(
    client: httpx.AsyncClient,
    from_chain: int,
    to_chain: int,
    from_token: str,
    to_token: str,
    from_amount: str,
    from_address: str,
    slippage: float = 0.03,
    api_key: str | None = None,
) -> list[dict]:
    """Get multiple route options via advanced/routes."""
    body = {
        "fromChainId": from_chain,
        "toChainId": to_chain,
        "fromTokenAddress": from_token,
        "toTokenAddress": to_token,
        "fromAmount": from_amount,
        "fromAddress": from_address,
        "options": {"slippage": slippage, "order": "RECOMMENDED"},
    }
    resp = await client.post(
        f"{BASE}/advanced/routes", headers=_headers(api_key), json=body, timeout=30
    )
    resp.raise_for_status()
    return resp.json().get("routes", [])


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

    return {
        "fee_usd": total_fee_usd,
        "gas_usd": total_gas_usd,
        "total_cost_usd": total_cost_usd,
        "from_amount": from_human,
        "to_amount": to_human,
        "to_amount_min": to_min_human,
        "slippage_amount": to_human - to_min_human,
        "duration_seconds": estimate.get("executionDuration", 0),
        "bridge": quote.get("tool", "unknown"),
    }


async def execute_quote(quote: dict, private_key: str, rpc_url: str) -> str:
    """Sign and send a quote's transactionRequest on-chain.

    Returns the transaction hash.
    """
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    tx = quote["transactionRequest"]
    sender = Web3.to_checksum_address(tx["from"])

    # ERC20 approval if needed
    approval_addr = quote["estimate"].get("approvalAddress")
    from_token = quote["action"]["fromToken"]["address"]
    if approval_addr and from_token != NATIVE:
        erc20 = w3.eth.contract(
            address=Web3.to_checksum_address(from_token),
            abi=[{
                "inputs": [
                    {"name": "spender", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                ],
                "name": "approve",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function",
            }],
        )
        approve_tx = erc20.functions.approve(
            Web3.to_checksum_address(approval_addr),
            int(quote["action"]["fromAmount"]),
        ).build_transaction({
            "from": sender,
            "nonce": w3.eth.get_transaction_count(sender),
        })
        signed = w3.eth.account.sign_transaction(approve_tx, private_key)
        w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(signed.hash)

    # Send the bridge/swap tx
    send_tx = {
        "from": sender,
        "to": Web3.to_checksum_address(tx["to"]),
        "data": tx["data"],
        "value": int(tx["value"], 16),
        "gas": int(tx["gasLimit"], 16),
        "nonce": w3.eth.get_transaction_count(sender),
    }
    if "gasPrice" in tx:
        send_tx["gasPrice"] = int(tx["gasPrice"], 16)

    signed = w3.eth.account.sign_transaction(send_tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


RPC_URLS = {
    1: "https://eth.llamarpc.com",
    8453: "https://mainnet.base.org",
    42161: "https://arb1.arbitrum.io/rpc",
    10: "https://mainnet.optimism.io",
    137: "https://polygon-rpc.com",
    56: "https://bsc-dataseed.binance.org",
    43114: "https://api.avax.network/ext/bc/C/rpc",
}


def format_quote(quote: dict) -> str:
    """Format a quote for display."""
    cost = calc_bridge_cost(quote)
    action = quote.get("action", {})
    from_sym = action.get("fromToken", {}).get("symbol", "?")
    to_sym = action.get("toToken", {}).get("symbol", "?")
    return (
        f"{from_sym} -> {to_sym} via {cost['bridge']}\n"
        f"  Send: {cost['from_amount']:.4f} | Receive: {cost['to_amount']:.4f} (min: {cost['to_amount_min']:.4f})\n"
        f"  Cost: ${cost['total_cost_usd']:.2f} (fees: ${cost['fee_usd']:.2f} + gas: ${cost['gas_usd']:.2f})\n"
        f"  Duration: ~{cost['duration_seconds']}s"
    )
