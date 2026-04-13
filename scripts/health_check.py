"""
Bot health check — point-in-time operational status.

Usage:
    python scripts/health_check.py [--warn-no-order-mins N] [--warn-no-fill-hours N]

Checks:
  1. Redis — ping
  2. Postgres — connect + query
  3. Wallet USDC.e balance ≥ MIN_USDC_BALANCE
  4. Wallet MATIC balance > 0 (gas)
  5. Recent order activity — last order within --warn-no-order-mins (default 10)
  6. Recent fill activity  — last fill within --warn-no-fill-hours (default 2)
  7. Pending markout tasks — count of fills awaiting 30s mid-price lookup
  8. Prometheus endpoint   — HTTP GET localhost:PROMETHEUS_PORT/metrics

Exit 0 if all checks pass (or WARN-only issues).
Exit 1 if any RED (connectivity / balance) check fails.

Designed to be run as a cron job every 1–5 minutes:
    */2 * * * * /home/botuser/.local/share/pypoetry/venv/bin/poetry run \
        python /home/botuser/polymarket-bot/scripts/health_check.py \
        >> /home/botuser/polymarket-bot/logs/health.log 2>&1
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ERC-20 ABI — only balanceOf
_ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    }
]

# USDC.e on Polygon (6 decimals)
_USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_USDC_DECIMALS = 6

# MATIC minimum — warn if below this (in MATIC, 18 decimals)
_MIN_MATIC_WARN = 0.05

# Status levels
_GREEN = "GREEN"
_WARN  = "WARN"
_RED   = "RED"


def _status_line(label: str, status: str, detail: str) -> str:
    icon = {"GREEN": "✓", "WARN": "⚠", "RED": "✗"}[status]
    return f"  {icon} [{status:5s}] {label}: {detail}"


async def _check_redis(settings) -> tuple[str, str]:
    from redis.asyncio import Redis
    from redis.exceptions import RedisError
    try:
        r = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=3)
        await r.ping()
        await r.aclose()
        return _GREEN, "ping OK"
    except RedisError as exc:
        return _RED, f"ping failed: {exc}"
    except Exception as exc:
        return _RED, f"connection error: {exc}"


async def _check_postgres(settings) -> tuple[str, str]:
    if not settings.DATABASE_URL:
        return _WARN, "DATABASE_URL not set — skipping"
    import asyncpg
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(settings.DATABASE_URL), timeout=5.0
        )
        await conn.fetchval("SELECT 1")
        await conn.close()
        return _GREEN, "query OK"
    except Exception as exc:
        return _RED, f"connection failed: {exc}"


async def _check_wallet_balances(settings) -> list[tuple[str, str, str]]:
    """Returns two check results: USDC.e and MATIC."""
    results = []
    try:
        from web3 import Web3
        from web3.middleware import geth_poa_middleware

        w3 = Web3(Web3.HTTPProvider(settings.POLYGON_RPC_URL, request_kwargs={"timeout": 8}))
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)

        if not w3.is_connected():
            results.append(("Wallet USDC.e", _RED, "RPC unreachable"))
            results.append(("Wallet MATIC", _RED, "RPC unreachable"))
            return results

        account = w3.eth.account.from_key(settings.PRIVATE_KEY)
        address = account.address

        # USDC.e
        usdc = w3.eth.contract(
            address=Web3.to_checksum_address(_USDC_E_ADDRESS),
            abi=_ERC20_ABI,
        )
        raw_usdc = usdc.functions.balanceOf(address).call()
        usdc_balance = raw_usdc / 10 ** _USDC_DECIMALS

        if usdc_balance < settings.MIN_USDC_BALANCE:
            results.append((
                "Wallet USDC.e",
                _RED,
                f"{usdc_balance:.2f} USDC.e — below MIN_USDC_BALANCE ({settings.MIN_USDC_BALANCE})",
            ))
        else:
            results.append(("Wallet USDC.e", _GREEN, f"{usdc_balance:.2f} USDC.e"))

        # MATIC
        raw_matic = w3.eth.get_balance(address)
        matic_balance = raw_matic / 10 ** 18

        if matic_balance < _MIN_MATIC_WARN:
            results.append((
                "Wallet MATIC",
                _WARN,
                f"{matic_balance:.4f} MATIC — low, may not cover gas",
            ))
        else:
            results.append(("Wallet MATIC", _GREEN, f"{matic_balance:.4f} MATIC"))

    except Exception as exc:
        results.append(("Wallet USDC.e", _RED, f"balance check failed: {exc}"))
        results.append(("Wallet MATIC", _RED, f"balance check failed: {exc}"))

    return results


async def _check_recent_orders(settings, warn_mins: int) -> tuple[str, str]:
    if not settings.DATABASE_URL:
        return _WARN, "DATABASE_URL not set — skipping"
    import asyncpg
    try:
        conn = await asyncio.wait_for(asyncpg.connect(settings.DATABASE_URL), timeout=5.0)
        row = await conn.fetchrow(
            "SELECT MAX(created_at) AS last FROM orders WHERE simulated = FALSE"
            if _table_has_simulated_col()
            else "SELECT MAX(created_at) AS last FROM orders"
        )
        await conn.close()

        if row is None or row["last"] is None:
            return _WARN, "no orders in database yet"

        last: datetime = row["last"]
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_mins = (datetime.now(tz=timezone.utc) - last).total_seconds() / 60

        if age_mins > warn_mins:
            return _WARN, f"last order {age_mins:.0f}m ago (threshold: {warn_mins}m)"
        return _GREEN, f"last order {age_mins:.0f}m ago"
    except Exception as exc:
        return _RED, f"query failed: {exc}"


async def _check_recent_fills(settings, warn_hours: int) -> tuple[str, str]:
    if not settings.DATABASE_URL:
        return _WARN, "DATABASE_URL not set — skipping"
    import asyncpg
    try:
        conn = await asyncio.wait_for(asyncpg.connect(settings.DATABASE_URL), timeout=5.0)
        row = await conn.fetchrow(
            "SELECT MAX(fill_timestamp) AS last, COUNT(*) AS total FROM fills "
            "WHERE simulated = FALSE"
        )
        await conn.close()

        if row is None or row["last"] is None:
            return _WARN, "no live fills recorded yet"

        last: datetime = row["last"]
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(tz=timezone.utc) - last).total_seconds() / 3600

        if age_hours > warn_hours:
            return _WARN, (
                f"last fill {age_hours:.1f}h ago (threshold: {warn_hours}h) — "
                f"total: {row['total']}"
            )
        return _GREEN, f"last fill {age_hours:.1f}h ago (total: {row['total']})"
    except Exception as exc:
        return _RED, f"query failed: {exc}"


async def _check_pending_markouts(settings) -> tuple[str, str]:
    """Count fills with markout_30s IS NULL but fill_timestamp > 5 minutes ago.

    These are fills that should have been resolved by the deferred 30s task
    but haven't been. A large number indicates the markout task is stalled.
    """
    if not settings.DATABASE_URL:
        return _WARN, "DATABASE_URL not set — skipping"
    import asyncpg
    try:
        conn = await asyncio.wait_for(asyncpg.connect(settings.DATABASE_URL), timeout=5.0)
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS stuck
            FROM fills
            WHERE strategy = 'A'
              AND simulated = FALSE
              AND markout_30s IS NULL
              AND fill_timestamp < NOW() - INTERVAL '5 minutes'
            """
        )
        await conn.close()

        stuck = row["stuck"] if row else 0
        if stuck > 10:
            return _WARN, f"{stuck} fills missing markout >5min after fill — deferred task may be stalled"
        return _GREEN, f"{stuck} fills awaiting markout (expected ~0)"
    except Exception as exc:
        return _RED, f"query failed: {exc}"


