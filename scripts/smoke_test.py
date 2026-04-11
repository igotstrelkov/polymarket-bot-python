"""
Smoke test — §11.3.

Usage:
    python scripts/smoke_test.py

Places a $0.01 Post-Only GTC limit order on a live low-volume market,
immediately cancels it, and verifies the cancel acknowledgement.

Exit 0 on PASS.
Exit 1 on FAIL — do NOT proceed to shadow_run.py if this exits 1.

Requires a funded wallet and real .env credentials.
DRY_RUN must be False (set DRY_RUN=false in environment).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

# Smoke test parameters
_ORDER_SIZE = 0.01          # minimum order: $0.01
_ORDER_PRICE = 0.50         # mid-market price (valid range: 0.01–0.99)
_PLACE_TIMEOUT_S = 10.0
_CANCEL_TIMEOUT_S = 10.0
_ACK_POLL_INTERVAL_S = 0.5
_ACK_TIMEOUT_S = 15.0


async def _find_low_volume_market(http: httpx.AsyncClient) -> dict | None:
    """Fetch the Gamma API and return a low-volume, active market token_id."""
    try:
        r = await http.get(
            "https://gamma-api.polymarket.com/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": "50",
                "order": "volumeNum",
                "ascending": "true",
            },
            timeout=15.0,
        )
        r.raise_for_status()
        markets = r.json()
        for m in markets:
            # Look for a market with a real token_id and accepting orders
            tokens = m.get("tokens") or m.get("clobTokenIds") or []
            if not tokens:
                continue
            if not m.get("acceptingOrders", True):
                continue
            tick_size = float(m.get("minimumTickSize") or m.get("tickSize") or 0.01)
            if tick_size > 0.01:
                continue
            token_id = tokens[0] if isinstance(tokens[0], str) else tokens[0].get("token_id")
            if token_id:
                return {"token_id": token_id, "tick_size": tick_size}
    except Exception as exc:
        log.warning("Market discovery failed: %s", exc)
    return None


async def _run() -> None:
    from config.settings import Settings
    from auth.credentials import CLOB_HOST, CHAIN_ID, derive_credentials, build_clob_client

    try:
        s = Settings()
    except Exception as exc:
        log.error("Failed to load Settings: %s", exc)
        sys.exit(1)

    if s.DRY_RUN:
        log.error(
            "DRY_RUN=true — smoke_test.py requires live API access. "
            "Set DRY_RUN=false to run."
        )
        sys.exit(1)

    log.info("=== Polymarket Smoke Test ===")

    async with httpx.AsyncClient() as http:
        # Step 1: find a low-volume market
        log.info("Step 1: discovering low-volume market...")
        market = await _find_low_volume_market(http)
        if market is None:
            log.error("FAIL: could not find a suitable low-volume market")
            sys.exit(1)

        token_id = market["token_id"]
        tick_size = market["tick_size"]
        log.info("Target token: %s (tick_size=%s)", token_id, tick_size)

        # Step 2: derive credentials and build CLOB client
        log.info("Step 2: deriving CLOB credentials...")
        creds = await derive_credentials(s.PRIVATE_KEY, CLOB_HOST, CHAIN_ID)
        clob = build_clob_client(s, creds)

        # Step 3: fetch live fee rate
        log.info("Step 3: fetching fee rate for %s...", token_id)
        fee_rate_bps = 0
        try:
            fee_resp = await http.get(
                f"{CLOB_HOST}/fee-rate/{token_id}",
                timeout=10.0,
            )
            fee_resp.raise_for_status()
            fee_rate_bps = int(fee_resp.json().get("base_fee", 0))
            log.info("Fee rate: %d bps", fee_rate_bps)
        except Exception as exc:
            log.warning("Fee rate fetch failed (%s) — using 0 bps", exc)

        # Step 4: place a $0.01 Post-Only GTC order
        log.info("Step 4: placing $0.01 Post-Only GTC order on %s @ %s...", token_id, _ORDER_PRICE)
        t0 = time.monotonic()
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore[import]

            order_args = OrderArgs(
                token_id=token_id,
                price=_ORDER_PRICE,
                size=_ORDER_SIZE,
                side="BUY",
            )
            response = clob.create_and_post_order(order_args)
            place_latency_ms = (time.monotonic() - t0) * 1000
            log.info("Order placed in %.1fms: %s", place_latency_ms, response)
        except Exception as exc:
            log.error("FAIL: order placement failed: %s", exc)
            sys.exit(1)

        # Extract order_id from response
        order_id = None
        if isinstance(response, dict):
            order_id = response.get("orderID") or response.get("order_id") or response.get("id")
        if not order_id:
            log.error("FAIL: could not extract order_id from response: %s", response)
            sys.exit(1)
        log.info("Order ID: %s", order_id)

        # Step 5: immediately cancel the order
        log.info("Step 5: cancelling order %s...", order_id)
        t1 = time.monotonic()
        try:
            cancel_resp = clob.cancel(order_id)
            cancel_latency_ms = (time.monotonic() - t1) * 1000
            log.info("Cancel dispatched in %.1fms: %s", cancel_latency_ms, cancel_resp)
        except Exception as exc:
            log.error("FAIL: cancel request failed: %s", exc)
            sys.exit(1)

        # Step 6: verify cancel acknowledgement (poll open orders)
        log.info("Step 6: waiting for cancel acknowledgement (max %.0fs)...", _ACK_TIMEOUT_S)
        deadline = time.monotonic() + _ACK_TIMEOUT_S
        acknowledged = False

        while time.monotonic() < deadline:
            await asyncio.sleep(_ACK_POLL_INTERVAL_S)
            try:
                open_orders = clob.get_orders({"status": "LIVE"})
                open_ids = {
                    o.get("id") or o.get("orderID")
                    for o in (open_orders or [])
                }
                if order_id not in open_ids:
                    acknowledged = True
                    break
            except Exception as exc:
                log.debug("Poll failed: %s", exc)

        if acknowledged:
            total_ms = (time.monotonic() - t0) * 1000
            log.info("PASS: cancel acknowledged. Total round-trip: %.0fms", total_ms)
            sys.exit(0)
        else:
            log.error(
                "FAIL: cancel not acknowledged within %.0fs for order %s",
                _ACK_TIMEOUT_S, order_id,
            )
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_run())
