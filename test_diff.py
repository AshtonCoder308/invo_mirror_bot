"""Offline tests for the snapshot diff and ticker normalization. Run: python test_diff.py"""
from mirror_bot import diff_snapshots, normalize_ticker


def trade(tid, size=100.0):
    return {"id": tid, "ticker": "BTC/USDT", "direction": "long", "position_size": size}


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
