"""
Parameter Service — live configuration distribution with full version history.

PRD §4.4.2:
  Stores and distributes configuration: max markets, quote widths, inventory
  limits, GTD durations, exposure budgets.  Supports live tuning without code
  changes.  Versions every config change for postmortems.

Design:
  - Thread-safe in-process store (asyncio single-threaded — no locking needed)
  - Every set() increments a monotonic version counter and appends a
    ConfigChange record so postmortems can replay the config timeline
  - get() returns the current value; snapshot() returns a copy of all params
  - get_history() returns the full change log, optionally filtered by key
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ConfigChange:
    """Immutable record of a single parameter change."""
    version: int
    timestamp: datetime
    key: str
    old_value: Any
    new_value: Any
    changed_by: str = "system"


class ParameterService:
    """In-process versioned parameter store.

    Usage:
        svc = ParameterService({"MM_MAX_MARKETS": 20, "MM_BASE_SPREAD": 0.04})
        svc.set("MM_BASE_SPREAD", 0.03, changed_by="operator")
        spread = svc.get("MM_BASE_SPREAD")          # 0.03
        history = svc.get_history("MM_BASE_SPREAD")  # [ConfigChange(...)]
    """

    def __init__(self, initial_params: dict[str, Any] | None = None) -> None:
        self._params: dict[str, Any] = dict(initial_params or {})
        self._version: int = 0
        self._history: list[ConfigChange] = []

    # ── Read API ──────────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Return the current value for *key*, or *default* if absent."""
        return self._params.get(key, default)

    def get_version(self) -> int:
        """Current monotonic version counter (increments on every set())."""
        return self._version

    def snapshot(self) -> dict[str, Any]:
        """Return a shallow copy of all current parameters."""
        return dict(self._params)

    def get_history(self, key: str | None = None) -> list[ConfigChange]:
        """Return the full change history, optionally filtered to *key*."""
        if key is None:
            return list(self._history)
        return [c for c in self._history if c.key == key]

    # ── Write API ─────────────────────────────────────────────────────────────

    def set(self, key: str, value: Any, changed_by: str = "system") -> int:
        """Update *key* to *value* and return the new version number.

        Every call appends a ConfigChange even if the value is unchanged
        (explicit set is explicit intent and should be auditable).
        """
        old_value = self._params.get(key)
        self._params[key] = value
        self._version += 1

        change = ConfigChange(
            version=self._version,
            timestamp=datetime.now(tz=timezone.utc),
            key=key,
            old_value=old_value,
            new_value=value,
            changed_by=changed_by,
        )
        self._history.append(change)

        log.info(
            "ParameterService: %s → %r (was %r) v%d by %s",
            key,
            value,
            old_value,
            self._version,
            changed_by,
        )
        return self._version

    def bulk_set(
        self,
        params: dict[str, Any],
        changed_by: str = "system",
    ) -> int:
        """Set multiple keys atomically (same timestamp, sequential versions).

        Returns the version number after the last change.
        """
        for key, value in params.items():
            self.set(key, value, changed_by=changed_by)
        return self._version
