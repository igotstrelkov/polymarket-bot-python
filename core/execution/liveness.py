"""
Three independent liveness asyncio tasks (FR-501).

Loop 1 — ORDER SAFETY: POST heartbeat every HEARTBEAT_INTERVAL_MS.
  The platform cancels ALL open orders if no heartbeat is received within 10s.
  2 consecutive missed acks → session dead → signal reconnect.
  Also monitors server-time drift: >5s logs warning, >30s sends alert.

Loop 2 — WS KEEPALIVE: Market + User channels.
  Application-level message every 10s. Format is ambiguous between docs:
  - WebSocket overview docs: literal string 'PING' / 'PONG'
  - Market/User channel reference docs: 'Ping {}' / 'Pong {}'
  A WsHeartbeatAdapter abstracts the format — confirm during Step 14 smoke test.

Loop 3 — SPORTS CHANNEL: Conditional (only when sports_ws is not None).
  Direction reversed: server sends 'ping' every 5s, client replies 'pong' within 10s.
  This format is unambiguous in the sports channel docs.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_HEARTBEAT_MISSED_LIMIT = 2
_WS_KEEPALIVE_INTERVAL_S = 10.0
_SPORTS_PONG_TIMEOUT_S = 10.0
_CLOCK_DRIFT_WARN_S = 5
_CLOCK_DRIFT_ALERT_S = 30


# ── WsHeartbeatAdapter ────────────────────────────────────────────────────────

@dataclass
class WsHeartbeatAdapter:
    """Abstracts the ping/pong format so it can be confirmed during smoke test.

    ping_msg: message to send (e.g. 'PING' or 'Ping {}')
    pong_msg: expected response (e.g. 'PONG' or 'Pong {}')
    """
    ping_msg: str
    pong_msg: str
    healthy: bool = field(default=True, init=False)

    def is_pong(self, msg: str) -> bool:
        return msg.strip() == self.pong_msg

    async def send_ping(self, ws) -> None:
        await ws.send(self.ping_msg)
        self.healthy = False  # cleared when pong received

    def on_pong(self) -> None:
        self.healthy = True


# ── Loop 1: Order-safety heartbeat ───────────────────────────────────────────

async def order_safety_heartbeat_loop(clob_client, settings, alerts) -> None:
    """POST heartbeat every HEARTBEAT_INTERVAL_MS (5s default).

    FR-501: Platform cancels ALL open orders if heartbeat not received within 10s.
    On 2 consecutive missed acks: declare session dead, signal reconnect.
    Also checks server-time for clock drift; alerts on drift > 30s.
    """
    interval_s = settings.HEARTBEAT_INTERVAL_MS / 1000.0
    consecutive_misses = 0

    while True:
        try:
            await clob_client.post_tick()
            consecutive_misses = 0
            log.debug("Order-safety heartbeat sent")
        except Exception as exc:
            consecutive_misses += 1
            log.warning(
                "Heartbeat missed (%d/%d): %s",
                consecutive_misses, _HEARTBEAT_MISSED_LIMIT, exc,
            )
            if consecutive_misses >= _HEARTBEAT_MISSED_LIMIT:
                log.error("Heartbeat: %d consecutive misses — session dead", consecutive_misses)
                await alerts.send("HEARTBEAT_SESSION_DEAD")
                # Signal reconnect by raising — caller's task group handles restart
                raise RuntimeError("Order-safety heartbeat: session declared dead") from exc

        # Clock drift check
        try:
            server_ts = await clob_client.get_server_time()
            drift = abs(time.time() - server_ts)
            if drift > _CLOCK_DRIFT_ALERT_S:
                log.error("Clock drift %.1fs exceeds alert threshold — GTD expiry at risk", drift)
                await alerts.send("CLOCK_DRIFT_ALERT")
            elif drift > _CLOCK_DRIFT_WARN_S:
                log.warning("Clock drift %.1fs detected", drift)
        except Exception as exc:
            log.debug("Server-time check failed: %s", exc)

        await asyncio.sleep(interval_s)


# ── Loop 2: WebSocket keepalive ───────────────────────────────────────────────

async def market_user_ws_heartbeat_loop(
    market_ws,
    user_ws,
    ping_msg: str = "PING",
    pong_msg: str = "PONG",
) -> None:
    """Send application-level ping to market and user channels every 10s.

    Format is resolved during Step 14 smoke test. Two named formats supported:
    - 'PING' / 'PONG'  (WebSocket overview docs, FR-501)
    - 'Ping {}' / 'Pong {}'  (market/user channel reference docs)

    Pass ping_msg/pong_msg at startup; freeze whichever the server acknowledges.
    """
    market_adapter = WsHeartbeatAdapter(ping_msg=ping_msg, pong_msg=pong_msg)
    user_adapter = WsHeartbeatAdapter(ping_msg=ping_msg, pong_msg=pong_msg)

    while True:
        await asyncio.sleep(_WS_KEEPALIVE_INTERVAL_S)
        try:
            await market_adapter.send_ping(market_ws)
        except Exception as exc:
            log.warning("Market WS ping failed: %s", exc)
        try:
            await user_adapter.send_ping(user_ws)
        except Exception as exc:
            log.warning("User WS ping failed: %s", exc)


# ── Loop 3: Sports channel heartbeat ─────────────────────────────────────────

async def sports_ws_heartbeat_loop(sports_ws) -> None:
    """Reply 'pong' within 10s of each server 'ping' on the sports channel.

    Only instantiate when sports_ws is not None (FR-501, FR-114).
    Direction reversed from Loops 1+2: server initiates, client replies.
    """
    if sports_ws is None:
        log.debug("Sports WS heartbeat loop not started (sports_ws=None)")
        return

    while True:
        try:
            msg = await asyncio.wait_for(sports_ws.recv(), timeout=_SPORTS_PONG_TIMEOUT_S)
            if isinstance(msg, str) and msg.strip() == "ping":
                await sports_ws.send("pong")
                log.debug("Sports WS: replied pong")
        except asyncio.TimeoutError:
            log.warning("Sports WS: no ping received within %.1fs", _SPORTS_PONG_TIMEOUT_S)
        except Exception as exc:
            log.warning("Sports WS heartbeat error: %s", exc)
            raise
