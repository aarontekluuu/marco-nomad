"""Tests for post-TX receipt validation — verify_deposit() on PoolAdapter."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# We test through the AaveV3Adapter since it's the simplest (1:1 aToken)
from protocols.aave_v3 import AaveV3Adapter


@pytest.fixture
def adapter():
    return AaveV3Adapter()


@pytest.fixture
def mock_w3():
    """Web3 mock that simulates on-chain calls without a real node."""
    w3 = MagicMock()
    return w3


CHAIN_ID = 8453  # Base
WALLET = "0xabc0000000000000000000000000000000000001"


# ── verify_deposit tests ─────────────────────────────────────────────────────

def test_verify_deposit_success(adapter, mock_w3):
    """Deposit matches expected amount — verification passes."""
    expected = 100.0
    # Simulate aToken balance: 99.8 USDC (within 0.5% tolerance)
    with patch.object(adapter, "balance", new=AsyncMock(return_value=99.8)):
        ok, actual, msg = asyncio.run(
            adapter.verify_deposit(mock_w3, WALLET, CHAIN_ID, expected)
        )
    assert ok is True
    assert actual == pytest.approx(99.8)
    assert "Verified" in msg


def test_verify_deposit_exact(adapter, mock_w3):
    """Deposit matches expected exactly."""
    with patch.object(adapter, "balance", new=AsyncMock(return_value=100.0)):
        ok, actual, msg = asyncio.run(
            adapter.verify_deposit(mock_w3, WALLET, CHAIN_ID, 100.0)
        )
    assert ok is True
    assert actual == 100.0


def test_verify_deposit_zero_balance(adapter, mock_w3):
    """Balance reads 0 — deposit did not land."""
    with patch.object(adapter, "balance", new=AsyncMock(return_value=0.0)):
        ok, actual, msg = asyncio.run(
            adapter.verify_deposit(mock_w3, WALLET, CHAIN_ID, 100.0)
        )
    assert ok is False
    assert actual == 0.0
    assert "0" in msg


def test_verify_deposit_below_tolerance(adapter, mock_w3):
    """Received amount is below the 0.5% slippage tolerance — verification fails."""
    expected = 100.0
    # 98.0 is 2% below expected — outside 0.5% tolerance
    with patch.object(adapter, "balance", new=AsyncMock(return_value=98.0)):
        ok, actual, msg = asyncio.run(
            adapter.verify_deposit(mock_w3, WALLET, CHAIN_ID, expected)
        )
    assert ok is False
    assert actual == pytest.approx(98.0)
    assert "expected" in msg.lower() or "min" in msg.lower()


def test_verify_deposit_at_tolerance_boundary(adapter, mock_w3):
    """Received amount is exactly at 0.5% slippage boundary — should pass."""
    expected = 100.0
    at_boundary = expected * 0.995  # exactly 99.5
    with patch.object(adapter, "balance", new=AsyncMock(return_value=at_boundary)):
        ok, actual, msg = asyncio.run(
            adapter.verify_deposit(mock_w3, WALLET, CHAIN_ID, expected)
        )
    assert ok is True


def test_verify_deposit_custom_tolerance(adapter, mock_w3):
    """Custom tolerance of 1% — 99.0 out of 100 should pass."""
    with patch.object(adapter, "balance", new=AsyncMock(return_value=99.0)):
        ok, actual, msg = asyncio.run(
            adapter.verify_deposit(mock_w3, WALLET, CHAIN_ID, 100.0, tolerance=0.01)
        )
    assert ok is True


def test_verify_deposit_balance_read_fails(adapter, mock_w3):
    """If balance() raises, verify_deposit returns failure gracefully."""
    with patch.object(
        adapter, "balance", new=AsyncMock(side_effect=RuntimeError("RPC error"))
    ):
        ok, actual, msg = asyncio.run(
            adapter.verify_deposit(mock_w3, WALLET, CHAIN_ID, 100.0)
        )
    assert ok is False
    assert actual == 0.0
    assert "failed" in msg.lower() or "error" in msg.lower()


def test_verify_deposit_none_balance(adapter, mock_w3):
    """If balance() returns None (unsupported chain), treat as 0."""
    with patch.object(adapter, "balance", new=AsyncMock(return_value=None)):
        ok, actual, msg = asyncio.run(
            adapter.verify_deposit(mock_w3, WALLET, CHAIN_ID, 100.0)
        )
    assert ok is False


# ── verify_deposit message content ──────────────────────────────────────────

def test_verify_deposit_success_message_has_amounts(adapter, mock_w3):
    """Success message should include both actual and expected amounts."""
    with patch.object(adapter, "balance", new=AsyncMock(return_value=99.9)):
        ok, actual, msg = asyncio.run(
            adapter.verify_deposit(mock_w3, WALLET, CHAIN_ID, 100.0)
        )
    assert ok is True
    assert "99.9" in msg or "99" in msg
    assert "100" in msg


def test_verify_deposit_failure_message_has_amounts(adapter, mock_w3):
    """Failure message should include received and expected amounts."""
    with patch.object(adapter, "balance", new=AsyncMock(return_value=90.0)):
        ok, actual, msg = asyncio.run(
            adapter.verify_deposit(mock_w3, WALLET, CHAIN_ID, 100.0)
        )
    assert ok is False
    assert "90" in msg
    assert "100" in msg
