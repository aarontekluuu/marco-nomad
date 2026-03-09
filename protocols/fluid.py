"""Fluid (Instadapp) adapter — deposit/withdraw into Fluid lending vaults.

Fluid vaults follow ERC-4626 standard. Each vault is a separate contract
for a specific lending market.
"""

from __future__ import annotations

import asyncio

from protocols import PoolAdapter, DepositResult, WithdrawResult, register

# Fluid USDC vault addresses per chain
# Source: https://docs.instadapp.io/
VAULT_ADDRESSES = {
    8453: "0x9272D6153133175175Bc276512B2336BE3931CE9",   # Base fUSDC
    42161: "0x1A996cb54bb95462040408C06122D45D6Cdb6096",  # Arbitrum fUSDC
}

USDC_DECIMALS = 6

# ERC-4626 vault ABI (same as Stargate)
VAULT_ABI = [
    {
        "inputs": [
            {"name": "assets", "type": "uint256"},
            {"name": "receiver", "type": "address"},
        ],
        "name": "deposit",
        "outputs": [{"name": "shares", "type": "uint256"}],
        "type": "function",
    },
    {
        "inputs": [
            {"name": "shares", "type": "uint256"},
            {"name": "receiver", "type": "address"},
            {"name": "owner", "type": "address"},
        ],
        "name": "redeem",
        "outputs": [{"name": "assets", "type": "uint256"}],
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "inputs": [{"name": "shares", "type": "uint256"}],
        "name": "convertToAssets",
        "outputs": [{"name": "assets", "type": "uint256"}],
        "type": "function",
    },
    {
        "inputs": [{"name": "assets", "type": "uint256"}],
        "name": "convertToShares",
        "outputs": [{"name": "shares", "type": "uint256"}],
        "type": "function",
    },
]


class FluidAdapter(PoolAdapter):
    PROTOCOL_SLUG = "fluid"
    POOL_ADDRESSES = VAULT_ADDRESSES
    RECEIPT_TOKENS = VAULT_ADDRESSES

    async def deposit(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id):
        from web3 import Web3

        vault_addr = self.POOL_ADDRESSES[chain_id]
        sender = Web3.to_checksum_address(wallet_address)

        await self._approve_if_needed(
            w3, token_address, vault_addr, amount_raw, wallet_address, private_key
        )

        vault = w3.eth.contract(
            address=Web3.to_checksum_address(vault_addr), abi=VAULT_ABI
        )
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")
        tx = vault.functions.deposit(amount_raw, sender).build_transaction({
            "from": sender,
            "nonce": nonce,
            "chainId": chain_id,
        })

        tx_hash, _ = await self._send_tx(w3, tx, private_key)

        shares = await asyncio.to_thread(
            vault.functions.balanceOf(sender).call
        )

        return DepositResult(
            tx_hash=tx_hash,
            amount_deposited=amount_raw / 10**USDC_DECIMALS,
            receipt_token=vault_addr,
            receipt_amount=shares,
        )

    async def withdraw(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id):
        from web3 import Web3

        vault_addr = self.POOL_ADDRESSES[chain_id]
        sender = Web3.to_checksum_address(wallet_address)

        vault = w3.eth.contract(
            address=Web3.to_checksum_address(vault_addr), abi=VAULT_ABI
        )
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")

        if amount_raw is None:
            shares = await asyncio.to_thread(
                vault.functions.balanceOf(sender).call
            )
            tx = vault.functions.redeem(shares, sender, sender).build_transaction({
                "from": sender,
                "nonce": nonce,
                "chainId": chain_id,
            })
        else:
            shares = await asyncio.to_thread(
                vault.functions.convertToShares(amount_raw).call
            )
            tx = vault.functions.redeem(shares, sender, sender).build_transaction({
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

        vault_addr = self.POOL_ADDRESSES.get(chain_id)
        if not vault_addr:
            return 0.0

        vault = w3.eth.contract(
            address=Web3.to_checksum_address(vault_addr), abi=VAULT_ABI
        )
        shares = await asyncio.to_thread(
            vault.functions.balanceOf(
                Web3.to_checksum_address(wallet_address)
            ).call
        )
        if shares == 0:
            return 0.0
        assets = await asyncio.to_thread(
            vault.functions.convertToAssets(shares).call
        )
        return assets / 10**USDC_DECIMALS

    def build_deposit_calldata(self, amount_raw, token_address, wallet_address, chain_id):
        from web3 import Web3

        vault_addr = self.POOL_ADDRESSES[chain_id]
        vault = Web3().eth.contract(
            address=Web3.to_checksum_address(vault_addr), abi=VAULT_ABI
        )
        calldata = vault.functions.deposit(
            amount_raw,
            Web3.to_checksum_address(wallet_address),
        )._encode_transaction_data()
        return vault_addr, calldata


register(FluidAdapter())
