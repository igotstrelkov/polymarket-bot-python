"""
Markout gate report — §11.4 Step 3.

Usage:
    python scripts/markout_report.py [--days N] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--min-fills N]

Queries the durable Postgres fills table for live (non-simulated) Strategy A
fills that have a completed 30-second markout and evaluates the three §11.4
Step 3 gate criteria:

  Criterion 1 — Median markout ≤ +0.5¢  (0.005)
  Criterion 2 — < 30% of fills with markout > +1¢  (0.010)
  Criterion 3 — Net size-weighted markout P&L is positive

Window selection (mutually exclusive with --days):
  --since 2026-04-15           fills from that date onward (UTC midnight)
  --since 2026-04-15 --until 2026-04-22   fills within that range (inclusive)
  --days 14                    last N days up to now (default)

Exit 0 if all three criteria pass.
Exit 1 if any criterion fails or if there are insufficient fills.

⚠️  This is the ONLY valid Strategy A readiness signal before deploying capital.
    paper_trading_report.py criteria 1–2 use SIMULATED fills and do not validate
    adverse-selection risk.

Requires DATABASE_URL in environment (or .env file).
DRY_RUN mode does not affect this script — it reads the database directly.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def _parse_date(value: str) -> datetime:
    """Parse YYYY-MM-DD into a UTC-midnight datetime."""
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date '{value}' — expected YYYY-MM-DD")
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)

# §11.4 Step 3 gate thresholds
_MEDIAN_THRESHOLD = 0.005      # ≤ +0.5¢
_ADVERSE_PCT_THRESHOLD = 0.30  # < 30% of fills
_ADVERSE_CUTOFF = 0.010        # markout > +1¢ counts as adverse
_MIN_FILLS_DEFAULT = 50        # gate requires a minimum sample size


def _percentile(values: list[float], pct: float) -> float:
    """Compute the pct-th percentile of values (0–100 scale)."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(sorted_v) - 1)
    return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * (k - lo)


def evaluate_gate(
    rows: list[dict],
) -> tuple[list[tuple[str, bool, str]], bool]:
    """Evaluate §11.4 Step 3 criteria against a list of fill dicts.

    Each dict must have: markout_30s (float), size (float).

    Returns: (results list, overall_pass bool)
    """
    markouts = [float(r["markout_30s"]) for r in rows]
    sizes = [float(r["size"]) for r in rows]

    median_val = _percentile(markouts, 50.0)
    adverse_count = sum(1 for m in markouts if m > _ADVERSE_CUTOFF)
    adverse_pct = adverse_count / len(markouts) if markouts else 0.0
    # Positive markout = adverse (mid moved against maker's filled side).
    # Net P&L from the market maker's view = -sum(markout * size).
    # Negative markout sum → positive P&L → maker profited (favourable fills).
    net_pnl = -sum(m * s for m, s in zip(markouts, sizes))

    c1_pass = median_val <= _MEDIAN_THRESHOLD
    c2_pass = adverse_pct < _ADVERSE_PCT_THRESHOLD
    c3_pass = net_pnl > 0.0

    results = [
        (
            "1",
            c1_pass,
            f"Median markout: {median_val:+.5f} "
            f"(threshold: ≤ +{_MEDIAN_THRESHOLD:.3f})",
        ),
        (
            "2",
            c2_pass,
            f"Adverse fills (>{_ADVERSE_CUTOFF:.3f}): "
            f"{adverse_count}/{len(markouts)} = {adverse_pct * 100:.1f}% "
            f"(threshold: < {_ADVERSE_PCT_THRESHOLD * 100:.0f}%)",
        ),
        (
            "3",
            c3_pass,
            f"Net maker P&L (size-weighted): {net_pnl:+.4f} USDC "
            f"(threshold: > 0)",
        ),
    ]
    overall = c1_pass and c2_pass and c3_pass
    return results, overall


