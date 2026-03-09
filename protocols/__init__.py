"""Protocol adapter registry — maps DeFi protocol slugs to deposit/withdraw adapters."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from web3 import Web3


@dataclass
class DepositResult:
    tx_hash: str
    amount_deposited: float  # USD value deposited
    receipt_token: str  # Address of receipt token (aToken, cToken, shares, etc.)
    receipt_amount: int  # Raw amount of receipt tokens received


@dataclass
class WithdrawResult:
    tx_hash: str
    amount_withdrawn: float  # USD value withdrawn
    token_received: str  # Address of token received (USDC etc.)


@dataclass
class PoolPosition:
    protocol: str  # Protocol slug (e.g. "aave-v3")
    pool_address: str  # Pool/vault contract address
    receipt_token: str  # Receipt token address
    deposited_amount: float  # Original deposit in USD
    current_amount: float  # Current value including yield
    deposited_at: str  # ISO timestamp
    chain_id: int


class PoolAdapter(ABC):
    """Base class for DeFi protocol deposit/withdraw adapters.

    Each adapter handles one protocol family (e.g., Aave v3 across all chains).
    Adapters must be stateless — all state lives in wallet_state.json.
    """

    # Protocol slug matching DefiLlama's project field (e.g., "aave-v3")
    PROTOCOL_SLUG: str = ""

    # Chains this adapter supports — {chain_id: pool_contract_address}
    POOL_ADDRESSES: dict[int, str] = {}

    # Receipt token addresses per chain (aToken, cToken, etc.)
    RECEIPT_TOKENS: dict[int, str] = {}

    @abstractmethod
    async def deposit(
        self,
        w3: "Web3",
        amount_raw: int,
        token_address: str,
        wallet_address: str,
        private_key: str,
        chain_id: int,
    ) -> DepositResult:
        """Deposit tokens into the yield pool.

        Args:
            w3: Web3 instance connected to the right chain
            amount_raw: Raw token amount (e.g., USDC with 6 decimals)
            token_address: Address of token to deposit (USDC)
            wallet_address: Marco's wallet address
            private_key: For signing TX
            chain_id: Target chain

        Returns:
            DepositResult with tx_hash and receipt token info
        """

    @abstractmethod
    async def withdraw(
        self,
        w3: "Web3",
        amount_raw: int | None,
        token_address: str,
        wallet_address: str,
        private_key: str,
        chain_id: int,
    ) -> WithdrawResult:
        """Withdraw tokens from the yield pool.

        Args:
            amount_raw: Raw amount to withdraw, or None for full withdrawal
            (other args same as deposit)

        Returns:
            WithdrawResult with tx_hash and amount received
        """

    @abstractmethod
    async def balance(
        self,
        w3: "Web3",
        wallet_address: str,
        chain_id: int,
    ) -> float:
        """Check current deposited balance including accrued yield.

        Returns USD-equivalent amount (for stablecoins, this equals token amount).
        """

    @abstractmethod
    def build_deposit_calldata(
        self,
        amount_raw: int,
        token_address: str,
        wallet_address: str,
        chain_id: int,
    ) -> tuple[str, str]:
        """Build calldata for LI.FI atomic bridge+deposit.

        Returns:
            (contract_address, calldata_hex) for use with LI.FI contractCalls
        """

    def supports_chain(self, chain_id: int) -> bool:
        return chain_id in self.POOL_ADDRESSES

    async def _approve_if_needed(
        self,
        w3: "Web3",
        token_address: str,
        spender: str,
        amount: int,
        wallet_address: str,
        private_key: str,
    ) -> str | None:
        """Approve token spending if current allowance is insufficient.

        Returns tx_hash if approval was needed, None otherwise.
        """
        from web3 import Web3 as W3

        erc20_abi = [
            {
                "inputs": [
                    {"name": "owner", "type": "address"},
                    {"name": "spender", "type": "address"},
                ],
                "name": "allowance",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function",
            },
            {
                "inputs": [
                    {"name": "spender", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                ],
                "name": "approve",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function",
            },
        ]
        contract = w3.eth.contract(
            address=W3.to_checksum_address(token_address), abi=erc20_abi
        )
        sender = W3.to_checksum_address(wallet_address)
        spender_addr = W3.to_checksum_address(spender)

        current = await asyncio.to_thread(
            contract.functions.allowance(sender, spender_addr).call
        )
        if current >= amount:
            return None

        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")
        tx = contract.functions.approve(spender_addr, amount).build_transaction({
            "from": sender,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
        })

        # EIP-1559
        try:
            latest = await asyncio.to_thread(w3.eth.get_block, "latest")
            if hasattr(latest, "baseFeePerGas") and latest.baseFeePerGas:
                tx["maxFeePerGas"] = latest.baseFeePerGas * 2
                tx["maxPriorityFeePerGas"] = w3.to_wei(0.1, "gwei")
        except Exception:
            pass

        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = await asyncio.to_thread(
            w3.eth.send_raw_transaction, signed.raw_transaction
        )
        receipt = await asyncio.to_thread(
            w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120
        )
        if receipt.status != 1:
            raise RuntimeError(f"Approval TX reverted: {tx_hash.hex()}")
        return tx_hash.hex()

    async def _send_tx(
        self,
        w3: "Web3",
        tx: dict,
        private_key: str,
    ) -> tuple[str, dict]:
        """Sign, send, and wait for a transaction. Returns (tx_hash, receipt)."""
        # EIP-1559 gas
        try:
            latest = await asyncio.to_thread(w3.eth.get_block, "latest")
            if hasattr(latest, "baseFeePerGas") and latest.baseFeePerGas:
                tx["maxFeePerGas"] = latest.baseFeePerGas * 2
                try:
                    tip = await asyncio.to_thread(w3.eth.max_priority_fee)
                    tx["maxPriorityFeePerGas"] = max(w3.to_wei(0.05, "gwei"), min(tip, w3.to_wei(5, "gwei")))
                except Exception:
                    tx["maxPriorityFeePerGas"] = w3.to_wei(0.1, "gwei")
        except Exception:
            pass

        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = await asyncio.to_thread(
            w3.eth.send_raw_transaction, signed.raw_transaction
        )
        receipt = await asyncio.to_thread(
            w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120
        )
        if receipt.status != 1:
            raise RuntimeError(f"TX reverted: {tx_hash.hex()}")
        return tx_hash.hex(), dict(receipt)


# ── Protocol Registry ──────────────────────────────────────────────

_registry: dict[str, PoolAdapter] = {}


def register(adapter: PoolAdapter):
    """Register a protocol adapter."""
    _registry[adapter.PROTOCOL_SLUG] = adapter


def get_adapter(protocol_slug: str) -> PoolAdapter | None:
    """Look up adapter by DefiLlama protocol slug."""
    # Normalize common variations
    slug = protocol_slug.lower().strip()
    if slug in _registry:
        return _registry[slug]
    # Fuzzy: try without version suffix
    base = slug.rsplit("-", 1)[0] if "-v" in slug else slug
    for key, adapter in _registry.items():
        if key.startswith(base):
            return adapter
    return None


def list_adapters() -> dict[str, PoolAdapter]:
    """Return all registered adapters."""
    return dict(_registry)


def get_adapter_for_pool(pool: dict) -> PoolAdapter | None:
    """Find adapter for a DefiLlama pool dict."""
    project = (pool.get("project") or "").lower()
    adapter = get_adapter(project)
    if adapter and adapter.supports_chain(
        _chain_name_to_id(pool.get("chain", ""))
    ):
        return adapter
    return None


def _chain_name_to_id(name: str) -> int:
    """Convert DefiLlama chain name to chain ID."""
    from yield_scanner import CHAIN_MAP_REVERSE
    return CHAIN_MAP_REVERSE.get(name, 0)


# ── Auto-import adapters on first use ───────────────────────────────

def _load_adapters():
    """Import all adapter modules to trigger registration."""
    import importlib
    import pkgutil
    import protocols
    for _, modname, _ in pkgutil.iter_modules(protocols.__path__):
        if modname.startswith("_"):
            continue
        importlib.import_module(f"protocols.{modname}")


_loaded = False


def ensure_loaded():
    global _loaded
    if not _loaded:
        _load_adapters()
        _loaded = True
