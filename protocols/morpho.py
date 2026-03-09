"""Morpho Blue adapter — supply/withdraw into Morpho lending markets.

Morpho Blue uses a singleton contract (same address on all chains via CREATE2).
Deposits are identified by a MarketParams struct, not a separate contract per pool.
"""

from __future__ import annotations

import asyncio

from protocols import PoolAdapter, DepositResult, WithdrawResult, register

# Morpho Blue singleton — same address on all EVM chains (CREATE2)
MORPHO_BLUE = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

# Chains where Morpho Blue is deployed
SUPPORTED_CHAINS = {8453, 42161, 10, 137, 1}

USDC_DECIMALS = 6

# Morpho Blue ABI (supply/withdraw use MarketParams tuple)
MORPHO_ABI = [
    {
        "inputs": [
            {
                "name": "marketParams",
                "type": "tuple",
                "components": [
                    {"name": "loanToken", "type": "address"},
                    {"name": "collateralToken", "type": "address"},
                    {"name": "oracle", "type": "address"},
                    {"name": "irm", "type": "address"},
                    {"name": "lltv", "type": "uint256"},
                ],
            },
            {"name": "assets", "type": "uint256"},
            {"name": "shares", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
            {"name": "data", "type": "bytes"},
        ],
        "name": "supply",
        "outputs": [
            {"name": "assetsSupplied", "type": "uint256"},
            {"name": "sharesSupplied", "type": "uint256"},
        ],
        "type": "function",
    },
    {
        "inputs": [
            {
                "name": "marketParams",
                "type": "tuple",
                "components": [
                    {"name": "loanToken", "type": "address"},
                    {"name": "collateralToken", "type": "address"},
                    {"name": "oracle", "type": "address"},
                    {"name": "irm", "type": "address"},
                    {"name": "lltv", "type": "uint256"},
                ],
            },
            {"name": "assets", "type": "uint256"},
            {"name": "shares", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
            {"name": "receiver", "type": "address"},
        ],
        "name": "withdraw",
        "outputs": [
            {"name": "assetsWithdrawn", "type": "uint256"},
            {"name": "sharesWithdrawn", "type": "uint256"},
        ],
        "type": "function",
    },
    {
        "inputs": [
            {"name": "id", "type": "bytes32"},
            {"name": "user", "type": "address"},
        ],
        "name": "position",
        "outputs": [
            {"name": "supplyShares", "type": "uint256"},
            {"name": "borrowShares", "type": "uint256"},
            {"name": "collateral", "type": "uint256"},
        ],
        "type": "function",
    },
]

# Default empty market params — must be set per-pool at runtime
EMPTY_MARKET = (
    "0x0000000000000000000000000000000000000000",  # loanToken
    "0x0000000000000000000000000000000000000000",  # collateralToken
    "0x0000000000000000000000000000000000000000",  # oracle
    "0x0000000000000000000000000000000000000000",  # irm
    0,  # lltv
)

MAX_UINT256 = 2**256 - 1


class MorphoAdapter(PoolAdapter):
    PROTOCOL_SLUG = "morpho-blue"
    POOL_ADDRESSES = {chain_id: MORPHO_BLUE for chain_id in SUPPORTED_CHAINS}
    RECEIPT_TOKENS = {}  # Morpho uses shares, not separate tokens

    def _get_market_params(self, chain_id: int) -> tuple:
        """Get market params for the active Morpho USDC market.

        In production, this should be looked up from the pool's DefiLlama metadata
        or stored in wallet_state when a pool is selected.
        """
        # Market params are pool-specific — the brain selects a pool and we need
        # to resolve its market params. For now, return empty and require the
        # caller to pass market_params via pool metadata.
        return EMPTY_MARKET

    async def deposit(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id,
                      market_params=None):
        from web3 import Web3

        sender = Web3.to_checksum_address(wallet_address)
        morpho_addr = Web3.to_checksum_address(MORPHO_BLUE)

        if not market_params:
            raise ValueError("Morpho deposit requires market_params tuple")

        # Approve Morpho to spend USDC
        await self._approve_if_needed(
            w3, token_address, MORPHO_BLUE, amount_raw, wallet_address, private_key
        )

        morpho = w3.eth.contract(address=morpho_addr, abi=MORPHO_ABI)
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")
        tx = morpho.functions.supply(
            market_params,
            amount_raw,  # assets
            0,  # shares (0 = use assets)
            sender,  # onBehalfOf
            b"",  # data (no callback)
        ).build_transaction({
            "from": sender,
            "nonce": nonce,
            "chainId": chain_id,
        })

        tx_hash, _ = await self._send_tx(w3, tx, private_key)

        return DepositResult(
            tx_hash=tx_hash,
            amount_deposited=amount_raw / 10**USDC_DECIMALS,
            receipt_token=MORPHO_BLUE,
            receipt_amount=0,  # Shares — would need to parse TX receipt
        )

    async def withdraw(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id,
                       market_params=None):
        from web3 import Web3

        sender = Web3.to_checksum_address(wallet_address)
        morpho_addr = Web3.to_checksum_address(MORPHO_BLUE)

        if not market_params:
            raise ValueError("Morpho withdraw requires market_params tuple")

        morpho = w3.eth.contract(address=morpho_addr, abi=MORPHO_ABI)
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")

        if amount_raw is None:
            # Withdraw all — use shares instead of assets
            # Need to look up current shares first
            tx = morpho.functions.withdraw(
                market_params,
                0,  # assets (0 = use shares)
                MAX_UINT256,  # shares (max = all)
                sender,
                sender,
            ).build_transaction({
                "from": sender,
                "nonce": nonce,
                "chainId": chain_id,
            })
        else:
            tx = morpho.functions.withdraw(
                market_params,
                amount_raw,
                0,  # shares (0 = use assets)
                sender,
                sender,
            ).build_transaction({
                "from": sender,
                "nonce": nonce,
                "chainId": chain_id,
            })

        tx_hash, _ = await self._send_tx(w3, tx, private_key)

        return WithdrawResult(
            tx_hash=tx_hash,
            amount_withdrawn=amount_raw / 10**USDC_DECIMALS if amount_raw else 0,
            token_received=token_address,
        )

    async def balance(self, w3, wallet_address, chain_id):
        # Morpho balance requires knowing the market ID — without it we can't query
        # For now return 0; full implementation needs market_id stored in wallet state
        return 0.0

    def build_deposit_calldata(self, amount_raw, token_address, wallet_address, chain_id,
                               market_params=None):
        from web3 import Web3

        if not market_params:
            raise ValueError("Morpho deposit calldata requires market_params")

        morpho = Web3().eth.contract(
            address=Web3.to_checksum_address(MORPHO_BLUE), abi=MORPHO_ABI
        )
        calldata = morpho.functions.supply(
            market_params,
            amount_raw,
            0,
            Web3.to_checksum_address(wallet_address),
            b"",
        )._encode_transaction_data()
        return MORPHO_BLUE, calldata


register(MorphoAdapter())
