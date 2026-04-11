"""
Paper trading report — §11.4 Step 2.

Usage:
    python scripts/paper_trading_report.py [--log-dir PATH] [--days N]

Reads structured JSON log lines from the bot's log output and evaluates
8 readiness criteria. Exits 0 if all criteria pass, 1 if any fail.

⚠️  Criteria 1–2 use SIMULATED fills. They confirm the state machine runs.
    They do NOT validate Strategy A profitability or adverse selection risk.
    The live markout gate (§11.4 Step 3) is the only real readiness signal.

Expected log format (one JSON object per line):
    {"event_type": "FILL", "timestamp": ..., "pnl": ..., "strategy": "A", ...}
    {"event_type": "STATUS_REPORT", "timestamp": ..., "fee_cache_hit_ratio": ..., ...}
    {"event_type": "INVENTORY_HALT", "timestamp": ..., ...}
    {"event_type": "MARKET_RESOLVED", "timestamp": ..., "flagged": true/false, ...}
    {"event_type": "REDEMPTION_SUCCESS", "timestamp": ..., ...}
    {"event_type": "ORDER_SCORED", "timestamp": ..., "token_id": ..., ...}
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ── Log parsing ────────────────────────────────────────────────────────────────

def parse_log_events(log_dir: Path) -> list[dict]:
    """Read all *.jsonl and *.log files from log_dir and return parsed events."""
    events: list[dict] = []
    patterns = ["*.jsonl", "*.log", "*.json"]
    files = [f for pat in patterns for f in sorted(log_dir.glob(pat))]

    if not files:
        log.warning("No log files found in %s", log_dir)
        return events

    for path in files:
        log.info("Reading %s", path)
        try:
            with path.open() as fh:
                for lineno, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict) and "event_type" in obj:
                            events.append(obj)
                    except json.JSONDecodeError:
                        pass  # skip non-JSON lines (plain text log lines)
        except OSError as exc:
            log.warning("Cannot read %s: %s", path, exc)

    log.info("Parsed %d events from %d file(s)", len(events), len(files))
    return events


def _ts(event: dict) -> datetime | None:
    """Extract UTC timestamp from event dict."""
    raw = event.get("timestamp")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


# ── Criterion evaluators ───────────────────────────────────────────────────────

def _criterion_1_pnl_positive_10_of_14(events: list[dict], days: int) -> tuple[bool, str]:
    """[SIMULATED] Simulated P&L positive for ≥ 10 of the last 14 days."""
    daily_pnl: dict[str, float] = defaultdict(float)

    for e in events:
        if e.get("event_type") != "FILL":
            continue
        ts = _ts(e)
        if ts is None:
            continue
        day_key = ts.strftime("%Y-%m-%d")
        pnl = float(e.get("pnl", 0.0))
        daily_pnl[day_key] += pnl

    if not daily_pnl:
        return False, "No FILL events found — cannot evaluate simulated P&L"

    recent_days = sorted(daily_pnl.keys())[-days:]
    positive_days = sum(1 for d in recent_days if daily_pnl[d] > 0)
    threshold = max(1, int(days * 10 / 14))  # ≥ 10/14 of requested days

    passed = positive_days >= threshold
    detail = (
        f"Positive days: {positive_days}/{len(recent_days)} "
        f"(threshold: ≥{threshold}) [SIMULATED — NOT PROFITABILITY]"
    )
    return passed, detail


def _criterion_2_max_drawdown(events: list[dict], capital: float) -> tuple[bool, str]:
    """[SIMULATED] Simulated max drawdown ≤ 5% of capital."""
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0

    for e in events:
        if e.get("event_type") != "FILL":
            continue
        cumulative += float(e.get("pnl", 0.0))
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    if capital <= 0:
        return False, "Capital must be > 0"

    drawdown_pct = (max_drawdown / capital) * 100
    passed = drawdown_pct <= 5.0
    detail = (
        f"Max drawdown: ${max_drawdown:.2f} ({drawdown_pct:.2f}% of ${capital:.0f} capital) "
        f"[SIMULATED — NOT PROFITABILITY]"
    )
    return passed, detail


def _criterion_3_trade_count(events: list[dict]) -> tuple[bool, str]:
    """Trade count average > 100/day."""
    daily_trades: dict[str, int] = defaultdict(int)

    for e in events:
        if e.get("event_type") != "FILL":
            continue
        ts = _ts(e)
        if ts is None:
            continue
        daily_trades[ts.strftime("%Y-%m-%d")] += 1

    if not daily_trades:
        return False, "No FILL events found"

    avg = sum(daily_trades.values()) / len(daily_trades)
    passed = avg > 100
    detail = (
        f"Average trades/day: {avg:.1f} over {len(daily_trades)} day(s) "
        f"(threshold: >100)"
    )
    return passed, detail


def _criterion_4_fee_cache_hit_ratio(events: list[dict]) -> tuple[bool, str]:
    """Fee cache hit ratio > 95%."""
    ratios: list[float] = []

    for e in events:
        if e.get("event_type") != "STATUS_REPORT":
            continue
        ratio = e.get("fee_cache_hit_ratio")
        if ratio is not None:
            ratios.append(float(ratio))

    if not ratios:
        return False, "No STATUS_REPORT events with fee_cache_hit_ratio found"

    avg_ratio = sum(ratios) / len(ratios) * 100  # convert to percentage if 0–1
    if avg_ratio <= 1.0:
        avg_ratio *= 100  # already a percentage if already > 1

    passed = avg_ratio > 95.0
    detail = f"Average fee cache hit ratio: {avg_ratio:.1f}% (threshold: >95%)"
    return passed, detail


def _criterion_5_zero_inventory_halts_first_7_days(events: list[dict]) -> tuple[bool, str]:
    """Zero inventory halt events in the first 7 days."""
    halt_events = [e for e in events if e.get("event_type") == "INVENTORY_HALT"]
    if not halt_events:
        return True, "No INVENTORY_HALT events"

    # Find the first event timestamp to establish day-0
    all_ts = [_ts(e) for e in events if _ts(e) is not None]
    if not all_ts:
        return False, "Cannot determine start date — no timestamps found"
    start = min(all_ts)

    early_halts = [
        e for e in halt_events
        if _ts(e) is not None and (_ts(e) - start).days < 7  # type: ignore[operator]
    ]
    passed = len(early_halts) == 0
    detail = (
        f"Inventory halts in first 7 days: {len(early_halts)} "
        f"(total halts: {len(halt_events)})"
    )
    return passed, detail


def _criterion_6_resolution_watchlist(events: list[dict]) -> tuple[bool, str]:
    """Resolution watchlist correctly flags > 95% of resolved markets."""
    resolved = [e for e in events if e.get("event_type") == "MARKET_RESOLVED"]
    if not resolved:
        return False, "No MARKET_RESOLVED events found"

    flagged = sum(1 for e in resolved if e.get("flagged") is True)
    ratio = flagged / len(resolved) * 100
    passed = ratio > 95.0
    detail = (
        f"Resolution watchlist: {flagged}/{len(resolved)} flagged ({ratio:.1f}%) "
        f"(threshold: >95%)"
    )
    return passed, detail


def _criterion_7_auto_redemption(events: list[dict]) -> tuple[bool, str]:
    """Auto-redemption simulation: ≥ 3 markets simulated."""
    redemptions = {
        e.get("condition_id")
        for e in events
        if e.get("event_type") == "REDEMPTION_SUCCESS"
        and e.get("condition_id")
    }
    passed = len(redemptions) >= 3
    detail = f"Auto-redemptions simulated: {len(redemptions)} (threshold: ≥3)"
    return passed, detail


def _criterion_8_order_scoring(events: list[dict]) -> tuple[bool, str]:
    """Order scoring trackable for ≥ 5 reward-eligible markets."""
    scored_tokens = {
        e.get("token_id")
        for e in events
        if e.get("event_type") == "ORDER_SCORED"
        and e.get("token_id")
    }
    passed = len(scored_tokens) >= 5
    detail = f"Reward-eligible markets scored: {len(scored_tokens)} (threshold: ≥5)"
    return passed, detail


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-dir",
        default="./logs",
        help="Directory containing JSON log files (default: ./logs)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Number of trading days to evaluate (default: 14)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=2000.0,
        help="Starting capital in USD for drawdown calculation (default: 2000)",
    )
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    if not log_dir.exists():
        log.error("Log directory not found: %s", log_dir)
        sys.exit(1)

    events = parse_log_events(log_dir)
    if not events:
        log.error("No events parsed from log directory: %s", log_dir)
        sys.exit(1)

    log.info("\n=== Paper Trading Readiness Report ===\n")

    criteria = [
        ("1", "[SIMULATED] P&L positive ≥10/14 days",
         lambda: _criterion_1_pnl_positive_10_of_14(events, args.days)),
        ("2", "[SIMULATED] Max drawdown ≤5% of capital",
         lambda: _criterion_2_max_drawdown(events, args.capital)),
        ("3", "Trade count average >100/day",
         lambda: _criterion_3_trade_count(events)),
        ("4", "Fee cache hit ratio >95%",
         lambda: _criterion_4_fee_cache_hit_ratio(events)),
        ("5", "Zero inventory halts in first 7 days",
         lambda: _criterion_5_zero_inventory_halts_first_7_days(events)),
        ("6", "Resolution watchlist flags >95% of resolved markets",
         lambda: _criterion_6_resolution_watchlist(events)),
        ("7", "Auto-redemption: ≥3 markets simulated",
         lambda: _criterion_7_auto_redemption(events)),
        ("8", "Order scoring trackable for ≥5 reward-eligible markets",
         lambda: _criterion_8_order_scoring(events)),
    ]

    results: list[tuple[str, str, bool, str]] = []
    for num, name, fn in criteria:
        try:
            passed, detail = fn()
        except Exception as exc:
            passed, detail = False, f"Evaluation error: {exc}"
        status = "PASS" if passed else "FAIL"
        results.append((num, name, passed, detail))
        log.info("Criterion %s [%s]: %s\n  → %s", num, status, name, detail)

    total = len(results)
    passed_count = sum(1 for _, _, p, _ in results if p)

    log.info("\n=== Summary: %d/%d criteria passed ===", passed_count, total)

    # Warn about simulated criteria
    log.warning(
        "\n⚠️  IMPORTANT: Criteria 1 and 2 use SIMULATED fills.\n"
        "   They confirm the state machine runs — NOT actual profitability.\n"
        "   The live markout gate (§11.4 Step 3) is the only valid\n"
        "   Strategy A readiness signal before deploying capital.\n"
    )

    if passed_count == total:
        log.info("PASS — all %d criteria met. Proceed to shadow_run.py.", total)
        sys.exit(0)
    else:
        failed = [f"#{n}" for n, _, p, _ in results if not p]
        log.error("FAIL — criteria not met: %s", ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
