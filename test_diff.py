"""Offline tests for the snapshot diff, ticker normalization, and state persistence.
Run: python test_diff.py"""
import os
import tempfile

import config
from mirror_bot import (diff_snapshots, handle_close, handle_open, handle_resize,
                        load_state, normalize_ticker, save_state)


def trade(tid, size=100.0):
    return {"id": tid, "ticker": "BTC/USDT", "direction": "long", "position_size": size}


class StubClient:
    """Records orders; prices every coin at $100 so deltas clear MIN_NOTIONAL_USD."""

    def __init__(self, positions=None):
        self.orders = []
        self.positions = positions or {}  # what the fake exchange already holds

    def is_listed(self, coin):
        return True

    def open_positions(self):
        return dict(self.positions)

    def round_size(self, coin, sz):
        return round(sz, 4)

    def mid(self, coin):
        return 100.0

    def open_position(self, coin, is_buy, margin_usd, leverage):
        self.orders.append(("open", coin))
        return 1.0

    def place_tpsl(self, coin, position_is_buy, size, tp_px=None, sl_px=None):
        return []

    def increase_position(self, coin, is_buy, size):
        self.orders.append(("increase", coin, size))

    def close_position(self, coin, size=None):
        self.orders.append(("close", coin, size))

    def cancel_orders(self, coin, oids):
        self.orders.extend(("cancel", coin, oid) for oid in oids)


def mirrored_entry(hl_size=100.0, target_size=100.0):
    entry = {"coin": "BTC", "is_buy": True, "mirrored": True,
             "hl_size": hl_size, "leverage": 3, "tpsl_oids": []}
    if target_size is not None:
        entry["target_size"] = target_size
    return entry


def test_no_changes():
    snap = {"1": trade("1")}
    assert diff_snapshots(snap, snap) == ([], [], [])


def test_open():
    opens, closes, resizes = diff_snapshots({"1": trade("1")}, {"1": trade("1"), "2": trade("2")})
    assert [t["id"] for t in opens] == ["2"] and not closes and not resizes


def test_close():
    opens, closes, resizes = diff_snapshots({"1": trade("1"), "2": trade("2")}, {"1": trade("1")})
    assert [t["id"] for t in closes] == ["2"] and not opens and not resizes


def test_resize():
    opens, closes, resizes = diff_snapshots({"1": trade("1", 100.0)}, {"1": trade("1", 250.0)})
    assert not opens and not closes
    assert len(resizes) == 1
    old, new = resizes[0]
    assert old["position_size"] == 100.0 and new["position_size"] == 250.0


def test_open_and_close_same_poll():
    opens, closes, _ = diff_snapshots({"1": trade("1")}, {"2": trade("2")})
    assert [t["id"] for t in opens] == ["2"] and [t["id"] for t in closes] == ["1"]


def test_startup_mirrors_existing():
    # Empty snapshot at startup => every open target position is an "open" event
    opens, closes, resizes = diff_snapshots({}, {"1": trade("1"), "2": trade("2")})
    assert len(opens) == 2 and not closes and not resizes


def test_state_roundtrip():
    orig = config.OPEN_POSITIONS_PATH
    config.OPEN_POSITIONS_PATH = os.path.join(tempfile.gettempdir(), "test_open_positions.json")
    try:
        state = {"snapshot": {"1": trade("1")},
                 "mirrored": {"1": {"coin": "BTC", "is_buy": True, "mirrored": True,
                                    "hl_size": 0.5, "leverage": 3, "tpsl_oids": [42]}}}
        save_state(state)
        assert load_state() == state
    finally:
        os.remove(config.OPEN_POSITIONS_PATH)
        config.OPEN_POSITIONS_PATH = orig


def test_open_already_tracked_is_skipped():
    state = {"mirrored": {"1": {"coin": "BTC", "is_buy": True, "mirrored": True,
                                "hl_size": 0.5, "leverage": 3, "tpsl_oids": []}}}
    # client=None would blow up if handle_open placed an order instead of skipping
    handle_open(None, state, trade("1"))
    assert state["mirrored"]["1"]["hl_size"] == 0.5