async def _run(
    since: datetime | None,
    until: datetime | None,
    days: int,
    min_fills: int,
) -> None:
    from config.settings import Settings
    from storage.postgres_client import PostgresClient

    try:
        s = Settings()
    except Exception as exc:
        log.error("Failed to load Settings: %s", exc)
        sys.exit(1)

    client = PostgresClient(dsn=s.DATABASE_URL)
    try:
        await client.connect()
    except Exception as exc:
        log.error("Cannot connect to Postgres: %s", exc)
        sys.exit(1)

    # Build window description and query
    now = datetime.now(tz=timezone.utc)
    if since is not None:
        window_since = since
        window_until = until or now
        window_desc = (
            f"since {since.date()}"
            if until is None
            else f"{since.date()} to {until.date()}"
        )
        query = """
            SELECT markout_30s, size
            FROM fills
            WHERE strategy = 'A'
              AND simulated = FALSE
              AND markout_30s IS NOT NULL
              AND fill_timestamp >= $1
              AND fill_timestamp < $2
            ORDER BY fill_timestamp
        """
        query_args = (window_since, window_until)
    else:
        window_desc = f"last {days} day(s)"
        query = """
            SELECT markout_30s, size
            FROM fills
            WHERE strategy = 'A'
              AND simulated = FALSE
              AND markout_30s IS NOT NULL
              AND fill_timestamp >= NOW() - ($1 * INTERVAL '1 day')
            ORDER BY fill_timestamp
        """
        query_args = (days,)

    try:
        rows = await client.fetch(query, *query_args)
    except Exception as exc:
        log.error("Query failed: %s", exc)
        sys.exit(1)
    finally:
        await client.close()

    n = len(rows)
    log.info(
        "=== Markout Gate Report — §11.4 Step 3 ===\n"
        "Strategy A live fills with markout (%s): %d",
        window_desc,
        n,
    )

    if n < min_fills:
        log.error(
            "FAIL: insufficient fills (%d < %d minimum). "
            "Run longer before evaluating the markout gate.",
            n,
            min_fills,
        )
        sys.exit(1)

    fill_dicts = [{"markout_30s": r["markout_30s"], "size": r["size"]} for r in rows]
    results, overall = evaluate_gate(fill_dicts)

    for num, passed, detail in results:
        status = "PASS" if passed else "FAIL"
        log.info("Criterion %s [%s]: %s", num, status, detail)

    passed_count = sum(1 for _, p, _ in results if p)
    log.info("\n=== Summary: %d/%d criteria passed ===", passed_count, len(results))

    log.warning(
        "\n⚠️  MARKOUT GATE — §11.4 Step 3\n"
        "   This is the ONLY valid Strategy A readiness signal.\n"
        "   Positive markout = adverse (mid moved against maker's filled side).\n"
        "   Negative markout = favourable (mid moved in maker's favour).\n"
        "   Net maker P&L = -sum(markout × size); positive = maker profited.\n"
    )

    if overall:
        log.info(
            "PASS — all 3 markout criteria met over %d fills (%s). "
            "Proceed to enable Strategy A capital.",
            n,
            window_desc,
        )
        sys.exit(0)
    else:
        failed = [f"#{num}" for num, p, _ in results if not p]
        log.error(
            "FAIL — markout gate not cleared: %s. "
            "Do NOT deploy capital until all 3 criteria pass.",
            ", ".join(failed),
        )
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Lookback window in days relative to now (default: 14 when --since not set)",
    )
    parser.add_argument(
        "--since",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Start of window (UTC midnight). Mutually exclusive with --days.",
    )
    parser.add_argument(
        "--until",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="End of window (UTC midnight, exclusive). Only valid with --since.",
    )
    parser.add_argument(
        "--min-fills",
        type=int,
        default=_MIN_FILLS_DEFAULT,
        help=f"Minimum fills required to evaluate gate (default: {_MIN_FILLS_DEFAULT})",
    )
    args = parser.parse_args()

    if args.until and args.since is None:
        parser.error("--until requires --since")
    if args.since and args.days is not None:
        parser.error("--since and --days are mutually exclusive")
    if args.since and args.until and args.until <= args.since:
        parser.error("--until must be after --since")

    # Default to 14 days when neither flag is given
    days = args.days if args.days is not None else 14

    asyncio.run(_run(args.since, args.until, days, args.min_fills))


if __name__ == "__main__":
    main()
