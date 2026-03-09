"""Seamless Protocol adapter — Aave v3 fork on Base.

Same ABI as Aave v3, different Pool address.
"""

from __future__ import annotations

from protocols.aave_v3 import AaveV3Adapter, POOL_ABI, BALANCE_ABI, USDC_DECIMALS
from protocols import DepositResult, WithdrawResult, register


class SeamlessAdapter(AaveV3Adapter):
    PROTOCOL_SLUG = "seamless-protocol"

    # Seamless Protocol Pool proxy on Base
    POOL_ADDRESSES = {
        8453: "0x8F44Fd754285aa6A2b8B9B97739B79746e0475a7",
    }

    # Seamless aUSDC on Base
    RECEIPT_TOKENS = {
        8453: "0x53E240C0F985175dA046A62F26D490d1E259036e",
    }


register(SeamlessAdapter())
