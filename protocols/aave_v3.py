"""Aave v3 pool adapter — supply/withdraw USDC into Aave lending pools.

aTokens are rebasing — balanceOf(wallet) auto-increases as yield accrues.
No need to track shares separately.
"""

from __future__ import annotations

import asyncio

from protocols import PoolAdapter, DepositResult, WithdrawResult, register

# Aave v3 Pool proxy addresses per chain
# Source: https://docs.aave.com/developers/deployed-contracts
POOL_ADDRESSES = {
    8453: "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",   # Base
    42161: "0x794a61358D6845594F94dc1DB02A252b5b4814aD",  # Arbitrum
    10: "0x794a61358D6845594F94dc1DB02A252b5b4814aD",     # Optimism
    137: "0x794a61358D6845594F94dc1DB02A252b5b4814aD",    # Polygon
}

# aUSDC token addresses per chain (for balance checks)
AUSDC_ADDRESSES = {
    8453: "0x4e65fE4DbA92790696d040ac24Aa414708F5c0AB",   # Base aUSDC
    42161: "0x625E7708f30cA75bfd92586e17077590C60eb4cD",  # Arbitrum aUSDC
    10: "0x625E7708f30cA75bfd92586e17077590C60eb4cD",     # Optimism aUSDC
    137: "0x625E7708f30cA75bfd92586e17077590C60eb4cD",    # Polygon aUSDC
}

USDC_DECIMALS = 6

# Aave v3 Pool ABI (only the functions we need)
POOL_ABI = [
    {
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
            {"name": "referralCode", "type": "uint16"},
        ],
        "name": "supply",
        "outputs": [],
        "type": "function",
    },
    {
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "to", "type": "address"},
        ],
        "name": "withdraw",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]

BALANCE_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]

MAX_UINT256 = 2**256 - 1


class AaveV3Adapter(PoolAdapter):
    PROTOCOL_SLUG = "aave-v3"
    POOL_ADDRESSES = POOL_ADDRESSES
    RECEIPT_TOKENS = AUSDC_ADDRESSES

    async def deposit(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id):
        from web3 import Web3

        pool_addr = self.POOL_ADDRESSES[chain_id]
        sender = Web3.to_checksum_address(wallet_address)

        # Approve pool to spend USDC
        await self._approve_if_needed(
            w3, token_address, pool_addr, amount_raw, wallet_address, private_key
        )

        # Build supply TX
        pool = w3.eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI
        )
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")
        tx = pool.functions.supply(
            Web3.to_checksum_address(token_address),
            amount_raw,
            sender,
            0,  # referralCode
        ).build_transaction({
            "from": sender,
            "nonce": nonce,
            "chainId": chain_id,
        })

        tx_hash, _ = await self._send_tx(w3, tx, private_key)

        return DepositResult(
            tx_hash=tx_hash,
            amount_deposited=amount_raw / 10**USDC_DECIMALS,
            receipt_token=AUSDC_ADDRESSES.get(chain_id, ""),
            receipt_amount=amount_raw,  # 1:1 for aTokens
        )

    async def withdraw(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id):
        from web3 import Web3

        pool_addr = self.POOL_ADDRESSES[chain_id]
        sender = Web3.to_checksum_address(wallet_address)

        # amount_raw=None means withdraw all
        withdraw_amount = MAX_UINT256 if amount_raw is None else amount_raw

        pool = w3.eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI
        )
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")
        tx = pool.functions.withdraw(
            Web3.to_checksum_address(token_address),
            withdraw_amount,
            sender,
        ).build_transaction({
            "from": sender,
            "nonce": nonce,
            "chainId": chain_id,
        })

        tx_hash, _ = await self._send_tx(w3, tx, private_key)

        # Check actual balance received
        actual = await self.balance(w3, wallet_address, chain_id)
        # After full withdraw, aToken balance should be ~0
        # The USDC we got back is position_usd - we'll read it from wallet
        return WithdrawResult(
            tx_hash=tx_hash,
            amount_withdrawn=amount_raw / 10**USDC_DECIMALS if amount_raw else 0,
            token_received=token_address,
        )

    async def balance(self, w3, wallet_address, chain_id):
        from web3 import Web3

        ausdc_addr = AUSDC_ADDRESSES.get(chain_id)
        if not ausdc_addr:
            return 0.0

        contract = w3.eth.contract(
            address=Web3.to_checksum_address(ausdc_addr), abi=BALANCE_ABI
        )
        raw = await asyncio.to_thread(
            contract.functions.balanceOf(
                Web3.to_checksum_address(wallet_address)
            ).call
        )
        return raw / 10**USDC_DECIMALS

    def build_deposit_calldata(self, amount_raw, token_address, wallet_address, chain_id):
        from web3 import Web3

        pool_addr = self.POOL_ADDRESSES[chain_id]
        pool = Web3().eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI
        )
        calldata = pool.functions.supply(
            Web3.to_checksum_address(token_address),
            amount_raw,
            Web3.to_checksum_address(wallet_address),
            0,
        )._encode_transaction_data()
        return pool_addr, calldata


# Register
register(AaveV3Adapter())
