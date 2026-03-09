"""Moonwell adapter — mint/redeem mTokens (Compound v2 fork).

Moonwell uses a Compound v2-style interface: mint() deposits underlying,
redeem() withdraws by burning mTokens. Exchange rate grows over time.
"""

from __future__ import annotations

import asyncio

from protocols import PoolAdapter, DepositResult, WithdrawResult, register

# Moonwell mUSDC (Comptroller-based) addresses
# Source: https://docs.moonwell.fi/
MTOKEN_ADDRESSES = {
    8453: "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",   # Base mUSDC
    10: "0x8E08617b0d66359D73bB46E7269b3a3e13FbEBF5",       # Optimism mUSDC
}

USDC_DECIMALS = 6

MTOKEN_ABI = [
    {
        "inputs": [{"name": "mintAmount", "type": "uint256"}],
        "name": "mint",
        "outputs": [{"name": "", "type": "uint256"}],  # 0 = success
        "type": "function",
    },
    {
        "inputs": [{"name": "redeemTokens", "type": "uint256"}],
        "name": "redeem",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "inputs": [{"name": "redeemAmount", "type": "uint256"}],
        "name": "redeemUnderlying",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOfUnderlying",
        "outputs": [{"name": "", "type": "uint256"}],
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


class MoonwellAdapter(PoolAdapter):
    PROTOCOL_SLUG = "moonwell"
    POOL_ADDRESSES = MTOKEN_ADDRESSES
    RECEIPT_TOKENS = MTOKEN_ADDRESSES  # mToken IS the receipt

    async def deposit(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id):
        from web3 import Web3

        mtoken_addr = self.POOL_ADDRESSES[chain_id]
        sender = Web3.to_checksum_address(wallet_address)

        # Approve mToken to spend USDC
        await self._approve_if_needed(
            w3, token_address, mtoken_addr, amount_raw, wallet_address, private_key
        )

        mtoken = w3.eth.contract(
            address=Web3.to_checksum_address(mtoken_addr), abi=MTOKEN_ABI
        )
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")
        tx = mtoken.functions.mint(amount_raw).build_transaction({
            "from": sender,
            "nonce": nonce,
            "chainId": chain_id,
        })

        tx_hash, _ = await self._send_tx(w3, tx, private_key)

        # Get mToken balance received
        m_balance = await asyncio.to_thread(
            mtoken.functions.balanceOf(sender).call
        )

        return DepositResult(
            tx_hash=tx_hash,
            amount_deposited=amount_raw / 10**USDC_DECIMALS,
            receipt_token=mtoken_addr,
            receipt_amount=m_balance,
        )

    async def withdraw(self, w3, amount_raw, token_address, wallet_address, private_key, chain_id):
        from web3 import Web3

        mtoken_addr = self.POOL_ADDRESSES[chain_id]
        sender = Web3.to_checksum_address(wallet_address)
        mtoken = w3.eth.contract(
            address=Web3.to_checksum_address(mtoken_addr), abi=MTOKEN_ABI
        )

        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")

        if amount_raw is None:
            # Redeem all mTokens
            m_balance = await asyncio.to_thread(
                mtoken.functions.balanceOf(sender).call
            )
            tx = mtoken.functions.redeem(m_balance).build_transaction({
                "from": sender,
                "nonce": nonce,
                "chainId": chain_id,
            })
        else:
            # Redeem specific underlying amount
            tx = mtoken.functions.redeemUnderlying(amount_raw).build_transaction({
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

        mtoken_addr = self.POOL_ADDRESSES.get(chain_id)
        if not mtoken_addr:
            return 0.0

        mtoken = w3.eth.contract(
            address=Web3.to_checksum_address(mtoken_addr), abi=MTOKEN_ABI
        )
        # balanceOfUnderlying returns USDC value including yield
        raw = await asyncio.to_thread(
            mtoken.functions.balanceOfUnderlying(
                Web3.to_checksum_address(wallet_address)
            ).call
        )
        return raw / 10**USDC_DECIMALS

    def build_deposit_calldata(self, amount_raw, token_address, wallet_address, chain_id):
        from web3 import Web3

        mtoken_addr = self.POOL_ADDRESSES[chain_id]
        mtoken = Web3().eth.contract(
            address=Web3.to_checksum_address(mtoken_addr), abi=MTOKEN_ABI
        )
        calldata = mtoken.functions.mint(amount_raw)._encode_transaction_data()
        return mtoken_addr, calldata


register(MoonwellAdapter())