def test_open_adopts_existing_same_direction_position():
    # Exchange already holds BTC long (lost state, manual trade, ...): adopt it
    # instead of doubling up
    client = StubClient(positions={"BTC": 0.5})
    state = {"mirrored": {}}
    handle_open(client, state, trade("1"))  # trade() is long
    assert not client.orders
    entry = state["mirrored"]["1"]
    assert entry["mirrored"] is True and entry["hl_size"] == 0.5


def test_open_skips_opposite_direction_position():
    client = StubClient(positions={"BTC": -0.5})  # short, trade is long
    state = {"mirrored": {}}
    handle_open(client, state, trade("1"))
    assert not client.orders
    assert state["mirrored"]["1"]["mirrored"] is False


def test_open_without_collision_mirrors():
    client = StubClient()
    state = {"mirrored": {}}
    handle_open(client, state, trade("1"))
    assert client.orders == [("open", "BTC")]
    assert state["mirrored"]["1"]["mirrored"] is True


def test_close_already_closed_on_exchange_cleans_up():
    # Position vanished from the exchange (TP/SL fired, manual close): cancel
    # leftover triggers and drop the entry, but send no close order
    orig = config.DRY_RUN
    config.DRY_RUN = False
    try:
        client = StubClient()  # fake exchange holds nothing
        entry = mirrored_entry()
        entry["tpsl_oids"] = [42]
        state = {"mirrored": {"1": entry}}
        handle_close(client, state, trade("1"))
        assert client.orders == [("cancel", "BTC", 42)]
        assert "1" not in state["mirrored"]
    finally:
        config.DRY_RUN = orig


def test_close_with_position_on_exchange_closes():
    orig = config.DRY_RUN
    config.DRY_RUN = False
    try:
        client = StubClient(positions={"BTC": 100.0})
        state = {"mirrored": {"1": mirrored_entry()}}
        handle_close(client, state, trade("1"))
        assert ("close", "BTC", 100.0) in client.orders
        assert "1" not in state["mirrored"]
    finally:
        config.DRY_RUN = orig


def test_resize_below_threshold_is_noise():
    client = StubClient()
    state = {"mirrored": {"1": mirrored_entry()}}
    handle_resize(client, state, trade("1", 100.0), trade("1", 102.0))  # +2% < 5%
    assert not client.orders
    assert state["mirrored"]["1"]["target_size"] == 100.0
    assert state["mirrored"]["1"]["hl_size"] == 100.0


def test_resize_above_threshold_executes():
    client = StubClient()
    state = {"mirrored": {"1": mirrored_entry()}}
    handle_resize(client, state, trade("1", 100.0), trade("1", 150.0))  # +50%
    assert client.orders == [("increase", "BTC", 50.0)]
    assert state["mirrored"]["1"]["target_size"] == 150.0
    assert state["mirrored"]["1"]["hl_size"] == 150.0


def test_resize_gradual_changes_accumulate():
    # Each step is below the threshold vs the previous poll, but they add up
    # past it vs the last-mirrored baseline
    client = StubClient()
    state = {"mirrored": {"1": mirrored_entry()}}
    sizes = [100.0, 97.0, 94.0]
    for old, new in zip(sizes, sizes[1:]):
        handle_resize(client, state, trade("1", old), trade("1", new))
    assert client.orders == [("close", "BTC", 6.0)]
    assert state["mirrored"]["1"]["target_size"] == 94.0


def test_resize_legacy_entry_without_target_size():
    # State saved before target_size existed: baseline falls back to the
    # previous snapshot's size and gets stored on first touch
    client = StubClient()
    state = {"mirrored": {"1": mirrored_entry(target_size=None)}}
    handle_resize(client, state, trade("1", 100.0), trade("1", 101.0))
    assert not client.orders
    assert state["mirrored"]["1"]["target_size"] == 100.0


def test_normalize_ticker():
    assert normalize_ticker("BTC/USDT") == "BTC"
    assert normalize_ticker("eth-usd") == "ETH"
    assert normalize_ticker("SOLUSDT") == "SOL"
    assert normalize_ticker("DOGE") == "DOGE"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok: {name}")
    print("all tests passed")
