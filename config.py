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

# Placeholder sizing: fixed margin per mirrored trade (notional = margin * leverage).
# Later: proportional to account equity vs target portfolio equity from invoapp.
FIXED_MARGIN_USD = 100.0

SLIPPAGE = 0.01

# invoapp's positionSize is mark-to-market, so it drifts with price every poll.
# Only mirror a resize when it moved this far (relative) from the size we last
# scaled to; smaller changes are treated as price noise.
RESIZE_THRESHOLD = 0.05
MAX_LEVERAGE = 10          # cap regardless of the target's leverage
MIN_NOTIONAL_USD = 10.0    # Hyperliquid minimum order size

# When True, every order is logged but nothing is sent to the exchange
DRY_RUN = False

LOG_PATH = "logs/mirror.log"

# Persisted bot state: last target snapshot + open mirrored positions.
# Lets a restart resume without re-mirroring positions it already holds.
OPEN_POSITIONS_PATH = "open_positions.json"
