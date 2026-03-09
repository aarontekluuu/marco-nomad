"""Stargate v2 adapter — deposit/redeem into Stargate USDC pools.

Stargate v2 pools are ERC-4626 vaults. deposit() and redeem() follow
the standard vault interface.
"""

from __future__ import annotations

import asyncio

from protocols import PoolAdapter, DepositResult, WithdrawResult, register

# Stargate v2 USDC Pool addresses
# Source: https://stargateprotocol.gitbook.io/stargate/v2
POOL_ADDRESSES = {
    8453: "0x27a16dc786820B16E5c9028b75B99F6f604b5d26",   # Base
    42161: "0xe8CDF27AcD73a434D661C84887215F7598e7d0d3",  # Arbitrum
    10: "0xcE8CcA271Ebc0533920C83d39F417ED6A0abB7D0",     # Optimism
    137: "0x1205f31718499dBf1fCa446663B532Ef87481FE1",    # Polygon
}

USDC_DECIMALS = 6

# ERC-4626 vault ABI subset
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

MAX_UINT256 = 2**256 - 1


class StargateAdapter(PoolAdapter):
    PROTOCOL_SLUG = "stargate"
    POOL_ADDRESSES = POOL_ADDRESSES
    RECEIPT_TOKENS = POOL_ADDRESSES  # Vault shares are the receipt

    async def deposit(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id):
        from web3 import Web3

        pool_addr = self.POOL_ADDRESSES[chain_id]
        sender = Web3.to_checksum_address(wallet_address)

        await self._approve_if_needed(
            w3, token_address, pool_addr, amount_raw, wallet_address, private_key
        )

        vault = w3.eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=VAULT_ABI
        )
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")
        tx = vault.functions.deposit(amount_raw, sender).build_transaction({
            "from": sender,
            "nonce": nonce,
            "chainId": chain_id,
        })

        tx_hash, _ = await self._send_tx(w3, tx, private_key)

        # Get shares received
        shares = await asyncio.to_thread(
            vault.functions.balanceOf(sender).call
        )

        return DepositResult(
            tx_hash=tx_hash,
            amount_deposited=amount_raw / 10**USDC_DECIMALS,
            receipt_token=pool_addr,
            receipt_amount=shares,
        )

    async def withdraw(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id):
        from web3 import Web3

        pool_addr = self.POOL_ADDRESSES[chain_id]
        sender = Web3.to_checksum_address(wallet_address)

        vault = w3.eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=VAULT_ABI
        )
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")

        if amount_raw is None:
            # Redeem all shares
            shares = await asyncio.to_thread(
                vault.functions.balanceOf(sender).call
            )
            tx = vault.functions.redeem(shares, sender, sender).build_transaction({
                "from": sender,
                "nonce": nonce,
                "chainId": chain_id,
            })
        else:
            # Convert desired USDC amount to shares
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

        pool_addr = self.POOL_ADDRESSES.get(chain_id)
        if not pool_addr:
            return 0.0

        vault = w3.eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=VAULT_ABI
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

        pool_addr = self.POOL_ADDRESSES[chain_id]
        vault = Web3().eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=VAULT_ABI
        )
        calldata = vault.functions.deposit(
            amount_raw,
            Web3.to_checksum_address(wallet_address),
        )._encode_transaction_data()
        return pool_addr, calldata


register(StargateAdapter())