async def _check_prometheus(settings) -> tuple[str, str]:
    import httpx
    port = settings.PROMETHEUS_PORT
    url = f"http://localhost:{port}/metrics"
    try:
        async with httpx.AsyncClient(timeout=3.0) as http:
            r = await http.get(url)
        if r.status_code == 200:
            return _GREEN, f"HTTP 200 on port {port}"
        return _WARN, f"HTTP {r.status_code} on port {port}"
    except Exception as exc:
        return _RED, f"no response on port {port}: {exc}"


def _table_has_simulated_col() -> bool:
    """The orders table may not have a simulated column — use a safe default."""
    return False  # orders table schema doesn't include simulated; fills table does


async def _run(warn_no_order_mins: int, warn_no_fill_hours: int) -> bool:
    from config.settings import Settings

    try:
        s = Settings()
    except Exception as exc:
        log.error("Failed to load Settings: %s", exc)
        return False

    log.info("=== Polymarket Bot Health Check ===")

    # Run all checks concurrently
    (
        redis_result,
        pg_result,
        wallet_results,
        order_result,
        fill_result,
        markout_result,
        prom_result,
    ) = await asyncio.gather(
        _check_redis(s),
        _check_postgres(s),
        _check_wallet_balances(s),
        _check_recent_orders(s, warn_no_order_mins),
        _check_recent_fills(s, warn_no_fill_hours),
        _check_pending_markouts(s),
        _check_prometheus(s),
    )

    checks: list[tuple[str, str, str]] = [
        ("Redis", *redis_result),
        ("Postgres", *pg_result),
        *wallet_results,
        (f"Orders (last {warn_no_order_mins}m)", *order_result),
        (f"Fills (last {warn_no_fill_hours}h)", *fill_result),
        ("Markout task", *markout_result),
        ("Prometheus", *prom_result),
    ]

    any_red = False
    for label, status, detail in checks:
        log.info(_status_line(label, status, detail))
        if status == _RED:
            any_red = True

    total = len(checks)
    red_count   = sum(1 for _, s, _ in checks if s == _RED)
    warn_count  = sum(1 for _, s, _ in checks if s == _WARN)
    green_count = sum(1 for _, s, _ in checks if s == _GREEN)

    log.info(
        "\n  Summary: %d/%d GREEN, %d WARN, %d RED",
        green_count, total, warn_count, red_count,
    )

    if any_red:
        log.error("UNHEALTHY — %d red check(s) require attention", red_count)
    elif warn_count:
        log.warning("DEGRADED — %d warning(s), no critical failures", warn_count)
    else:
        log.info("HEALTHY")

    return not any_red


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--warn-no-order-mins",
        type=int,
        default=10,
        help="WARN if no order placed in this many minutes (default: 10)",
    )
    parser.add_argument(
        "--warn-no-fill-hours",
        type=int,
        default=2,
        help="WARN if no fill in this many hours (default: 2)",
    )
    args = parser.parse_args()

    healthy = asyncio.run(_run(args.warn_no_order_mins, args.warn_no_fill_hours))
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
