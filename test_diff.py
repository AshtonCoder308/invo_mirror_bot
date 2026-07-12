"""Offline tests for the snapshot diff, ticker normalization, and state persistence.
Run: python test_diff.py"""
import os
import tempfile

import config
from hl_client import OrderError
from mirror_bot import (adjust_for_foreign, allocate_margin, check_pending_entries,
                        confirm_foreign_positions, diff_snapshots, handle_close,
                        handle_fill, handle_open, load_state, normalize_ticker,
                        retry_unmirrored, save_state)


def trade(tid, size=100.0, direction="long", entry_price=100.0):
    return {"id": tid, "ticker": "BTC/USDT", "direction": direction,
            "position_size": size, "entry_price": entry_price}


class StubClient:
    """Records orders; prices every coin at $100 so sizes clear MIN_NOTIONAL_USD."""

    def __init__(self, positions=None, account=(100_000.0, 0.0, 0.0), breakdown=None):
        self.orders = []
        self.positions = positions or {}  # what the fake exchange already holds
        self.resting = set()  # oids of orders still resting on the fake exchange
        self.account = account  # (available, total_notional, margin_used)
        self.breakdown = breakdown or {}  # coin -> {margin, notional, upnl}

    def is_listed(self, coin):
        return True

    def open_positions(self):
        return dict(self.positions)

    def open_orders(self):
        return set(self.resting)

    def margin_summary(self):
        return self.account

    def position_breakdown(self):
        return dict(self.breakdown)

    def round_size(self, coin, sz):
        return round(sz, 4)

    def mid(self, coin):
        return 100.0

    def open_position(self, coin, is_buy, margin_usd, leverage):
        self.orders.append(("open", coin))
        return 1.0

    def place_entry_trigger(self, coin, is_buy, size, trigger_px, leverage):
        self.orders.append(("entry_trigger", coin, size, trigger_px))
        return 7

    def place_tpsl(self, coin, position_is_buy, size, tp_px=None, sl_px=None):
        self.orders.append(("tpsl", coin, size))
        return []

    def close_position(self, coin, size=None):
        self.orders.append(("close", coin, size))

    def cancel_orders(self, coin, oids):
        self.orders.extend(("cancel", coin, oid) for oid in oids)


def mirrored_entry(hl_size=100.0):
    return {"coin": "BTC", "is_buy": True, "mirrored": True,
            "hl_size": hl_size, "leverage": 3, "tpsl_oids": [], "entry_oid": None}


def pending_entry(oid=7):
    """Entry whose trigger order rests on the exchange but hasn't filled."""
    return {"coin": "BTC", "is_buy": True, "mirrored": False, "hl_size": 0,
            "leverage": 3, "tpsl_oids": [], "entry_oid": oid,
            "tp_px": 120.0, "sl_px": 80.0}


class tmp_state_file:
    """Point save_state at a temp file so tests that reach it never clobber
    the real open_positions.json in the repo root."""

    def __enter__(self):
        self.orig = config.OPEN_POSITIONS_PATH
        config.OPEN_POSITIONS_PATH = os.path.join(tempfile.gettempdir(),
                                                  "test_open_positions.json")

    def __exit__(self, *exc):
        if os.path.exists(config.OPEN_POSITIONS_PATH):
            os.remove(config.OPEN_POSITIONS_PATH)
        config.OPEN_POSITIONS_PATH = self.orig


def test_no_changes():
    snap = {"1": trade("1")}
    assert diff_snapshots(snap, snap) == ([], [])


def test_open():
    opens, closes = diff_snapshots({"1": trade("1")}, {"1": trade("1"), "2": trade("2")})
    assert [t["id"] for t in opens] == ["2"] and not closes


def test_close():
    opens, closes = diff_snapshots({"1": trade("1"), "2": trade("2")}, {"1": trade("1")})
    assert [t["id"] for t in closes] == ["2"] and not opens


