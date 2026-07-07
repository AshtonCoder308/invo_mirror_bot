# invo_mirror_bot

Copy-trading bot: polls a target [invoapp](https://invoapp.com) portfolio and mirrors its perp positions onto [Hyperliquid](https://hyperliquid.xyz).

- Polls invoapp every 2 min, diffs open-trade snapshots, mirrors opens / closes / resizes
- Fixed $100 margin per trade, leverage capped at 10x, TP/SL mirrored as trigger orders
- Resizes below 5% are ignored (invoapp's `positionSize` drifts with price)
- State persisted to `open_positions.json` — restarts don't re-mirror
- Reconciles against actual exchange positions: adopts an existing same-direction position instead of doubling up, cleans up state when a position was already closed (e.g. TP/SL fired)
- `DRY_RUN = True` in `config.py` logs orders without sending them

## Files

| File | Purpose |
|---|---|
| `mirror_bot.py` | Main loop: poll, diff, mirror |
| `hl_client.py` | Hyperliquid SDK wrapper (orders, TP/SL, rounding) |
| `fetch_open_positions.py` | invoapp API client; run standalone to print target's open trades |
| `config.py` | All tunables (poll interval, sizing, leverage cap, dry-run, testnet/mainnet URL) |
| `test_diff.py` | Offline tests |
| `execution.md` | Testnet → mainnet deployment roadmap |

## Setup

### 1. Create venv + install requirements

```bash
python3 -m venv venv_wsl
source venv_wsl/bin/activate
pip install -r requirements.txt
```

### 2. Set env vars

Create `C:\Users\ashne\.secrets\invo_mirror_bot.env` (outside the repo — this folder syncs to Google Drive):

```env
PORTFOLIO_ID=<target invoapp portfolio id>
INVOAPP_JWT=<invoapp bearer token>
PUBLIC_KEY=<Hyperliquid main wallet address>
TESTNET_PRIVATE_KEY=<Hyperliquid API/agent wallet private key>
```

Notes:
- The JWT expires. The bot exits on 401 — paste a fresh token and restart.
- Use an agent wallet key, not the main wallet key. Fund testnet via the [faucet](https://app.hyperliquid-testnet.xyz/drip).

### 3. Run

```bash
# check the signal source works
python fetch_open_positions.py

# run tests
python test_diff.py

# run the bot
python mirror_bot.py
```

Terminal shows one line per poll plus actual actions; full DEBUG trace goes to `logs/mirror.log`.

## Going live

Set `DRY_RUN = False` in `config.py` for real testnet orders; switch `BASE_URL` to `MAINNET_API_URL` for mainnet. Follow the phases in `execution.md` — risk controls before real money.
