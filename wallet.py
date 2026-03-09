"""Wallet module - tracks position state and chain location."""

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / "wallet_state.json"
WALLET_DIR = Path.home() / ".marco"
WALLET_FILE = WALLET_DIR / "wallet.json"
MIN_POSITION_USD = 5.0  # Never migrate if position would drop below this
MIN_MIGRATION_INTERVAL_HOURS = 4  # Cooldown between migrations to prevent thrashing
MAX_MIGRATIONS = 200  # Cap migration history to prevent wallet_state.json bloat
MAX_TX_VALUE_USD = 50.0  # SAFETY: Hard cap on single TX value — prevents draining wallet
MAX_DAILY_BRIDGE_COST_USD = 5.0  # SAFETY: Max cumulative bridge costs per 24h

# TWAP execution config
TWAP_ENABLED = os.getenv("TWAP_ENABLED", "false").lower() == "true"
TWAP_CHUNKS = int(os.getenv("TWAP_CHUNKS", "3"))
TWAP_INTERVAL_SECONDS = int(os.getenv("TWAP_INTERVAL", "600"))  # 10min between chunks
TWAP_MIN_POSITION_USD = 50.0  # Only TWAP for positions above this

# Stop-loss config
STOP_LOSS_CYCLES = int(os.getenv("STOP_LOSS_CYCLES", "3"))
STOP_LOSS_ENABLED = os.getenv("STOP_LOSS_ENABLED", "true").lower() == "true"

# Well-known USDC addresses per chain (must match yield_scanner.CHAIN_MAP)
USDC = {
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",       # Ethereum
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",     # Base
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",    # Arbitrum
    10: "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",       # Optimism
    137: "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",      # Polygon
    56: "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",       # BSC
    43114: "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",   # Avalanche
    250: "0x04068DA6C83AFCFA0e13ba15A6696662335D5B75",      # Fantom
    324: "0x1d17CBcF0D6D143135aE902365D2E5e2A16538D4",      # zkSync Era
    59144: "0x176211869cA2b568f2A7D4EE941E073a821EE1ff",    # Linea (USDC.e)
    534352: "0x06eFdBFf2a14a7c8E15944D1F4A48F9F95F663A4",  # Scroll
}

USDC_DECIMALS = 6

