# Hyperliquid Mirror Bot: Testnet to Live Deployment Workflow

## Overview
This bot mirrors the perp futures positions of a target trader's wallet on Hyperliquid. Since it tracks another portfolio's live trades rather than a standalone strategy, backtesting is skipped in favor of direct testnet validation.

---

## Phase 1: Environment Setup

- Create a Hyperliquid testnet account
- Fund the account with testnet USDC via the faucet
- Generate an API (agent) wallet, kept separate from your main wallet, for programmatic order placement
- Install `hyperliquid-python-sdk` (or integrate directly with the REST/WebSocket API)
- Identify the target trader's wallet address; Hyperliquid exposes position and fill data publicly on-chain, no permission required

---

## Phase 2: Build the Mirror Logic

- Subscribe to the target wallet's activity via WebSocket (`userFills`, `userEvents`) or poll the info endpoint
- Define the mirroring rule:
  - Fixed ratio of target's position size
  - Fixed dollar allocation per trade
  - Proportional to your account equity vs the target's equity
- Map target's asset, side, and size to an equivalent order on your account
- Choose order type: market (speed) vs limit (price control) and set slippage tolerance
- Handle edge cases:
  - Partial fills on the target's side
  - Position increases and decreases (scaling in/out)
  - Leverage mismatches between your account and theirs
  - Full position closes

---

## Phase 3: Risk Controls (build before any live testing)

- Max position size per asset
- Max total exposure / account-wide leverage cap
- Daily loss limit that automatically halts the bot
- Reject trades if mirrored size implies unrealistic leverage for your account balance
- Full logging of every signal received and every order sent (timestamp, size, price, response)

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

- [ ] Testnet account + agent wallet created
- [ ] Mirror logic built and mapped to target wallet
- [ ] Risk controls implemented (position cap, leverage cap, daily loss limit)
- [ ] Logging in place for signals and orders
- [ ] Testnet run completed, failure modes tested
- [ ] Mainnet dry run at minimum size completed
- [ ] Alerting and kill switch active
- [ ] Capital scaled gradually to full allocation