"""Stargate v2 adapter — deposit/redeem into Stargate USDC pools.

Stargate v2 pools are OFT-based (LayerZero), NOT ERC-4626.
deposit(address to, uint256 amountLD) and redeem(uint256 amountLD, address receiver).
The pool contract itself is the LP token.
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
}

USDC_DECIMALS = 6

# Stargate v2 Pool ABI (NOT ERC-4626)
POOL_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amountLD", "type": "uint256"},
        ],
        "name": "deposit",
        "outputs": [{"name": "amountLD", "type": "uint256"}],
        "type": "function",
        "stateMutability": "payable",
    },
    {
        "inputs": [
            {"name": "amountLD", "type": "uint256"},
            {"name": "receiver", "type": "address"},
        ],
        "name": "redeem",
        "outputs": [{"name": "amountLD", "type": "uint256"}],
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


class StargateAdapter(PoolAdapter):
    PROTOCOL_SLUG = "stargate"
    POOL_ADDRESSES = POOL_ADDRESSES
    RECEIPT_TOKENS = POOL_ADDRESSES  # Pool IS the LP token

    async def deposit(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id):
        from web3 import Web3

        pool_addr = self.POOL_ADDRESSES[chain_id]
        sender = Web3.to_checksum_address(wallet_address)

        # Approve pool to spend USDC
        await self._approve_if_needed(
            w3, token_address, pool_addr, amount_raw, wallet_address, private_key
        )

        pool = w3.eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI
        )
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")
        tx = pool.functions.deposit(sender, amount_raw).build_transaction({
            "from": sender,
            "nonce": nonce,
            "chainId": chain_id,
            "value": 0,
        })

        tx_hash, _ = await self._send_tx(w3, tx, private_key)

        # Get LP tokens received
        lp_balance = await asyncio.to_thread(
            pool.functions.balanceOf(sender).call
        )

        return DepositResult(
            tx_hash=tx_hash,
            amount_deposited=amount_raw / 10**USDC_DECIMALS,
            receipt_token=pool_addr,
            receipt_amount=lp_balance,
        )

    async def withdraw(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id):
        from web3 import Web3

        pool_addr = self.POOL_ADDRESSES[chain_id]
        sender = Web3.to_checksum_address(wallet_address)

        pool = w3.eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI
        )
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")

        if amount_raw is None:
            # Redeem all LP tokens
            lp_balance = await asyncio.to_thread(
                pool.functions.balanceOf(sender).call
            )
            redeem_amount = lp_balance
        else:
            redeem_amount = amount_raw

        tx = pool.functions.redeem(redeem_amount, sender).build_transaction({
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

        pool = w3.eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI
        )
        # LP token balance ~ 1:1 with USDC deposited (Stargate pools maintain peg)
        lp_raw = await asyncio.to_thread(
            pool.functions.balanceOf(
                Web3.to_checksum_address(wallet_address)
            ).call
        )
        return lp_raw / 10**USDC_DECIMALS

    def build_deposit_calldata(self, amount_raw, token_address, wallet_address, chain_id):
        from web3 import Web3

        pool_addr = self.POOL_ADDRESSES[chain_id]
        pool = Web3().eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI
        )
        calldata = pool.functions.deposit(
            Web3.to_checksum_address(wallet_address),
            amount_raw,
        )._encode_transaction_data()
        return pool_addr, calldata


register(StargateAdapter())
