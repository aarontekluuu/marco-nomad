"""Compound v3 (Comet) adapter — supply/withdraw USDC.

Compound v3 uses a single Comet contract per market. USDC suppliers earn
yield directly — no separate receipt token. Balance is tracked via balanceOf
on the Comet contract itself.
"""

from __future__ import annotations

import asyncio

from protocols import PoolAdapter, DepositResult, WithdrawResult, register

# Comet USDC proxy addresses per chain
# Source: https://docs.compound.finance/#networks
COMET_ADDRESSES = {
    8453: "0xb125E6687d4313864e53df431d5425969c15Eb2F",   # Base cUSDCv3
    42161: "0xA5EDBDD9646f8dFF606d7448e414884C7d905dCA",  # Arbitrum cUSDCv3
    10: "0x2e44e174f7D53F0212823acC11C01A11d58c5bCB",     # Optimism cUSDCv3
    137: "0xF25212E676D1F7F89Cd72fFEe66158f541246445",    # Polygon cUSDCv3
}

USDC_DECIMALS = 6

COMET_ABI = [
    {
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "supply",
        "outputs": [],
        "type": "function",
    },
    {
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "withdraw",
        "outputs": [],
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]

MAX_UINT256 = 2**256 - 1


class CompoundV3Adapter(PoolAdapter):
    PROTOCOL_SLUG = "compound-v3"
    POOL_ADDRESSES = COMET_ADDRESSES
    RECEIPT_TOKENS = COMET_ADDRESSES  # Comet IS the receipt token

    async def deposit(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id):
        from web3 import Web3

        comet_addr = self.POOL_ADDRESSES[chain_id]
        sender = Web3.to_checksum_address(wallet_address)

        # Approve Comet to spend USDC
        await self._approve_if_needed(
            w3, token_address, comet_addr, amount_raw, wallet_address, private_key
        )

        comet = w3.eth.contract(
            address=Web3.to_checksum_address(comet_addr), abi=COMET_ABI
        )
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")
        tx = comet.functions.supply(
            Web3.to_checksum_address(token_address),
            amount_raw,
        ).build_transaction({
            "from": sender,
            "nonce": nonce,
            "chainId": chain_id,
        })

        tx_hash, _ = await self._send_tx(w3, tx, private_key)

        return DepositResult(
            tx_hash=tx_hash,
            amount_deposited=amount_raw / 10**USDC_DECIMALS,
            receipt_token=comet_addr,
            receipt_amount=amount_raw,
        )

    async def withdraw(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id):
        from web3 import Web3

        comet_addr = self.POOL_ADDRESSES[chain_id]
        sender = Web3.to_checksum_address(wallet_address)

        withdraw_amount = MAX_UINT256 if amount_raw is None else amount_raw

        comet = w3.eth.contract(
            address=Web3.to_checksum_address(comet_addr), abi=COMET_ABI
        )
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")
        tx = comet.functions.withdraw(
            Web3.to_checksum_address(token_address),
            withdraw_amount,
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
        from web3 import Web3

        comet_addr = self.POOL_ADDRESSES.get(chain_id)
        if not comet_addr:
            return 0.0

        comet = w3.eth.contract(
            address=Web3.to_checksum_address(comet_addr), abi=COMET_ABI
        )
        raw = await asyncio.to_thread(
            comet.functions.balanceOf(
                Web3.to_checksum_address(wallet_address)
            ).call
        )
        return raw / 10**USDC_DECIMALS

    def build_deposit_calldata(self, amount_raw, token_address, wallet_address, chain_id):
        from web3 import Web3

        comet_addr = self.POOL_ADDRESSES[chain_id]
        comet = Web3().eth.contract(
            address=Web3.to_checksum_address(comet_addr), abi=COMET_ABI
        )
        calldata = comet.functions.supply(
            Web3.to_checksum_address(token_address),
            amount_raw,
        )._encode_transaction_data()
        return comet_addr, calldata


register(CompoundV3Adapter())