# Stablecoins Marco can hold — keyed by (chain_id, symbol) -> {address, decimals}
# Used for same-chain swaps when a DAI/USDT pool pays better than USDC
STABLECOINS = {
    # Ethereum
    (1, "USDC"): {"address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": 6},
    (1, "USDT"): {"address": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "decimals": 6},
    (1, "DAI"):  {"address": "0x6B175474E89094C44Da98b954EedeAC495271d0F", "decimals": 18},
    # Base
    (8453, "USDC"):  {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
    (8453, "USDbC"): {"address": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", "decimals": 6},
    (8453, "DAI"):   {"address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "decimals": 18},
    # Arbitrum
    (42161, "USDC"):  {"address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
    (42161, "USDT"):  {"address": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", "decimals": 6},
    (42161, "DAI"):   {"address": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1", "decimals": 18},
    # Optimism
    (10, "USDC"): {"address": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", "decimals": 6},
    (10, "USDT"): {"address": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58", "decimals": 6},
    (10, "DAI"):  {"address": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1", "decimals": 18},
    # Polygon
    (137, "USDC"): {"address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "decimals": 6},
    (137, "USDT"): {"address": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", "decimals": 6},
    (137, "DAI"):  {"address": "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063", "decimals": 18},
}

# Which stablecoin symbols Marco can swap into (all pegged to $1)
ALLOWED_STABLES = {"USDC", "USDT", "DAI", "USDbC", "USDC.e", "FRAX", "LUSD", "GHO", "PYUSD", "crvUSD"}


# --- AAR-89: Gas-aware execution ---
MAX_GAS_GWEI_L1 = float(os.getenv("MAX_GAS_GWEI_L1", "30"))
MAX_GAS_GWEI_L2 = float(os.getenv("MAX_GAS_GWEI_L2", "0.5"))
L2_CHAINS = {8453, 42161, 10, 137, 324, 59144, 534352}


def check_gas_price(chain_id: int, rpc_url: str) -> tuple[bool, float]:
    """Check if gas price is acceptable. Returns (ok, current_gwei)."""
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        gas_price = w3.eth.gas_price
        gwei = gas_price / 1e9
        max_gwei = MAX_GAS_GWEI_L2 if chain_id in L2_CHAINS else MAX_GAS_GWEI_L1
        return gwei <= max_gwei, gwei
    except Exception:
        return True, 0  # Fail-open on gas check failure


# --- AAR-95: Adaptive slippage ---
def calc_adaptive_slippage(pool: dict, position_usd: float) -> float:
    """Calculate slippage based on pool liquidity and position size."""
    tvl = pool.get("tvlUsd", 0)
    ratio = position_usd / tvl if tvl > 0 else 1.0
    if ratio < 0.0001:  # < 0.01% of pool TVL
        return 0.001  # 0.1% - very tight
    elif ratio < 0.001:  # < 0.1% of pool TVL
        return 0.003  # 0.3%
    elif ratio < 0.01:  # < 1% of pool TVL
        return 0.005  # 0.5% - default
    else:
        return 0.01  # 1% - wider for large positions relative to pool


# --- AAR-100: Strategy profiles ---
STRATEGY_PROFILES = {
    "conservative": {
        "min_tvl": 10_000_000, "min_apy": 2.0, "max_bridge_cost_pct": 1.0,
        "min_confidence": 0.8, "trusted_only": True, "description": "Trusted protocols only, high TVL, low risk"
    },
    "balanced": {
        "min_tvl": 500_000, "min_apy": 3.0, "max_bridge_cost_pct": 2.0,
        "min_confidence": 0.6, "trusted_only": False, "description": "Default — moderate risk/reward"
    },
    "aggressive": {
        "min_tvl": 100_000, "min_apy": 5.0, "max_bridge_cost_pct": 3.0,
        "min_confidence": 0.4, "trusted_only": False, "description": "Higher APY chase, lower thresholds"
    },
}


def get_strategy(state: dict) -> str:
    return state.get("_strategy", "balanced")


def set_strategy(state: dict, profile: str):
    if profile not in STRATEGY_PROFILES:
        raise ValueError(f"Unknown strategy: {profile}. Options: {', '.join(STRATEGY_PROFILES)}")
    state["_strategy"] = profile
    save_state(state)


def validate_private_key(private_key: str) -> tuple[bool, str, str]:
    """Validate a private key and derive its address.

    Returns (valid, address, error_message).
    SECURITY: Never logs or stores the key itself — only the derived address.
    """
    if not private_key:
        return False, "", "No private key provided"
    # Strip whitespace and optional 0x prefix for validation
    key = private_key.strip()
    if key.startswith("0x") or key.startswith("0X"):
        key_hex = key[2:]
    else:
        key_hex = key
    # Must be exactly 64 hex characters (32 bytes)
    if len(key_hex) != 64:
        return False, "", f"Invalid key length: expected 64 hex chars, got {len(key_hex)}"
    try:
        int(key_hex, 16)
    except ValueError:
        return False, "", "Key contains non-hex characters"
    # Derive address (requires web3)
    try:
        from eth_account import Account
        acct = Account.from_key(key)
        return True, acct.address, ""
    except ImportError:
        # Can't derive without web3, but key format is valid
        return True, "", "web3 not installed — cannot derive address"
    except Exception as e:
        return False, "", f"Key derivation failed: {e}"


def create_wallet() -> tuple[str, str]:
    """Create a new Ethereum wallet for Marco. Returns (address, private_key).

    Stores the key at ~/.marco/wallet.json with restrictive permissions.
    If a wallet already exists, returns the existing one.
    SECURITY: File is chmod 600 (owner-read/write only).
    """
    WALLET_DIR.mkdir(parents=True, exist_ok=True)

    if WALLET_FILE.exists():
        data = json.loads(WALLET_FILE.read_text())
        pk = data.get("privateKey", "")
        if pk:
            valid, addr, _ = validate_private_key(pk)
            if valid and addr:
                return addr, pk

    # Generate new wallet
    from eth_account import Account
    acct = Account.create()
    wallet_data = {
        "privateKey": acct.key.hex(),
        "address": acct.address,
        "createdAt": datetime.now().isoformat(),
        "source": "marco-nomad",
    }
    WALLET_FILE.write_text(json.dumps(wallet_data, indent=2))
    os.chmod(WALLET_FILE, 0o600)
    logger.info(f"Created new Marco wallet: {acct.address}")
    return acct.address, acct.key.hex()


def load_wallet() -> tuple[str, str] | None:
    """Load existing wallet from ~/.marco/wallet.json.

    Returns (address, private_key) or None if no wallet exists.
    """
    # 1. Check Marco's own wallet
    if WALLET_FILE.exists():
        try:
            data = json.loads(WALLET_FILE.read_text())
            pk = data.get("privateKey", "")
            if pk:
                valid, addr, _ = validate_private_key(pk)
                if valid and addr:
                    return addr, pk
        except (json.JSONDecodeError, OSError):
            pass

    # 2. Fallback: env var
    pk = os.getenv("WALLET_PRIVATE_KEY", "")
    if pk:
        valid, addr, _ = validate_private_key(pk)
        if valid and addr:
            return addr, pk

    return None


def check_wallet_address_match(state: dict, private_key: str) -> tuple[bool, str]:
    """Verify the private key matches the configured wallet address.

    CRITICAL: Prevents sending funds to an address you don't control,
    or signing TXs from a different wallet than intended.
    """
    valid, derived_addr, err = validate_private_key(private_key)
    if not valid:
        return False, f"Invalid private key: {err}"
    if not derived_addr:
        # In LIVE mode, web3 is required to verify address — fail-closed
        return False, "Cannot verify address: web3/eth_account not installed. Required for LIVE mode."
    configured_addr = state.get("address", "")
    if not configured_addr:
        # No address configured — auto-set from key
        state["address"] = derived_addr
        save_state(state)
        return True, f"Wallet address set to {derived_addr}"
    if derived_addr.lower() != configured_addr.lower():
        return False, (
            f"MISMATCH: private key derives {derived_addr} but "
            f"wallet_state has {configured_addr}. "
            f"Refusing to sign — would lose funds."
        )
    return True, "Address verified"


def load_state() -> dict:
    """Load wallet state from disk."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "address": os.getenv("WALLET_ADDRESS", ""),
        "current_chain": 8453,
        "current_token": "USDC",  # Which stablecoin Marco currently holds
        "current_pool": None,
        "position_usd": float(os.getenv("POSITION_SIZE_USD", "100")),
        "migrations": [],
    }


def save_state(state: dict):
    """Save wallet state to disk atomically (write to tmp, then rename)."""
    data = json.dumps(state, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=STATE_FILE.parent, suffix=".tmp")
    try:
        os.write(fd, data.encode())
        os.close(fd)
        fd = -1  # Mark as closed
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def can_migrate(state: dict, cost_usd: float) -> tuple[bool, str]:
    """Check if migration is safe. Returns (allowed, reason).

    Safety gates (all fail-closed):
    1. Minimum position after cost deduction
    2. Migration cooldown (prevent thrashing)
    3. Single TX value cap (prevent draining)
    4. Daily bridge cost cap (prevent fee bleed)
    """
    position = state.get("position_usd", 0)
    if position - cost_usd < MIN_POSITION_USD:
        return False, f"Position ${position:.2f} - bridge ${cost_usd:.2f} = ${position - cost_usd:.2f} < min ${MIN_POSITION_USD}"

    # SAFETY: Hard cap on single TX value
    if position > MAX_TX_VALUE_USD:
        return False, f"Position ${position:.2f} exceeds max TX value ${MAX_TX_VALUE_USD}. Increase MAX_TX_VALUE_USD to proceed."

    migrations = state.get("migrations", [])
    if migrations:
        last_ts = migrations[-1].get("timestamp", "")
        try:
            last_dt = datetime.fromisoformat(last_ts)
            hours_since = (datetime.now() - last_dt).total_seconds() / 3600
            if hours_since < MIN_MIGRATION_INTERVAL_HOURS:
                return False, f"Cooldown: {hours_since:.1f}h since last migration (min {MIN_MIGRATION_INTERVAL_HOURS}h)"
        except (ValueError, TypeError):
            # Fail-closed: if timestamp is corrupt, enforce cooldown to prevent rapid migrations
            return False, "Cooldown: last migration timestamp is invalid — blocking until next cycle"

        # SAFETY: Daily bridge cost cap — sum costs from last 24h
        now = datetime.now()
        daily_cost = 0.0
        for m in migrations:
            try:
                m_dt = datetime.fromisoformat(m.get("timestamp", ""))
                if (now - m_dt).total_seconds() < 86400:
                    daily_cost += m.get("cost_usd", 0)
            except (ValueError, TypeError):
                continue
        if daily_cost + cost_usd > MAX_DAILY_BRIDGE_COST_USD:
            return False, f"Daily cost cap: ${daily_cost:.2f} spent + ${cost_usd:.2f} = ${daily_cost + cost_usd:.2f} > ${MAX_DAILY_BRIDGE_COST_USD} limit"

    return True, "ok"


def record_migration(
    state: dict, from_chain: int, to_chain: int, pool: dict,
    cost_usd: float, reason: str, to_token: str | None = None,
):
    """Record a migration decision. Caps history at MAX_MIGRATIONS."""
    from_token = state.get("current_token", "USDC")
    target_token = to_token or _infer_pool_token(pool)
    is_swap = from_chain == to_chain and from_token != target_token

    state["migrations"].append({
        "timestamp": datetime.now().isoformat(),
        "from_chain": from_chain,
        "to_chain": to_chain,
        "from_token": from_token,
        "to_token": target_token,
        "type": "swap" if is_swap else "bridge",
        "pool_symbol": pool.get("symbol", "?"),
        "pool_project": pool.get("project", "?"),
        "pool_apy": pool.get("apy", 0),
        "cost_usd": cost_usd,
        "reason": reason,
    })
    # Cap migration history
    if len(state["migrations"]) > MAX_MIGRATIONS:
        state["migrations"] = state["migrations"][-MAX_MIGRATIONS:]
    state["current_chain"] = to_chain
    state["current_token"] = target_token
    state["current_pool"] = {
        "pool_id": pool.get("pool"),  # DefiLlama unique pool UUID
        "symbol": pool.get("symbol"),
        "project": pool.get("project"),
        "chain": pool.get("chain"),
        "apy": pool.get("apy"),
    }
    save_state(state)


def _infer_pool_token(pool: dict) -> str:
    """Infer the primary token from a pool symbol. Returns 'USDC' as default."""
    symbol = (pool.get("symbol") or "USDC").upper()
    # Single token pool
    if symbol in ALLOWED_STABLES or symbol in {"USDC.E", "USDBC"}:
        return symbol
    # LP pair — return first recognized stablecoin
    for sep in ("-", "/"):
        if sep in symbol:
            for part in symbol.split(sep):
                part = part.strip()
                if part in ALLOWED_STABLES:
                    return part
    return "USDC"


BALANCE_OF_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]

ERC20_TRANSFER_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]

# Pre-approved withdrawal addresses (set in .env as comma-separated)
OWNER_ADDRESSES = [
    a.strip().lower()
    for a in os.getenv("OWNER_ADDRESSES", "").split(",")
    if a.strip()
]


def withdraw(
    state: dict, to_address: str, amount_usd: float, private_key: str,
) -> dict:
    """Withdraw stablecoins to a pre-approved owner address.

    SAFETY:
    - Only sends to addresses in OWNER_ADDRESSES allowlist
    - Verifies on-chain balance before sending
    - Maximum single withdrawal capped at position size
    - Returns {"tx_hash": str, "amount": float, "to": str} or raises

    Must be called from a thread (sync web3 calls).
    """
    from web3 import Web3

    to_lower = to_address.strip().lower()
    if not OWNER_ADDRESSES:
        raise ValueError("No OWNER_ADDRESSES configured in .env. Set it before withdrawing.")
    if to_lower not in OWNER_ADDRESSES:
        raise ValueError(
            f"Address {to_address} is not in OWNER_ADDRESSES allowlist. "
            f"Approved: {', '.join(OWNER_ADDRESSES)}"
        )

    position = state.get("position_usd", 0)
    if amount_usd <= 0 or amount_usd > position:
        raise ValueError(f"Invalid amount: ${amount_usd:.2f} (position: ${position:.2f})")

    chain_id = state["current_chain"]
    token = state.get("current_token", "USDC")
    stable_info = STABLECOINS.get((chain_id, token))
    if stable_info:
        token_addr = stable_info["address"]
        decimals = stable_info["decimals"]
    else:
        token_addr = USDC.get(chain_id)
        decimals = USDC_DECIMALS

    if not token_addr:
        raise ValueError(f"No token address for {token} on chain {chain_id}")

    from lifi import RPC_URLS
    rpc_url = RPC_URLS.get(chain_id)
    if not rpc_url:
        raise ValueError(f"No RPC URL for chain {chain_id}")

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    wallet_addr = state.get("address", "")
    sender = Web3.to_checksum_address(wallet_addr)
    recipient = Web3.to_checksum_address(to_address)

    # Verify on-chain balance
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_addr),
        abi=ERC20_TRANSFER_ABI,
    )
    on_chain_raw = contract.functions.balanceOf(sender).call()
    on_chain_usd = on_chain_raw / (10 ** decimals)
    if on_chain_usd < amount_usd * 0.95:
        raise ValueError(
            f"On-chain balance ${on_chain_usd:.2f} < requested ${amount_usd:.2f}"
        )

    amount_raw = int(amount_usd * 10 ** decimals)

    # Build transfer TX
    nonce = w3.eth.get_transaction_count(sender, "pending")
    tx = contract.functions.transfer(recipient, amount_raw).build_transaction({
        "from": sender,
        "nonce": nonce,
        "chainId": chain_id,
    })

    # EIP-1559 gas
    try:
        latest = w3.eth.get_block("latest")
        if hasattr(latest, "baseFeePerGas") and latest.baseFeePerGas:
            tx["maxFeePerGas"] = latest.baseFeePerGas * 2
            tx["maxPriorityFeePerGas"] = w3.to_wei(0.1, "gwei")
    except Exception:
        pass

    # Sign and send
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status != 1:
        raise RuntimeError(f"Transfer TX reverted: {tx_hash.hex()}")

    # Update state
    state["position_usd"] = round(state["position_usd"] - amount_usd, 2)
    save_state(state)

    return {
        "tx_hash": tx_hash.hex(),
        "amount": amount_usd,
        "to": to_address,
        "token": token,
    }


def check_onchain_balance(
    chain_id: int, wallet_address: str, rpc_url: str, token: str = "USDC",
) -> float | None:
    """Query actual stablecoin balance on-chain. Returns USD amount or None on failure."""
    try:
        from web3 import Web3
        # Look up token address from STABLECOINS registry, fallback to USDC
        stable_info = STABLECOINS.get((chain_id, token))
        if stable_info:
            token_addr = stable_info["address"]
            decimals = stable_info["decimals"]
        else:
            token_addr = USDC.get(chain_id)
            decimals = USDC_DECIMALS
        if not token_addr or not wallet_address:
            return None
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr),
            abi=BALANCE_OF_ABI,
        )
        raw = contract.functions.balanceOf(
            Web3.to_checksum_address(wallet_address)
        ).call()
        return raw / (10 ** decimals)
    except Exception:
        return None


def reconcile_balance(state: dict, rpc_url: str) -> float | None:
    """Compare tracked position_usd with on-chain balance. Returns drift or None."""
    actual = check_onchain_balance(
        state["current_chain"],
        state.get("address", ""),
        rpc_url,
        token=state.get("current_token", "USDC"),
    )
    if actual is None:
        return None
    tracked = state.get("position_usd", 0)
    drift = actual - tracked
    if abs(drift) > 0.01:  # More than 1 cent drift
        # Safety: refuse auto-correction for large drifts (>10% of position)
        # This prevents silent state corruption from failed bridges or double-spends
        drift_pct = abs(drift) / tracked * 100 if tracked > 0 else 100
        if drift_pct > 10:
            import logging
            logging.getLogger(__name__).critical(
                f"LARGE DRIFT: on-chain ${actual:.2f} vs tracked ${tracked:.2f} "
                f"(drift: ${drift:+.2f}, {drift_pct:.1f}%). Refusing auto-correction."
            )
            state["_drift_alert"] = {
                "timestamp": datetime.now().isoformat(),
                "on_chain": round(actual, 2),
                "tracked": round(tracked, 2),
                "drift_usd": round(drift, 4),
                "drift_pct": round(drift_pct, 2),
            }
            save_state(state)
            return drift  # Return drift but don't update position
        state["position_usd"] = round(actual, 2)
        state["_last_reconcile"] = datetime.now().isoformat()
        state["_last_drift_usd"] = round(drift, 4)
        state.pop("_drift_alert", None)  # Clear any previous alert
        save_state(state)
    return drift


def get_limits(state: dict) -> list[dict]:
    """Get active limit orders."""
    return state.get("_limits", [])


def add_limit(state: dict, chain: str, min_apy: float, description: str = ""):
    """Add a limit order that triggers when a chain's APY exceeds min_apy."""
    limits = state.get("_limits", [])
    limits.append({
        "chain": chain,
        "min_apy": min_apy,
        "description": description,
        "created": datetime.now().isoformat(),
    })
    state["_limits"] = limits
    save_state(state)


def remove_limit(state: dict, index: int):
    """Remove a limit order by index."""
    limits = state.get("_limits", [])
    if 0 <= index < len(limits):
        limits.pop(index)
        state["_limits"] = limits
        save_state(state)


def check_stop_loss(state: dict, current_apy: float, min_apy: float) -> bool:
    """Track consecutive below-threshold cycles. Returns True if stop-loss triggered."""
    if not STOP_LOSS_ENABLED:
        return False
    if current_apy >= min_apy:
        state["_stop_loss_count"] = 0
        return False
    count = state.get("_stop_loss_count", 0) + 1
    state["_stop_loss_count"] = count
    return count >= STOP_LOSS_CYCLES


def calc_pnl(state: dict) -> dict:
    """Calculate realized PnL from migration history."""
    migrations = state.get("migrations", [])
    total_costs = sum(m.get("cost_usd", 0) for m in migrations)
    initial = float(os.getenv("POSITION_SIZE_USD", "10"))
    current = state.get("position_usd", 0)
    # Yield earned = current_position + total_costs - initial
    # (costs were deducted from position, so add them back to see gross)
    gross_yield = current + total_costs - initial
    net_pnl = current - initial
    roi_pct = (net_pnl / initial * 100) if initial > 0 else 0
    return {
        "initial": initial,
        "current": current,
        "total_costs": total_costs,
        "gross_yield": gross_yield,
        "net_pnl": net_pnl,
        "roi_pct": roi_pct,
        "num_migrations": len(migrations),
    }


def get_positions(state: dict) -> list[dict]:
    """Get all positions. Currently single-position, but schema supports multiple."""
    positions = state.get("_positions", [])
    if not positions:
        # Backwards compat: derive from legacy single-position state
        return [{
            "chain_id": state.get("current_chain", 8453),
            "token": state.get("current_token", "USDC"),
            "amount_usd": state.get("position_usd", 0),
            "pool": state.get("current_pool"),
        }]
    return positions

MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "3"))

# DCA mode
DCA_ENABLED = os.getenv("DCA_ENABLED", "false").lower() == "true"
DCA_CHUNKS = int(os.getenv("DCA_CHUNKS", "3"))

def get_dca_state(state: dict) -> dict | None:
    """Get active DCA state if DCA is in progress."""
    return state.get("_dca")

def start_dca(state: dict, target_chain: str, total_chunks: int = DCA_CHUNKS):
    state["_dca"] = {
        "target_chain": target_chain,
        "total_chunks": total_chunks,
        "completed_chunks": 0,
        "started_at": datetime.now().isoformat(),
    }
    save_state(state)

def advance_dca(state: dict) -> bool:
    """Advance DCA by one chunk. Returns True if DCA is complete."""
    dca = state.get("_dca")
    if not dca:
        return True
    dca["completed_chunks"] += 1
    if dca["completed_chunks"] >= dca["total_chunks"]:
        state.pop("_dca", None)
        save_state(state)
        return True
    save_state(state)
    return False

# Auto-compound
COMPOUND_ENABLED = os.getenv("COMPOUND_ENABLED", "false").lower() == "true"

# Protocol reward token claim ABIs — extend as protocols are supported
COMPOUND_PROTOCOLS = {
    "aave-v3": {
        "description": "Claim AAVE rewards via IncentivesController",
        "supported": False,  # TODO: implement claim ABI
    },
    "moonwell": {
        "description": "Claim WELL rewards",
        "supported": False,
    },
}

def get_compound_status() -> dict:
    """Return which protocols support auto-compound."""
    return {k: v for k, v in COMPOUND_PROTOCOLS.items()}


# --- AAR-97: Multi-chain balance view ---
def check_all_chain_balances(wallet_address: str) -> dict[str, float]:
    """Check USDC balance on all supported chains. Returns {chain_name: balance_usd}."""
    from lifi import RPC_URLS
    from yield_scanner import CHAIN_MAP
    balances = {}
    for chain_id, rpc_url in RPC_URLS.items():
        chain_name = CHAIN_MAP.get(chain_id, f"Chain {chain_id}")
        bal = check_onchain_balance(chain_id, wallet_address, rpc_url)
        if bal is not None and bal > 0.01:
            balances[chain_name] = round(bal, 2)
    return balances


def format_state(state: dict) -> str:
    """Format current wallet state for display."""
    from yield_scanner import CHAIN_MAP
    pool = state.get("current_pool")
    chain_name = CHAIN_MAP.get(state["current_chain"], f"Chain {state['current_chain']}")
    token = state.get("current_token", "USDC")
    pool_str = f"{pool['symbol']} on {pool['project']} — {pool['apy']:.1f}% APY" if pool else "None"
    migrations = state.get("migrations", [])
    total_cost = sum(m.get("cost_usd", 0) for m in migrations)
    return (
        f"${state['position_usd']:.2f} {token} on {chain_name} | "
        f"Pool: {pool_str} | "
        f"Migrations: {len(migrations)} (${total_cost:.2f} total cost)"
    )
