"""Base strategy ABC."""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.control.capability_enricher import MarketCapabilityModel
from core.execution.book_state import BookStateStore
from core.execution.types import Signal
from fees.cache import FeeRateCache
from inventory.manager import InventoryState


class BaseStrategy(ABC):
    strategy_id: str          # 'A', 'B', or 'C'
    enabled: bool
    max_exposure: float
    kill_switch_active: bool = False

    @abstractmethod
    async def evaluate(
        self,
        market: MarketCapabilityModel,
        book: BookStateStore,
        inventory: InventoryState,
        fee_cache: FeeRateCache,
    ) -> list[Signal]: ...

    def kill(self) -> None:
        self.kill_switch_active = True
