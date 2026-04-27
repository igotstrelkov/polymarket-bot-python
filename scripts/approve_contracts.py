"""
One-time contract approval script.

Usage:
    python scripts/approve_contracts.py

When USE_RELAYER=true (Gnosis Safe / Builder Relayer — production default):
  Uses the CLOB API's update_balance_allowance endpoint, which routes the
  approval through the Builder Relayer as a gasless meta-transaction.

When USE_RELAYER=false (EOA — local dev / fallback):
  Sends a direct EIP-1559 on-chain approve() from the EOA wallet.

Safe to re-run — idempotent (checks allowance first in EOA mode).
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

USDC_E_ADDRESS    = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CLOB_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
CTF_ADDRESS       = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

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

_MAX_UINT256 = 2 ** 256 - 1
_SUFFICIENT_ALLOWANCE = 100_000 * 10 ** 6


async def _fetch_any_token_id(http_client) -> str:
    """Fetch one active token ID from the Gamma API — needed for CONDITIONAL approval."""
    import json as _json

    resp = await http_client.get(
        "https://gamma-api.polymarket.com/markets",
        params={"limit": 5, "active": "true", "closed": "false"},
    )
    resp.raise_for_status()
    markets = resp.json()
    if not markets:
        raise RuntimeError("Gamma API returned no markets")

    for market in markets:
        raw = market.get("clobTokenIds") or []
        # API returns clobTokenIds as a JSON-encoded string in some responses
        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except Exception:
                pass
        if isinstance(raw, list) and raw:
            token_id = raw[0]
            if isinstance(token_id, str) and len(token_id) > 10:
                return token_id

    raise RuntimeError("Could not find a valid token_id in Gamma API response")


async def _approve_via_relayer(s) -> None:
    """Approval via Builder Relayer (USE_RELAYER=true, Type 2 / Gnosis Safe)."""
    import httpx
    from py_clob_client.client import ClobClient  # type: ignore[import]
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    from auth.credentials import CLOB_HOST, CHAIN_ID, build_clob_client, derive_credentials

    log.info("USE_RELAYER=true — using Builder Relayer for approvals")
    creds = await derive_credentials(s.PRIVATE_KEY, CLOB_HOST, CHAIN_ID)
    client = ClobClient(
        host=CLOB_HOST,
        key=s.PRIVATE_KEY,
        chain_id=CHAIN_ID,
        creds=build_clob_client(s, creds).creds,
    )

    # COLLATERAL (USDC.e ERC-20) — no token_id needed
    log.info("Requesting approval for COLLATERAL...")
    try:
        resp = client.update_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        log.info("COLLATERAL: %s", resp)
    except Exception as exc:
        log.error("update_balance_allowance(COLLATERAL) failed: %s", exc)
        sys.exit(1)

    # CONDITIONAL (CTF ERC-1155) — requires a valid token_id
    log.info("Fetching a valid token_id for CONDITIONAL approval...")
    async with httpx.AsyncClient(timeout=10.0) as http:
        token_id = await _fetch_any_token_id(http)
    log.info("Using token_id: %s", token_id)

    log.info("Requesting approval for CONDITIONAL...")
    try:
        resp = client.update_balance_allowance(
            params=BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
            )
        )
        log.info("CONDITIONAL: %s", resp)
    except Exception as exc:
        log.error("update_balance_allowance(CONDITIONAL) failed: %s", exc)
        sys.exit(1)

    log.info("Relayer approvals complete")


async def _approve_via_eoa(s) -> None:
    """Direct on-chain approval (USE_RELAYER=false, Type 0 / EOA)."""
    from web3 import Web3  # type: ignore[import]

    log.info("USE_RELAYER=false — using direct EOA approval")
    web3 = Web3(Web3.HTTPProvider(s.POLYGON_RPC_URL))
    if not web3.is_connected():
        log.error("Cannot connect to Polygon RPC at %s", s.POLYGON_RPC_URL)
        sys.exit(1)

    account = web3.eth.account.from_key(s.PRIVATE_KEY)
    owner = account.address
    log.info("Wallet address: %s", owner)

    matic_wei = web3.eth.get_balance(owner)
    log.info("MATIC balance: %.4f", matic_wei / 1e18)
    if matic_wei < web3.to_wei(0.01, "ether"):
        log.error("Insufficient MATIC — need at least 0.01 MATIC for gas")
        sys.exit(1)

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

        log.info("%s: approving max uint256 (EIP-1559)...", name)
        nonce = web3.eth.get_transaction_count(owner)
        base_fee = web3.eth.get_block("latest")["baseFeePerGas"]
        priority_fee = web3.to_wei(30, "gwei")
        tx = usdc.functions.approve(spender, _MAX_UINT256).build_transaction({
            "from": owner,
            "nonce": nonce,
            "gas": 100_000,
            "maxFeePerGas": base_fee * 2 + priority_fee,
            "maxPriorityFeePerGas": priority_fee,
        })
        signed = account.sign_transaction(tx)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        log.info("%s: tx submitted — %s", name, tx_hash.hex())
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] == 1:
            log.info("%s: approval confirmed", name)
        else:
            log.error(
                "%s: approval reverted — check https://polygonscan.com/tx/0x%s",
                name, tx_hash.hex(),
            )
            sys.exit(1)

    log.info("EOA approvals complete")


async def _run() -> None:
    from config.settings import Settings

    try:
        s = Settings()
    except Exception as exc:
        log.error("Failed to load Settings: %s", exc)
        sys.exit(1)

    if s.USE_RELAYER:
        await _approve_via_relayer(s)
    else:
        await _approve_via_eoa(s)


if __name__ == "__main__":
    asyncio.run(_run())
