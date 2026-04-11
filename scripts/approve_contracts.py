"""
One-time contract approval script.

Usage:
    python scripts/approve_contracts.py

Approves USDC.e for:
  1. Polymarket CLOB Exchange contract
  2. CTF (Conditional Token Framework) contract

Uses web3==6.14.0. Safe to re-run — idempotent (checks allowance first).

Requires:
  PRIVATE_KEY and POLYGON_RPC_URL in environment (or .env file).
  A small amount of MATIC for gas (< $0.01 per approval).
"""

from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Contract addresses (Polygon PoS) ──────────────────────────────────────────

# USDC.e (bridged USDC) on Polygon
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Polymarket CLOB Exchange contract
CLOB_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# Conditional Token Framework (CTF) contract
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# ERC-20 ABI — only the functions we need
_ERC20_ABI = [
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
]

# Max uint256 — standard infinite approval
_MAX_UINT256 = 2 ** 256 - 1

# Approval is sufficient if allowance > this threshold (100K USDC.e, 6 decimals)
_SUFFICIENT_ALLOWANCE = 100_000 * 10 ** 6


async def _run() -> None:
    from config.settings import Settings
    from web3 import Web3  # type: ignore[import]
    from web3.middleware import geth_poa_middleware  # type: ignore[import]

    try:
        s = Settings()
    except Exception as exc:
        log.error("Failed to load Settings: %s", exc)
        sys.exit(1)

    web3 = Web3(Web3.HTTPProvider(s.POLYGON_RPC_URL))
    web3.middleware_onion.inject(geth_poa_middleware, layer=0)

    if not web3.is_connected():
        log.error("Cannot connect to Polygon RPC at %s", s.POLYGON_RPC_URL)
        sys.exit(1)

    account = web3.eth.account.from_key(s.PRIVATE_KEY)
    owner = account.address
    log.info("Wallet address: %s", owner)

    usdc = web3.eth.contract(
        address=Web3.to_checksum_address(USDC_E_ADDRESS),
        abi=_ERC20_ABI,
    )

    for name, spender_raw in [
        ("CLOB Exchange", CLOB_EXCHANGE_ADDRESS),
        ("CTF", CTF_ADDRESS),
    ]:
        spender = Web3.to_checksum_address(spender_raw)
        allowance = usdc.functions.allowance(owner, spender).call()
        log.info("%s current allowance: %d", name, allowance)

        if allowance >= _SUFFICIENT_ALLOWANCE:
            log.info("%s: allowance sufficient — skipping", name)
            continue

        log.info("%s: approving max uint256...", name)
        nonce = web3.eth.get_transaction_count(owner)
        tx = usdc.functions.approve(spender, _MAX_UINT256).build_transaction({
            "from": owner,
            "nonce": nonce,
            "gas": 60_000,
            "gasPrice": web3.eth.gas_price,
        })
        signed = account.sign_transaction(tx)
        tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] == 1:
            log.info("%s: approval confirmed — tx=%s", name, tx_hash.hex())
        else:
            log.error("%s: approval transaction reverted — tx=%s", name, tx_hash.hex())
            sys.exit(1)

    log.info("Contract approvals complete")


if __name__ == "__main__":
    asyncio.run(_run())
