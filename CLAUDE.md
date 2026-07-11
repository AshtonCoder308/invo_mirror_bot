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

- **`mirror_bot.py`** — main loop. Each poll builds a trade-id-keyed snapshot of the target's open trades and diffs it against the previous one (`diff_snapshots`) to produce open/close/resize events, handled by `handle_open` / `handle_close` / `handle_resize`. State (`{"snapshot": ..., "mirrored": ...}`) is persisted to `open_positions.json` after **every** event via atomic write-then-rename, so a crash mid-poll never re-mirrors or corrupts. On first start with no state file, all currently open target positions are treated as opens and mirrored — this is intentional.
- **`hl_client.py`** — all exchange interaction. Every order method short-circuits to a log line when `config.DRY_RUN` is true; keep that invariant when adding methods. `_check()` exists because Hyperliquid returns `"status": "ok"` responses that still contain per-order errors. Sizes must be rounded to the asset's `szDecimals` and prices to 5 significant figures / `6 - szDecimals` decimals (`round_size` / `round_price`).
- **`config.py`** — all tunables (poll interval, per-trade allocation via `MAX_OPEN_TRADES`, risk caps, leverage cap, min notional, dry-run flag, testnet vs mainnet URL).

Key invariants when changing mirror logic:
- Entries in `state["mirrored"]` exist even for trades that were *not* mirrored (`"mirrored": False`, e.g. coin not listed on Hyperliquid) so they aren't retried every poll; close/resize handlers must check the flag. `handle_open` inserts the entry *before* sending any order, so a failure mid-open (e.g. TP/SL rejected after the position filled) still leaves the position tracked when `poll_once` persists state.
- invoapp's `positionSize` is mark-to-market, so it drifts with price on every poll. Resizes are therefore measured against `target_size` (the size the mirror last scaled to, stored in the mirrored entry) and only mirrored when the relative change exceeds `config.RESIZE_THRESHOLD`; the local position scales by that *ratio*, not absolute size, because local sizing (equal account-value slices) is independent of the target's absolute size.
- The target's leverage is capped by both `config.MAX_LEVERAGE` and the asset's own max.
- Risk controls (phase 3) gate every exposure-adding order: a new trade's margin is `min(account_value / MAX_OPEN_TRADES, free_margin)` (`allocate_margin`), its notional is clamped by `MAX_POSITION_NOTIONAL_USD` and `MAX_ACCOUNT_LEVERAGE`, and it's rejected (`mirrored: False`) when the surviving notional is below `MIN_NOTIONAL_USD`. Resize increases obey the same notional caps plus a free-margin clamp (clamped, not skipped). `check_daily_loss` halts the bot — leaving positions untouched — when equity drops `DAILY_LOSS_LIMIT` below the UTC day's starting equity (anchored in `state["risk"]`); the check is skipped in dry-run.
- Positions opened and closed between two polls are invisible — a known limitation of polling.
- The bot reconciles against the exchange's actual positions (`HLClient.open_positions`) instead of blindly trusting its state file. An open where the exchange already holds that coin in the same direction *adopts* the existing position (records its size, places TP/SL, sends no order — a second order would merge anyway); an opposite-direction position is skipped with a warning (`mirrored: False`). A close where the position is already gone (TP/SL fired, manual close) cancels leftover triggers and drops the entry without sending a close order — this check is skipped in dry-run, where mirrored positions never exist on the exchange. Startup also runs `reconcile()` (live mode only) to warn on tracked-vs-exchange divergence.

Runtime artifacts `open_positions.json` and `logs/mirror.log` are gitignored but present locally; they reflect real bot state, so don't delete or overwrite them casually.
