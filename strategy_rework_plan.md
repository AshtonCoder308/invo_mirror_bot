# Plan: hands-off positions, ignore list, no resizing, trigger-market entry at 0.5% better price

Status: **implemented** 2026-07-11. Written 2026-07-11, against commit `82e3297` (Phase 3 - Risk controls); kept as the design rationale for the trigger-entry rework.

## Context

Before testnet validation, four behavior changes to the mirror strategy (decisions already confirmed):

1. **Never touch positions the bot didn't open** — drop the same-direction *adoption* path in `handle_open`; any untracked exchange position (either direction, startup or mid-run) is skipped with a warning. Accepted trade-off: no lost-state-file recovery (startup `reconcile()` still warns; manual cleanup).
2. **`IGNORED_COINS`** — configurable set of Hyperliquid coin names never mirrored even when the target trades them.
3. **No resizing at all** — target increases *and* decreases are ignored. Once opened, our position only changes via full close (target closes, or our TP/SL fires).
4. **Entry at a 0.5% better price via trigger-market order** — when the target opens a trade, immediately place a resting **trigger order with market execution** at 0.5% beyond the target's entry: long → triggers when price falls to `entry_price × 0.995`; short → triggers when price rises to `entry_price × 1.005`. Market execution on trigger means no partial-fill handling. If the price never reaches the trigger, the order rests until the target closes the trade, then is cancelled (trade never entered). Notes: fill happens at market after the trigger (slippage-capped), so the realized entry can be slightly past 0.5%; trigger price is based on invoapp's `entry_price`.
5. **Websocket fill reaction (sub-2-minute)** — Hyperliquid's websocket is free (rate limits only, no fees; the SDK's `Info` already bundles a manager, currently disabled via `skip_ws=True`). Decision: keep the exchange-side resting trigger for entry timing (it fires at the exact price instant even if the bot is down) and subscribe to the **`userFills`** channel so the bot reacts within ~1s of a fill — placing TP/SL immediately instead of up to one poll (~2 min) later. Polling remains the backstop when the socket drops.

`fetch_open_positions.py` already supplies `entry_price` per trade — no fetch changes needed.

## Changes

### `config.py`
- Add `IGNORED_COINS = set()` (HL names, e.g. `"BTC"`) with a comment.
- Add `ENTRY_IMPROVEMENT = 0.005` — entry trigger placed this far beyond the target's entry price, in our favor.
- Remove `RESIZE_THRESHOLD` (resizing gone).

### `hl_client.py`
- **`place_entry_trigger(coin, is_buy, size, trigger_px)`** → resting oid. Same trigger order type `place_tpsl` uses (`{"trigger": {"triggerPx": px, "isMarket": True, "tpsl": "tp"}}` — "tp" gives fall-to-trigger for buys, rise-to-trigger for sells) but `reduce_only=False`. The order's limit px is the trigger px padded by `config.SLIPPAGE` (buy: `×(1+SLIPPAGE)`, sell: `×(1−SLIPPAGE)`) and rounded with `round_price`; leverage is set via `update_leverage` before placing. DRY_RUN short-circuits to a log line and returns a fake oid (keep the invariant).
- **`open_orders()`** → set of resting oids for the account (`info.open_orders(self.address)`), used for fill detection.
- **Websocket wiring**: constructor gains `on_fill=None`; when provided (live mode only), build `Info` with `skip_ws=False` and `info.subscribe({"type": "userFills", "user": self.address}, handler)`. The handler must **skip the initial snapshot message** (`isSnapshot: True` — historical fills, not new ones) and otherwise call `on_fill(fill)` per fill dict (`{oid, coin, side, sz, px, ...}`). The callback runs on the websocket thread, so it must not touch client/bot state — it only enqueues.