def test_resize_is_not_an_event():
    # Target size changes are deliberately ignored: no resizing at all
    opens, closes = diff_snapshots({"1": trade("1", 100.0)}, {"1": trade("1", 250.0)})
    assert not opens and not closes


def test_open_and_close_same_poll():
    opens, closes = diff_snapshots({"1": trade("1")}, {"2": trade("2")})
    assert [t["id"] for t in opens] == ["2"] and [t["id"] for t in closes] == ["1"]


def test_startup_mirrors_existing():
    # Empty snapshot at startup => every open target position is an "open" event
    opens, closes = diff_snapshots({}, {"1": trade("1"), "2": trade("2")})
    assert len(opens) == 2 and not closes


def test_state_roundtrip():
    with tmp_state_file():
        state = {"snapshot": {"1": trade("1")},
                 "mirrored": {"1": mirrored_entry(hl_size=0.5)}}
        save_state(state)
        assert load_state() == state


def test_open_already_tracked_is_skipped():
    state = {"mirrored": {"1": mirrored_entry(hl_size=0.5)}}
    # client=None would blow up if handle_open placed an order instead of skipping
    handle_open(None, state, trade("1"))
    assert state["mirrored"]["1"]["hl_size"] == 0.5


def test_open_skips_existing_same_direction_position():
    # Never touch a position the bot didn't open, even in the same direction:
    # no adoption, just warn and leave it alone
    client = StubClient(positions={"BTC": 0.5})
    state = {"mirrored": {}}
    handle_open(client, state, trade("1"))  # trade() is long
    assert not client.orders
    assert state["mirrored"]["1"]["mirrored"] is False


def test_open_skips_opposite_direction_position():
    client = StubClient(positions={"BTC": -0.5})
    state = {"mirrored": {}}
    handle_open(client, state, trade("1"))
    assert not client.orders
    assert state["mirrored"]["1"]["mirrored"] is False


def test_open_ignored_coin_skipped():
    orig = config.IGNORED_COINS
    config.IGNORED_COINS = {"BTC"}
    try:
        client = StubClient()
        state = {"mirrored": {}}
        handle_open(client, state, trade("1"))
        assert not client.orders
        assert state["mirrored"]["1"]["mirrored"] is False
    finally:
        config.IGNORED_COINS = orig


def test_open_long_places_trigger_below_entry():
    client = StubClient()
    state = {"mirrored": {}}
    handle_open(client, state, trade("1"))  # entry_price 100, long
    assert len(client.orders) == 1
    kind, coin, size, trigger_px = client.orders[0]
    assert kind == "entry_trigger" and coin == "BTC" and trigger_px == 99.5
    entry = state["mirrored"]["1"]
    # No position yet: mirrored only flips once the trigger fills
    assert entry["mirrored"] is False and entry["entry_oid"] == 7
    assert entry["hl_size"] == 0


def test_open_short_places_trigger_above_entry():
    client = StubClient()
    state = {"mirrored": {}}
    handle_open(client, state, trade("1", direction="short"))
    kind, coin, size, trigger_px = client.orders[0]
    # fp noise (100 * 1.005 != 100.5 exactly); the real client rounds the price
    assert kind == "entry_trigger" and abs(trigger_px - 100.5) < 1e-9
    assert state["mirrored"]["1"]["is_buy"] is False


def test_open_long_at_market_when_price_already_beyond_trigger():
    # Target long from 110, stub mid is 100: price is already below the 109.45
    # trigger, which the exchange would reject as triggering immediately -
    # take the better entry at market and place TP/SL right away
    client = StubClient()
    state = {"mirrored": {}}
    handle_open(client, state, trade("1", entry_price=110.0))
    assert client.orders == [("open", "BTC"), ("tpsl", "BTC", 1.0)]
    entry = state["mirrored"]["1"]
    assert entry["mirrored"] is True and entry["hl_size"] == 1.0
    assert entry["entry_oid"] is None


