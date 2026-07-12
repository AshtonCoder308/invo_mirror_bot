import os

from hyperliquid.utils import constants

# Secrets live outside the Drive-synced project folder so they are never uploaded.
# The bot may run from Windows or WSL, so pick whichever path form exists.
_SECRETS_CANDIDATES = (
    r"C:\Users\ashne\.secrets\invo_mirror_bot.env",
    "/mnt/c/Users/ashne/.secrets/invo_mirror_bot.env",
)
SECRETS_PATH = next((p for p in _SECRETS_CANDIDATES if os.path.exists(p)), _SECRETS_CANDIDATES[0])

BASE_URL = constants.TESTNET_API_URL
POLL_INTERVAL_S = 120
RETRY_DELAY_S = 10  # quick retry after a transient invoapp error before waiting a full poll

# Dynamic sizing: each new trade's margin is an equal slice of total account
# value (unrealized PnL included), or whatever free margin is left if that's less.
MAX_OPEN_TRADES = 3

# Risk controls (phase 3). The notional caps apply to every open.
MAX_POSITION_NOTIONAL_USD = 20_000.0  # hard cap on any single position's notional
MAX_ACCOUNT_LEVERAGE = 15.0           # cap on total open notional / account value

SLIPPAGE = 0.01

# Hyperliquid coin names (e.g. "BTC") never mirrored even when the target
# trades them; the trade is tracked as mirrored=False so it isn't retried.
IGNORED_COINS = set()

# Entries rest as trigger-market orders placed this far beyond the target's
# entry price, in our favor (long: 0.5% below, short: 0.5% above). If the
# price never reaches the trigger, the trade is never entered.
ENTRY_IMPROVEMENT = 0.005

MAX_LEVERAGE = 20          # cap regardless of the target's leverage
MIN_NOTIONAL_USD = 10.0    # Hyperliquid minimum order size

# When True, every order is logged but nothing is sent to the exchange
DRY_RUN = False

LOG_PATH = "logs/mirror.log"

# Persisted bot state: last target snapshot + open mirrored positions.
# Lets a restart resume without re-mirroring positions it already holds.
OPEN_POSITIONS_PATH = "open_positions.json"
