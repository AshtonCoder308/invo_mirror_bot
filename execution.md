# Hyperliquid Mirror Bot: Testnet to Live Deployment Workflow

## Overview
This bot mirrors the perp futures positions of a target invoapp portfolio onto Hyperliquid. Signals come from polling the invoapp API (`fetch_open_positions.py`); execution happens on Hyperliquid via the Python SDK. Since it tracks another portfolio's live trades rather than a standalone strategy, backtesting is skipped in favor of direct testnet validation.

---

## Phase 1: Environment Setup

- [x] Create a Hyperliquid testnet account
- [x] Fund the account with testnet USDC via the faucet (https://app.hyperliquid-testnet.xyz/drip; requires a wallet that has been funded on mainnet)
- [x] Generate API (agent) wallets, kept separate from the main wallet, for programmatic order placement — approved separately per network (testnet and mainnet)
- [x] Store credentials outside the Drive-synced folder in `C:\Users\ashne\.secrets\invo_mirror_bot.env`: invoapp JWT + portfolio ID, `HL_ACCOUNT_ADDRESS` (main wallet public address), `HL_TESTNET_SECRET_KEY` and `HL_MAINNET_SECRET_KEY` (API wallet private keys)
- [x] Install `hyperliquid-python-sdk`
- [x] Identify the signal source: the target invoapp portfolio (`PORTFOLIO_ID`), fetched with `fetch_open_positions.py`

---

## Phase 2: Build the Mirror Logic

- Poll the invoapp API on an interval and diff consecutive position snapshots to detect opens, closes, and size changes
- Map invoapp tickers to Hyperliquid coin names (e.g. plain `ETH`, `BTC`); skip or alert on assets not listed on Hyperliquid
- Define the mirroring rule:
  - Fixed ratio of target's position size
  - Fixed dollar allocation per trade
  - Proportional to your account equity vs the target's equity
  - (implemented: equal slices of own account value — each trade's margin is account value / `MAX_OPEN_TRADES`, or the remaining free margin if that's less)
- Map target's asset, side, size, and leverage to an equivalent order on your account (convert invoapp USD position size to coin units, rounded to the asset's `szDecimals`)
- Choose order type: market (speed) vs limit (price control) and set slippage tolerance
- Handle edge cases:
  - Position increases and decreases (scaling in/out)
  - Leverage mismatches between your account and theirs
  - Full position closes
  - Positions opened before the bot started (decision: mirror existing positions on first start; tracked state is persisted to `open_positions.json` so restarts resume without re-mirroring)
  - Polling gaps — a position opened and closed between two polls is invisible

---

## Phase 3: Risk Controls (build before any live testing)

- [x] Max position size per asset (`MAX_POSITION_NOTIONAL_USD` caps every open and resize increase)
- [x] Max total exposure / account-wide leverage cap (total open notional capped at `MAX_ACCOUNT_LEVERAGE` × account value)
- [x] Daily loss limit that automatically halts the bot (`DAILY_LOSS_LIMIT` vs the UTC day's starting equity, checked every poll; positions are left untouched on halt)
- [x] Reject trades if mirrored size implies unrealistic leverage for your account balance (per-trade margin = min(account value / `MAX_OPEN_TRADES`, free margin), rejected when the surviving notional is below `MIN_NOTIONAL_USD`)
- [x] Full logging of every signal received and every order sent (timestamp, size, price, response)

---

## Phase 4: Testnet Validation

- Run the bot live on testnet, mirroring the target's real trades with fake capital
- Verify:
  - Latency between target's fill and your order placement
  - Correct sizing and side/leverage mapping
  - Fee handling accuracy
  - Error handling on rejected or failed orders
- Deliberately test failure modes:
  - API disconnects
  - Rate limiting
  - Target opening an unusually large position
  - Target closing a position instantly

---

## Phase 5: Mainnet Dry Run

- Switch endpoint to mainnet, keep position sizes at the minimum allowed
- Run for 1-2 weeks with real money at minimal stakes
- Compare live fills and slippage against testnet expectations

---

## Phase 6: Scaled Deployment

- Increase capital allocation gradually: e.g. 10% of target size, then 50%, then full
- Set up alerting (Telegram/Discord webhook) for errors, large drawdowns, and disconnects
- Keep a manual kill switch accessible at all times
- Review logs daily for the first few weeks of full deployment

---

## Summary Checklist

- [x] Testnet account + agent wallets created, credentials stored outside synced folder
- [x] Testnet account funded via faucet
- [x] SDK installed
- [x] Mirror logic built and mapped to target invoapp portfolio (`mirror_bot.py`; dry-run verified, live testnet validation pending)
- [x] Risk controls implemented (position cap, leverage cap, daily loss limit)
- [x] Logging in place for signals and orders (console + `logs/mirror.log`)
- [ ] Testnet run completed, failure modes tested
- [ ] Mainnet dry run at minimum size completed
- [ ] Alerting and kill switch active
- [ ] Capital scaled gradually to full allocation