def test_open_short_at_market_when_price_already_beyond_trigger():
    # Target short from 90, stub mid is 100: already above the 90.45 trigger
    client = StubClient()
    state = {"mirrored": {}}
    handle_open(client, state, trade("1", direction="short", entry_price=90.0))
    assert client.orders == [("open", "BTC"), ("tpsl", "BTC", 1.0)]
    assert state["mirrored"]["1"]["mirrored"] is True


def test_retry_unmirrored_drops_stuck_entries_at_startup():
    with tmp_state_file():
        stuck = {"coin": "ETH", "is_buy": False, "mirrored": False, "hl_size": 0,
                 "leverage": 25, "tpsl_oids": [], "entry_oid": None,
                 "tp_px": None, "sl_px": None}
        state = {"snapshot": {"1": trade("1"), "2": trade("2"), "3": trade("3")},
                 "mirrored": {"1": stuck, "2": mirrored_entry(), "3": pending_entry(oid=7)}}
        retry_unmirrored(state)
        # Stuck entry replays as an open on the first poll; the mirrored
        # position and the resting trigger are untouched
        assert "1" not in state["mirrored"] and "1" not in state["snapshot"]
        assert "2" in state["mirrored"] and "2" in state["snapshot"]
        assert "3" in state["mirrored"] and "3" in state["snapshot"]


def test_open_rejected_by_risk_limits_not_retried():
    # Available balance ($5) implies a notional below the minimum: reject, but
    # still track the trade (mirrored=False) so it isn't retried every poll
    client = StubClient(account=(5.0, 0.0, 0.0))
    state = {"mirrored": {}}
    handle_open(client, state, trade("1"))
    assert not client.orders
    assert state["mirrored"]["1"]["mirrored"] is False


def test_fill_detection_waits_while_resting():
    client = StubClient()
    client.resting = {7}
    entry = pending_entry(oid=7)
    state = {"mirrored": {"1": entry}}
    check_pending_entries(client, state)
    assert entry["mirrored"] is False and entry["entry_oid"] == 7
    assert not client.orders


def test_fill_detection_promotes_filled_entry():
    with tmp_state_file():
        # oid 7 no longer resting and a long BTC position exists: it filled
        client = StubClient(positions={"BTC": 2.0})
        entry = pending_entry(oid=7)
        state = {"mirrored": {"1": entry}}
        check_pending_entries(client, state)
        # hl_size comes from the actual position, TP/SL now guard it
        assert entry["mirrored"] is True and entry["hl_size"] == 2.0
        assert entry["entry_oid"] is None
        assert client.orders == [("tpsl", "BTC", 2.0)]


def test_fill_detection_vanished_order_stays_unmirrored():
    with tmp_state_file():
        # oid gone but no position: cancelled/rejected externally - warn once,
        # never retry (entry_oid cleared so the sweep skips it next poll)
        client = StubClient()
        entry = pending_entry(oid=7)
        state = {"mirrored": {"1": entry}}
        check_pending_entries(client, state)
        assert entry["mirrored"] is False and entry["entry_oid"] is None
        assert not client.orders


def test_promotion_records_entry_when_tpsl_fails():
    # place_tpsl failing after the trigger filled must not orphan the
    # position: mirrored/hl_size are set before the TP/SL order goes out
    class FailingTPSL(StubClient):
        def place_tpsl(self, coin, position_is_buy, size, tp_px=None, sl_px=None):
            raise OrderError("trigger rejected")

    with tmp_state_file():
        client = FailingTPSL(positions={"BTC": 1.0})
        entry = pending_entry(oid=7)
        state = {"mirrored": {"1": entry}}
        check_pending_entries(client, state)  # catches the OrderError itself
        assert entry["mirrored"] is True and entry["hl_size"] == 1.0


def test_handle_fill_matching_oid_promotes():
    with tmp_state_file():
        client = StubClient(positions={"BTC": 2.0})
        entry = pending_entry(oid=7)
        state = {"mirrored": {"1": entry}}
        handle_fill(client, state, {"oid": 7, "coin": "BTC", "sz": "2.0"})
        assert entry["mirrored"] is True and entry["hl_size"] == 2.0
        assert entry["entry_oid"] is None
        assert client.orders == [("tpsl", "BTC", 2.0)]


