"""
Builder Relayer client and EOA failover logic.

FR-204: Production uses Gnosis Safe (sig type 2) via Builder Relayer.
FR-205: Gnosis Safe proxy is deployed on first run and cached in Redis.
FR-216: If Relayer is unreachable for EOA_FALLBACK_TIMEOUT_S seconds,
        switch to direct EOA execution, alert, and revert on recovery.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from config.settings import Settings

log = logging.getLogger(__name__)

RELAY_HOST = "https://relay.polymarket.com"

# Alert name constants — replaced with AlertEvent enum when alerts/ is built in Step 11
_ALERT_FAILOVER_ACTIVATED = "RELAYER_FAILOVER_ACTIVATED"
_ALERT_RECOVERED = "RELAYER_RECOVERED"


class RelayClient:
    """Async HTTP client wrapping the Builder Relayer API (§8.2)."""

    def __init__(self, settings: Settings, host: str = RELAY_HOST) -> None:
        self._host = host.rstrip("/")
        self._settings = settings

    def _headers(self) -> dict[str, str]:
        return {
            "POLY-API-KEY": self._settings.BUILDER_API_KEY,
            "POLY-SECRET": self._settings.BUILDER_SECRET,
            "POLY-PASSPHRASE": self._settings.BUILDER_PASSPHRASE,
        }

    async def get_deployed(self) -> str | None:
        """Return the Gnosis Safe address if already deployed, else None."""
        async with httpx.AsyncClient() as http:
            try:
                r = await http.get(
                    f"{self._host}/wallet/deployed",
                    headers=self._headers(),
                    timeout=10.0,
                )
                if r.status_code == 200:
                    return r.json().get("safe_address")
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                log.warning("relay get_deployed failed: %s", exc)
        return None

    async def deploy(self) -> str:
        """Deploy the Gnosis Safe proxy wallet. Returns the Safe address."""
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{self._host}/wallet/deploy",
                headers=self._headers(),
                timeout=30.0,
            )
            r.raise_for_status()
            address: str = r.json()["safe_address"]
            return address

    async def execute(self, txs: list[dict]) -> dict:
        """Submit gasless transactions via the Builder Relayer."""
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{self._host}/wallet/execute",
                json={"transactions": txs},
                headers=self._headers(),
                timeout=30.0,
            )
            r.raise_for_status()
            return r.json()


async def get_or_deploy_safe(
    settings: Settings,
    redis_client: Any,
    relay_client: RelayClient | None = None,
) -> str:
    """Return the Gnosis Safe address, deploying it on first run.

    1. Check Redis cache (key: 'wallet:safe_address').
    2. If absent, call relay_client.get_deployed().
    3. If not deployed, call relay_client.deploy().
    4. Cache address in Redis and return it.

    FR-205: safe address is cached so deploy() is called only once.
    """
    if relay_client is None:
        relay_client = RelayClient(settings)

    cached = await redis_client.get("wallet:safe_address")
    if cached:
        return cached if isinstance(cached, str) else cached.decode()

    address = await relay_client.get_deployed()

    if address is None:
        log.info("Gnosis Safe not deployed — deploying now")
        address = await relay_client.deploy()
        log.info("Gnosis Safe deployed at %s", address)

    await redis_client.set("wallet:safe_address", address)
    return address


@dataclass
class FailoverState:
    """Mutable state for submit_with_failover.

    Pass an instance explicitly in production (owned by the Orchestrator)
    and inject a fresh instance in each unit test.
    """
    relayer_down_since: float | None = None
    eoa_active: bool = False


async def submit_with_failover(
    order: Any,
    relayer_client: RelayClient,
    eoa_client: Any,
    settings: Settings,
    alerts: Any,
    _state: FailoverState | None = None,
) -> Any:
    """Submit an order via the Builder Relayer with automatic EOA failover.

    FR-216 behaviour:
    - Attempt Relayer submission on every call.
    - Track consecutive failures. If the Relayer has been unreachable for
      at least EOA_FALLBACK_TIMEOUT_S seconds, activate EOA mode and alert.
    - While EOA is active, attempt a Relayer recovery probe on each call.
      On success, deactivate EOA mode, clear the outage timer, and alert.
    - Suspend submissions if POL balance is insufficient for EOA gas
      (balance check responsibility lies with the caller/Orchestrator).
    """
    if _state is None:
        _state = _module_state

    # ── Recovery probe when EOA is already active ─────────────────────────
    if _state.eoa_active:
        try:
            result = await relayer_client.execute([order])
            _state.eoa_active = False
            _state.relayer_down_since = None
            await alerts.send(_ALERT_RECOVERED)
            log.info("Builder Relayer recovered — reverted to Relayer execution")
            return result
        except (httpx.ConnectError, httpx.TimeoutException):
            # Still unreachable — stay on EOA
            return await eoa_client.create_and_post_order(order)

    # ── Normal path: try Relayer first ────────────────────────────────────
    try:
        result = await relayer_client.execute([order])
        _state.relayer_down_since = None  # clear any partial outage window
        return result
    except (httpx.ConnectError, httpx.TimeoutException):
        now = time.monotonic()
        if _state.relayer_down_since is None:
            _state.relayer_down_since = now

        elapsed = now - _state.relayer_down_since
        if elapsed >= settings.EOA_FALLBACK_TIMEOUT_S:
            _state.eoa_active = True
            await alerts.send(_ALERT_FAILOVER_ACTIVATED)
            log.warning(
                "Builder Relayer unreachable for %.1fs — activating EOA failover",
                elapsed,
            )
            return await eoa_client.create_and_post_order(order)

        # Outage window not yet reached — propagate to let caller decide
        raise


# Module-level singleton used when no _state is injected (production path).
# Tests must pass their own FailoverState() to avoid cross-test pollution.
_module_state = FailoverState()
