"""
Bot process entry point.

Usage:
    python -m bot

Uses uvloop on Linux/macOS for maximum asyncio throughput.
Falls back to the default asyncio event loop on Windows.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

log = logging.getLogger(__name__)


async def _main() -> None:
    from core.orchestrator import Orchestrator

    orch = Orchestrator()

    loop = asyncio.get_running_loop()

    def _handle_signal() -> None:
        log.info("Received shutdown signal — stopping")
        asyncio.create_task(orch.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        await orch.start()
    except Exception:
        log.exception("Orchestrator raised an unhandled exception — shutting down")
        await orch.stop()
        sys.exit(1)


if __name__ == "__main__":
    try:
        import uvloop  # type: ignore[import]
        asyncio.run(_main(), loop_factory=uvloop.new_event_loop)
    except ImportError:
        # Windows or uvloop not installed — fall back to default event loop
        asyncio.run(_main())