def test_handle_fill_unknown_oid_does_nothing():
    # TP/SL executions and manual trades produce fills too; they're left to
    # the poll-time close/reconcile logic
    client = StubClient()
    entry = pending_entry(oid=7)
    state = {"mirrored": {"1": entry}}
    handle_fill(client, state, {"oid": 99, "coin": "ETH", "sz": "1.0"})
    assert entry["mirrored"] is False and entry["entry_oid"] == 7
    assert not client.orders


def test_close_while_trigger_resting_cancels_it():
    orig = config.DRY_RUN
    config.DRY_RUN = False
    try:
        client = StubClient()  # no position: the trigger never fired
        state = {"mirrored": {"1": pending_entry(oid=7)}}
        handle_close(client, state, trade("1"))
        assert client.orders == [("cancel", "BTC", 7)]
        assert "1" not in state["mirrored"]
    finally:
        config.DRY_RUN = orig


def test_close_after_trigger_fired_mid_poll_closes_position():
    orig = config.DRY_RUN
    config.DRY_RUN = False
    try:
        # Trigger fired between polls (position exists) but was never
        # promoted: the close must still flatten the position
        client = StubClient(positions={"BTC": 2.0})
        state = {"mirrored": {"1": pending_entry(oid=7)}}
        handle_close(client, state, trade("1"))
        assert ("cancel", "BTC", 7) in client.orders
        assert ("close", "BTC", 2.0) in client.orders
        assert "1" not in state["mirrored"]
    finally:
        config.DRY_RUN = orig


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


def test_allocate_margin_equal_slice():
    # Plenty of capital: each trade gets account_value / MAX_OPEN_TRADES
    assert allocate_margin(3000.0, 3000.0, 0.0, 1) == 3000.0 / config.MAX_OPEN_TRADES


def test_allocate_margin_uses_remaining_capital():
    # Free margin below the slice: invest whatever is left
    assert allocate_margin(3000.0, 400.0, 0.0, 1) == 400.0


def test_allocate_margin_rejects_when_no_capital():
    # What's left implies a notional below the exchange minimum
    assert allocate_margin(3000.0, 5.0, 0.0, 1) is None


def test_allocate_margin_caps_position_notional():
    orig = config.MAX_POSITION_NOTIONAL_USD
    config.MAX_POSITION_NOTIONAL_USD = 2000.0
    try:
        # 10x leverage would imply a 10k notional; the per-position cap clamps
        # it to 2k, i.e. 200 margin
        assert allocate_margin(3000.0, 3000.0, 0.0, 10) == 200.0
    finally:
        config.MAX_POSITION_NOTIONAL_USD = orig


def test_allocate_margin_caps_account_leverage():
    orig = config.MAX_ACCOUNT_LEVERAGE
    config.MAX_ACCOUNT_LEVERAGE = 2.0
    try:
        # Exposure headroom = 3000*2 - 5000 = 1000 notional
        assert allocate_margin(3000.0, 3000.0, 5000.0, 1) == 1000.0
        # No headroom left at all: reject
        assert allocate_margin(3000.0, 3000.0, 6000.0, 1) is None
    finally:
        config.MAX_ACCOUNT_LEVERAGE = orig


def test_adjust_for_foreign_capital_from_available():
    # Available balance 600 (exchange already held back both positions'
    # margin); the bot's own BTC margin (100) is added back so the per-trade
    # slice stays a share of all bot money. Foreign ETH costs nothing extra.
    breakdown = {"ETH": {"margin": 300.0, "notional": 900.0, "upnl": 50.0},
                 "BTC": {"margin": 100.0, "notional": 300.0, "upnl": 0.0}}
    bot_av, bot_free, bot_notional = adjust_for_foreign(
        600.0, 1200.0, breakdown, {"ETH"})
    assert bot_av == 700.0        # available + bot's own margin
    assert bot_free == 600.0      # spendable = the exchange's available balance
    assert bot_notional == 300.0  # foreign notional doesn't count against leverage


