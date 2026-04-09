"""
Shared event and intent types for the execution plane.

All execution-plane modules import from here. Do not redefine these types
elsewhere — Steps 6 and 7 both import Signal and OrderIntent from this file
to avoid circular dependencies.
"""

from dataclasses import dataclass


@dataclass
class PriceLevel:
    price: float
    size: float


@dataclass
class BookEvent:
    token_id: str
    bids: list[PriceLevel]
    asks: list[PriceLevel]
    timestamp: float


@dataclass
class FillEvent:
    order_id: str
    token_id: str
    market_id: str
    side: str           # 'BUY' | 'SELL'
    price: float
    size: float
    maker_taker: str    # 'MAKER' | 'TAKER' — from User channel, not inferred
    strategy: str
    fill_timestamp: float


@dataclass
class CancelEvent:
    order_id: str
    token_id: str


@dataclass
class OrderAckEvent:
    order_id: str
    token_id: str


@dataclass
class Signal:
    """Intermediate signal produced by a strategy's evaluate() method,
    before the Quote Engine has applied reward constraints. Not yet
    validated by the Order Diff or Risk Gate."""
    token_id: str
    side: str
    price: float
    size: float
    time_in_force: str   # 'GTC' | 'GTD'
    post_only: bool
    expiration: int | None
    strategy: str
    fee_rate_bps: int
    neg_risk: bool
    tick_size: float


@dataclass
class OrderIntent:
    """Desired order state passed from QuoteEngine to OrderDiff.
    Identical fields to Signal; separate type so the diff layer has
    a distinct, typed input."""
    token_id: str
    side: str
    price: float
    size: float
    time_in_force: str   # 'GTC' | 'GTD'
    post_only: bool      # True for Strategy A and C; False for Strategy B
    expiration: int | None  # required when time_in_force == 'GTD'
    strategy: str
    fee_rate_bps: int    # normalised from 'base_fee' at fetch boundary
    neg_risk: bool
    tick_size: float
