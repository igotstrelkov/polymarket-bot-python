"""
Unit tests for core/control/parameter_service.py.

Covers: get/set, version increments, history (full and per-key),
bulk_set, snapshot isolation, and None defaults.
"""

from __future__ import annotations

import pytest

from core.control.parameter_service import ConfigChange, ParameterService


# ── Basic get / set ───────────────────────────────────────────────────────────

def test_get_returns_initial_value():
    svc = ParameterService({"MM_BASE_SPREAD": 0.04})
    assert svc.get("MM_BASE_SPREAD") == 0.04


def test_get_returns_default_for_missing_key():
    svc = ParameterService()
    assert svc.get("MISSING", 42) == 42


def test_get_returns_none_by_default_for_missing_key():
    svc = ParameterService()
    assert svc.get("ABSENT") is None


def test_set_updates_value():
    svc = ParameterService({"MM_BASE_SPREAD": 0.04})
    svc.set("MM_BASE_SPREAD", 0.03)
    assert svc.get("MM_BASE_SPREAD") == 0.03


def test_set_creates_new_key():
    svc = ParameterService()
    svc.set("NEW_KEY", "hello")
    assert svc.get("NEW_KEY") == "hello"


# ── Version counter ───────────────────────────────────────────────────────────

def test_initial_version_is_zero():
    svc = ParameterService()
    assert svc.get_version() == 0


def test_version_increments_on_each_set():
    svc = ParameterService()
    svc.set("a", 1)
    assert svc.get_version() == 1
    svc.set("b", 2)
    assert svc.get_version() == 2


def test_set_returns_new_version():
    svc = ParameterService()
    v = svc.set("x", 99)
    assert v == 1


# ── History ───────────────────────────────────────────────────────────────────

def test_get_history_empty_initially():
    svc = ParameterService({"a": 1})
    assert svc.get_history() == []


def test_get_history_records_change():
    svc = ParameterService({"a": 1})
    svc.set("a", 2, changed_by="operator")
    history = svc.get_history()
    assert len(history) == 1
    change = history[0]
    assert change.key == "a"
    assert change.old_value == 1
    assert change.new_value == 2
    assert change.changed_by == "operator"
    assert change.version == 1


def test_get_history_filtered_by_key():
    svc = ParameterService()
    svc.set("a", 1)
    svc.set("b", 2)
    svc.set("a", 3)
    history_a = svc.get_history("a")
    assert len(history_a) == 2
    assert all(c.key == "a" for c in history_a)


def test_get_history_filtered_key_not_in_history_returns_empty():
    svc = ParameterService()
    svc.set("a", 1)
    assert svc.get_history("b") == []


def test_get_history_returns_copy():
    """Mutating the returned list must not affect the internal history."""
    svc = ParameterService()
    svc.set("a", 1)
    history = svc.get_history()
    history.clear()
    assert len(svc.get_history()) == 1


def test_history_version_matches_service_version():
    svc = ParameterService()
    svc.set("x", 10)
    svc.set("y", 20)
    versions = [c.version for c in svc.get_history()]
    assert versions == [1, 2]


def test_history_records_none_old_value_for_new_key():
    svc = ParameterService()
    svc.set("fresh", 5)
    change = svc.get_history()[0]
    assert change.old_value is None


# ── bulk_set ──────────────────────────────────────────────────────────────────

def test_bulk_set_updates_all_keys():
    svc = ParameterService({"a": 1, "b": 2})
    svc.bulk_set({"a": 10, "b": 20})
    assert svc.get("a") == 10
    assert svc.get("b") == 20


def test_bulk_set_increments_version_per_key():
    svc = ParameterService()
    final_version = svc.bulk_set({"a": 1, "b": 2, "c": 3})
    assert final_version == 3


def test_bulk_set_returns_final_version():
    svc = ParameterService()
    v = svc.bulk_set({"x": 1, "y": 2})
    assert v == svc.get_version()


# ── snapshot ──────────────────────────────────────────────────────────────────

def test_snapshot_returns_all_params():
    svc = ParameterService({"a": 1, "b": 2})
    snap = svc.snapshot()
    assert snap == {"a": 1, "b": 2}


def test_snapshot_is_isolated_from_changes():
    """Mutating the snapshot must not change the service, and vice versa."""
    svc = ParameterService({"a": 1})
    snap = svc.snapshot()
    snap["a"] = 999
    assert svc.get("a") == 1


def test_snapshot_reflects_latest_state():
    svc = ParameterService({"a": 1})
    svc.set("a", 2)
    svc.set("b", 3)
    snap = svc.snapshot()
    assert snap == {"a": 2, "b": 3}


# ── ConfigChange dataclass ────────────────────────────────────────────────────

def test_config_change_has_timestamp():
    svc = ParameterService()
    svc.set("t", 1)
    change = svc.get_history()[0]
    assert change.timestamp is not None


def test_config_change_default_changed_by_is_system():
    svc = ParameterService()
    svc.set("k", 1)
    assert svc.get_history()[0].changed_by == "system"