def test_adjust_for_foreign_upnl_irrelevant():
    # The hold behind the available balance already tracks the positions'
    # buckets; the uPnL figures themselves must not change the result
    for upnl in (50.0, -50.0):
        breakdown = {"ETH": {"margin": 300.0, "notional": 900.0, "upnl": upnl},
                     "BTC": {"margin": 100.0, "notional": 300.0, "upnl": 0.0}}
        assert adjust_for_foreign(600.0, 1200.0, breakdown, {"ETH"}) \
            == (700.0, 600.0, 300.0)


def test_open_allocates_from_bot_capital_only():
    # Available balance 2000; a foreign ETH position holds 1000 margin /
    # 10000 notional. The slice comes from the available 2000 only
    breakdown = {"ETH": {"margin": 1000.0, "notional": 10000.0, "upnl": 0.0}}
    client = StubClient(account=(2000.0, 10000.0, 1000.0), breakdown=breakdown)
    state = {"mirrored": {}}
    handle_open(client, state, trade("1"))
    kind, coin, size, trigger_px = client.orders[0]
    assert kind == "entry_trigger"
    assert size == round(2000.0 / config.MAX_OPEN_TRADES / 99.5, 4)

    # Foreign notional excluded from the account-leverage cap: 10000 foreign
    # notional would leave no headroom at 2x on the full account
    orig = config.MAX_ACCOUNT_LEVERAGE
    config.MAX_ACCOUNT_LEVERAGE = 2.0
    try:
        client = StubClient(account=(2000.0, 10000.0, 1000.0), breakdown=breakdown)
        state = {"mirrored": {}}
        handle_open(client, state, trade("1"))
        assert client.orders and client.orders[0][0] == "entry_trigger"
    finally:
        config.MAX_ACCOUNT_LEVERAGE = orig


def test_confirm_foreign_positions():
    breakdown = {"ETH": {"margin": 300.0, "notional": 900.0, "upnl": 50.0}}
    client = StubClient(positions={"ETH": 2.0}, account=(1000.0, 900.0, 300.0),
                        breakdown=breakdown)
    state = {"mirrored": {}}
    assert confirm_foreign_positions(client, state, ask=lambda _: "y") is True
    assert confirm_foreign_positions(client, state, ask=lambda _: "n") is False
    assert confirm_foreign_positions(client, state, ask=lambda _: "") is False

    # Bot-tracked positions aren't foreign, but the capital still needs a yes
    client = StubClient(positions={"BTC": 1.0},
                        breakdown={"BTC": {"margin": 100.0, "notional": 300.0, "upnl": 0.0}})
    state = {"mirrored": {"1": mirrored_entry()}}
    assert confirm_foreign_positions(client, state, ask=lambda _: "y") is True
    assert confirm_foreign_positions(client, state, ask=lambda _: "n") is False

    # A pending entry whose trigger fired while the bot was down (long BTC
    # position matches the entry) is ours - promoted on the first poll
    client = StubClient(positions={"BTC": 2.0},
                        breakdown={"BTC": {"margin": 100.0, "notional": 300.0, "upnl": 0.0}})
    state = {"mirrored": {"1": pending_entry(oid=7)}}
    assert confirm_foreign_positions(client, state, ask=lambda _: "y") is True

    # But an opposite-direction position on a pending entry's coin is foreign
    client = StubClient(positions={"BTC": -2.0}, account=(1000.0, 300.0, 100.0),
                        breakdown={"BTC": {"margin": 100.0, "notional": 300.0, "upnl": 0.0}})
    state = {"mirrored": {"1": pending_entry(oid=7)}}
    assert confirm_foreign_positions(client, state, ask=lambda _: "y") is True


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