### `mirror_bot.py`
- **`diff_snapshots`**: return only `(opens, closes)`; drop resize detection. **Delete `handle_resize`** and its loop in `poll_once`; drop `target_size` from entries.
- **`handle_open`** becomes flat early-return branches:
  1. already tracked → return
  2. insert entry into `state["mirrored"]` first (phase-3 no-orphan invariant); entry gains `"entry_oid": None`
  3. `coin in config.IGNORED_COINS` → log, stay `mirrored: False`
  4. not listed → warn (unchanged)
  5. any existing exchange position on the coin (either direction) → warn + skip (replaces adopt/opposite branches)
  6. otherwise: risk check via existing `allocate_margin` (mirror_bot.py:60); size = surviving notional / trigger price, rounded; place the entry trigger; store `entry_oid`; `mirrored` stays False until the trigger fills. TP/SL cannot be placed yet (reduce-only needs a position).
- **Fill detection in `poll_once`** (skipped in DRY_RUN, like the other exchange checks): for entries with an `entry_oid` and `mirrored == False`, fetch `client.open_orders()` and `client.open_positions()` once, then per entry:
  - oid still resting → keep waiting
  - oid gone + position on the coin in our direction → filled: record `hl_size` from the actual position, `mirrored = True`, clear `entry_oid`, place TP/SL via existing `place_tpsl`
  - oid gone + no position → order was cancelled/rejected externally: warn, entry stays unmirrored (never retried)
  Each transition is followed by `save_state`, same pattern as the event loops. This poll-time sweep is the **backstop**; the websocket path below usually gets there first.
- **Websocket fill reaction in `main`** (live mode only): create a `queue.Queue`; pass `on_fill=q.put` to `HLClient`. Replace `time.sleep(config.POLL_INTERVAL_S)` with a deadline wait: `q.get(timeout=remaining)` in a loop until the next poll is due, so a fill wakes the loop within ~1s. Each dequeued fill goes to a new `handle_fill(client, state, fill)`: match `fill["oid"]` against entries with a resting `entry_oid`; on match, promote exactly like the poll-time sweep (record `hl_size` from `client.open_positions()` for accuracy, `mirrored = True`, clear `entry_oid`, place TP/SL, `save_state`). Unmatched fills (TP/SL executions, manual trades) are logged at DEBUG and left to the existing poll-time close/reconcile logic. All order placement and state mutation stays on the main thread — the ws thread only enqueues.
- **`handle_close`**: when the entry has a resting `entry_oid`, cancel it (reuse `cancel_orders`); if the trigger fired between polls and a position exists, close it and cancel leftover TP/SL — extends the existing already-closed/still-open logic.
- **`reconcile()`**: warning text "opens on this coin will halt" → "will be skipped".

### `test_diff.py`
- `trade()` helper gains `entry_price=100.0`; StubClient gains `place_entry_trigger` (records `("entry_trigger", coin, size, trigger_px)`, returns oid 7), `open_orders()` backed by a settable set.
- Remove all `test_resize_*` tests; update diff tests to the 2-tuple return.
- Replace `test_open_adopts_existing_same_direction_position` → skip test (no orders, `mirrored` False).
- New tests: ignored coin skipped; long open places trigger at 99.5 / short at 100.5 with no immediate position; fill detection promotes the entry (hl_size from position, TP/SL placed, `mirrored` True); externally-vanished order warns and stays unmirrored; close while trigger resting cancels it; close after trigger fired mid-poll closes the position; `handle_fill` with a matching oid promotes the entry, with an unknown oid does nothing (offline — `handle_fill` takes plain dicts, no ws needed).

### Docs
- `CLAUDE.md`: remove the resize invariant bullet; replace the adoption sentence with the never-touch rule; document `IGNORED_COINS`, the trigger-entry lifecycle (`entry_oid` → fill via websocket or poll backstop → TP/SL), the ws-thread-only-enqueues rule, and that fill detection/websocket are disabled in dry-run.
- `execution.md`: under Phase 2, note the decisions (no resize mirroring; entries via trigger-market at 0.5% better than the target's entry); under Phase 3 add checked bullets for the hands-off rule and `IGNORED_COINS`.

## Verification

- `wsl -e bash -c "cd '/mnt/c/Users/ashne/My Drive/projects/invo_mirror_bot' && venv_wsl/bin/python test_diff.py"` → all tests pass.
- Single commit on `testnet_bot` when green.
