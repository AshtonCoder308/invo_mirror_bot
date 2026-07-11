# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A copy-trading bot that polls a target invoapp portfolio and mirrors its perp futures positions onto Hyperliquid. `execution.md` is the deployment roadmap (testnet → mainnet); consult it before adding features.

## Commands

```bash
python test_diff.py            # run all tests (plain asserts, no pytest)
python mirror_bot.py           # run the bot (polls every POLL_INTERVAL_S)
python fetch_open_positions.py # one-off: print the target portfolio's open trades
```

There is no requirements.txt; dependencies are `hyperliquid-python-sdk`, `eth_account`, `requests`, `python-dotenv`. The `venv_wsl/` virtualenv is for running under WSL and is gitignored.

Tests are plain functions named `test_*` executed by the `__main__` block in `test_diff.py`. To run a single test, run the file and it executes all of them (they're fast), or import the function in `python -c`. Tests are offline only — they exercise diffing, ticker normalization, and state persistence without touching either API.

## Secrets

Credentials live **outside** this Drive-synced folder at `C:\Users\ashne\.secrets\invo_mirror_bot.env` (WSL path fallback in `config.py`). Required env vars: `PORTFOLIO_ID`, `INVOAPP_JWT`, `PUBLIC_KEY` (main wallet address), `TESTNET_PRIVATE_KEY` (agent wallet key). The invoapp JWT expires; the bot exits cleanly on 401 and the JWT must be renewed manually in that file. Never move secrets into the repo — this folder syncs to Google Drive.

## Architecture

Signal flow: `fetch_open_positions.py` (invoapp API → normalized trade dicts) → `mirror_bot.py` (diff + strategy) → `hl_client.py` (Hyperliquid SDK wrapper) → exchange.

- **`mirror_bot.py`** — main loop. Each poll builds a trade-id-keyed snapshot of the target's open trades and diffs it against the previous one (`diff_snapshots`) to produce open/close events, handled by `handle_open` / `handle_close`. Target resizes are deliberately **not** mirrored: once opened, our position only changes via a full close (target closes, or our TP/SL fires). State (`{"snapshot": ..., "mirrored": ...}`) is persisted to `open_positions.json` after **every** event via atomic write-then-rename, so a crash mid-poll never re-mirrors or corrupts. On first start with no state file, all currently open target positions are treated as opens and mirrored — this is intentional.
- **`hl_client.py`** — all exchange interaction. Every order method short-circuits to a log line when `config.DRY_RUN` is true; keep that invariant when adding methods. `_check()` exists because Hyperliquid returns `"status": "ok"` responses that still contain per-order errors. Sizes must be rounded to the asset's `szDecimals` and prices to 5 significant figures / `6 - szDecimals` decimals (`round_size` / `round_price`).
- **`config.py`** — all tunables (poll interval, per-trade allocation via `MAX_OPEN_TRADES`, risk caps, leverage cap, min notional, dry-run flag, testnet vs mainnet URL).

Key invariants when changing mirror logic:
- Entries in `state["mirrored"]` exist even for trades that were *not* mirrored (`"mirrored": False`, e.g. coin not listed on Hyperliquid or in `config.IGNORED_COINS`) so they aren't retried every poll; close handlers must check the flag. `handle_open` inserts the entry *before* sending any order, so a failure mid-open still leaves the order tracked when `poll_once` persists state.
- **Trigger-entry lifecycle**: `handle_open` never opens at market. It rests a trigger-market order at `ENTRY_IMPROVEMENT` (0.5%) beyond the target's entry price in our favor (long: below, short: above) and stores its oid in the entry's `entry_oid`; `mirrored` stays False until the trigger fills. Fills are detected two ways: the `userFills` websocket (`handle_fill`, usually within ~1s) and the poll-time sweep `check_pending_entries` as backstop. Either path *promotes* the entry — records `hl_size` from the actual exchange position, sets `mirrored: True`, clears `entry_oid`, and only then places TP/SL (reduce-only orders need a position). If the price never reaches the trigger, the order rests until the target closes the trade, then `handle_close` cancels it — the trade is simply never entered. An oid that vanishes without a fill (cancelled externally) is warned once and never retried.
- **The websocket thread only enqueues.** `HLClient(on_fill=...)` subscribes to `userFills` (skipping the `isSnapshot` replay); the callback runs on the ws thread and must not touch client or bot state — fills go into a `queue.Queue` and are handled on the main thread between polls (`main`'s deadline wait wakes on each fill). Fill detection and the websocket are both disabled in dry-run, where orders never rest on the exchange.
- The target's leverage is capped by both `config.MAX_LEVERAGE` and the asset's own max.
- Risk controls (phase 3) gate every exposure-adding order: a new trade's margin is `min(account_value / MAX_OPEN_TRADES, free_margin)` (`allocate_margin`), its notional is clamped by `MAX_POSITION_NOTIONAL_USD` and `MAX_ACCOUNT_LEVERAGE`, and it's rejected (`mirrored: False`) when the surviving notional is below `MIN_NOTIONAL_USD`. `check_daily_loss` halts the bot — leaving positions untouched — when equity drops `DAILY_LOSS_LIMIT` below the UTC day's starting equity (anchored in `state["risk"]`); the check is skipped in dry-run.
- Positions opened and closed between two polls are invisible — a known limitation of polling.
- **The bot never touches positions it didn't open.** An open where the exchange already holds that coin — either direction (lost state file, manual trade, ...) — is skipped with a warning (`mirrored: False`); there is no adoption path, so a lost state file requires manual cleanup. A close where the position is already gone (TP/SL fired, manual close) cancels leftover triggers and drops the entry without sending a close order — this check is skipped in dry-run, where mirrored positions never exist on the exchange. Startup also runs `reconcile()` (live mode only) to warn on tracked-vs-exchange divergence.

Runtime artifacts `open_positions.json` and `logs/mirror.log` are gitignored but present locally; they reflect real bot state, so don't delete or overwrite them casually.
