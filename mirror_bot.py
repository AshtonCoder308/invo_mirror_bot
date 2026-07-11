import json
import logging
import os
import queue
import time

import requests
from dotenv import load_dotenv

import config

load_dotenv(config.SECRETS_PATH)

from fetch_open_positions import fetch_open_trades
from hl_client import HLClient, OrderError

logger = logging.getLogger("mirror")

QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "PERP")


def normalize_ticker(ticker):
    """Map an invoapp ticker to a Hyperliquid coin name, e.g. 'BTC/USDT' -> 'BTC'."""
    t = (ticker or "").upper().replace("/", "").replace("-", "")
    for suffix in QUOTE_SUFFIXES:
        if t.endswith(suffix) and len(t) > len(suffix):
            return t[: -len(suffix)]
    return t


def load_state():
    if not os.path.exists(config.OPEN_POSITIONS_PATH):
        return None
    with open(config.OPEN_POSITIONS_PATH) as f:
        return json.load(f)


def save_state(state):
    # Write-then-replace so a crash mid-write can't corrupt the state file
    tmp = config.OPEN_POSITIONS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, config.OPEN_POSITIONS_PATH)


def diff_snapshots(prev, curr):
    """Compare trade-id-keyed snapshots. Returns (opens, closes). Target
    resizes are deliberately not mirrored: once opened, our position only
    changes via a full close."""
    opens = [t for tid, t in curr.items() if tid not in prev]
    closes = [t for tid, t in prev.items() if tid not in curr]
    return opens, closes


def allocate_margin(account_value, free_margin, total_notional, leverage):
    """Risk-checked margin for a new trade, or None when the trade is rejected.

    Each trade gets an equal slice of account value (account_value /
    MAX_OPEN_TRADES); when free margin can't cover a full slice, whatever is
    left gets invested instead. The implied notional is then clamped by the
    per-position cap and the account-wide leverage cap, and the trade is
    rejected outright if what survives is below the exchange minimum."""
    margin = min(account_value / config.MAX_OPEN_TRADES, free_margin)
    notional = min(margin * leverage,
                   config.MAX_POSITION_NOTIONAL_USD,
                   account_value * config.MAX_ACCOUNT_LEVERAGE - total_notional)
    if notional < config.MIN_NOTIONAL_USD:
        return None
    return notional / leverage


def handle_open(client, state, trade):
    tid = str(trade["id"])
    if tid in state["mirrored"]:
        # Already mirrored in a previous run whose snapshot wasn't saved yet
        logger.debug("OPEN %s: already tracked, skipping", tid)
        return
    coin = normalize_ticker(trade["ticker"])
    entry = {"coin": coin, "is_buy": trade["direction"] == "long", "mirrored": False,
             "hl_size": 0, "leverage": trade.get("leverage") or 1, "tpsl_oids": [],
             "entry_oid": None, "tp_px": trade.get("price_target"),
             "sl_px": trade.get("stop_loss")}
    # Track the entry before any order goes out: poll_once persists state even
    # when a handler raises, so a failure mid-open must not leave an order or
    # position on the exchange untracked
    state["mirrored"][tid] = entry
    if coin in config.IGNORED_COINS:
        logger.info("OPEN %s: %s is in IGNORED_COINS, skipping", tid, coin)
        return
    if not client.is_listed(coin):
        logger.warning("skip %s (%s, asset_type=%s): %s not listed on Hyperliquid",
                       tid, trade["ticker"], trade.get("asset_type"), coin)
        return
    szi = client.open_positions().get(coin)
    if szi:
        # Never touch a position the bot didn't open, in either direction:
        # warn and leave it alone (mirrored stays False)
        logger.warning("OPEN %s: exchange already holds %s (size %s) not opened by "
                       "the bot, skipping - resolve manually", tid, coin, szi)
        return
    lev = min(entry["leverage"], config.MAX_LEVERAGE)
    account_value, total_notional, margin_used = client.margin_summary()
    margin = allocate_margin(account_value, account_value - margin_used, total_notional, lev)
    if margin is None:
        logger.warning("OPEN %s: rejected by risk limits - no capital left for %s "
                       "(account %.2f, free %.2f, open notional %.2f)",
                       tid, coin, account_value, account_value - margin_used, total_notional)
        return
    # Rest a trigger ENTRY_IMPROVEMENT beyond the target's entry, in our favor
    # (long: below, short: above). mirrored stays False until it fills - TP/SL
    # are reduce-only and need a position to exist first
    improve = 1 - config.ENTRY_IMPROVEMENT if entry["is_buy"] else 1 + config.ENTRY_IMPROVEMENT
    trigger_px = trade["entry_price"] * improve
    size = client.round_size(coin, margin * lev / trigger_px)
    if size <= 0:
        raise OrderError(f"{coin}: size rounds to 0")
    entry["entry_oid"] = client.place_entry_trigger(coin, entry["is_buy"], size, trigger_px, lev)
    logger.info("OPEN trigger placed: %s %s size=%s trigger=%s (invoapp trade %s)",
                coin, "long" if entry["is_buy"] else "short", size, trigger_px, tid)


def handle_close(client, state, trade):
    tid = str(trade["id"])
    entry = state["mirrored"].pop(tid, None)
    if not entry:
        logger.debug("CLOSE %s: was not tracked, nothing to do", tid)
        return
    if entry.get("entry_oid") is not None:
        # Entry trigger never promoted: cancel it (the trade was never
        # entered). If it fired between polls, a position exists and must be
        # closed like a mirrored one; skipped in dry-run where orders and
        # positions never exist on the exchange
        client.cancel_orders(entry["coin"], [entry["entry_oid"]])
        if not config.DRY_RUN:
            szi = client.open_positions().get(entry["coin"])
            if szi and (szi > 0) == entry["is_buy"]:
                client.cancel_orders(entry["coin"], entry["tpsl_oids"])
                client.close_position(entry["coin"], abs(szi))
                logger.warning("CLOSE %s: entry trigger for %s had fired between polls, "
                               "closed the position", tid, entry["coin"])
                return
        logger.info("CLOSE %s: cancelled resting entry trigger for %s (never filled)",
                    tid, entry["coin"])
        return
    if not entry["mirrored"]:
        logger.debug("CLOSE %s: was not mirrored, nothing to do", tid)
        return
    # In dry-run mirrored positions never exist on the exchange, so the check
    # would always trip
    if not config.DRY_RUN and entry["coin"] not in client.open_positions():
        # Already closed on the exchange (TP/SL fired, manual close): the
        # account already matches the target - just clean up leftover triggers
        client.cancel_orders(entry["coin"], entry["tpsl_oids"])
        logger.warning("CLOSE %s: %s already closed on exchange, cleaned up state",
                       tid, entry["coin"])
        return
    client.cancel_orders(entry["coin"], entry["tpsl_oids"])
    client.close_position(entry["coin"], entry["hl_size"])
    logger.info("CLOSE mirrored: %s (invoapp trade %s)", entry["coin"], tid)


def promote_entry(client, entry, size):
    """A resting entry trigger filled: record the position and guard it with
    TP/SL. mirrored/hl_size are set before place_tpsl so a rejected trigger
    still leaves the position tracked when the caller persists state."""
    entry["hl_size"] = size
    entry["mirrored"] = True
    entry["entry_oid"] = None
    entry["tpsl_oids"] = client.place_tpsl(
        entry["coin"], entry["is_buy"], size, entry.get("tp_px"), entry.get("sl_px"))


def check_pending_entries(client, state):
    """Poll-time backstop for the websocket: promote entries whose resting
    entry trigger has filled; an oid that vanished without producing a
    position was cancelled externally and stays unmirrored (never retried)."""
    pending = {tid: e for tid, e in state["mirrored"].items()
               if not e["mirrored"] and e.get("entry_oid") is not None}
    if not pending:
        return
    resting = client.open_orders()
    positions = client.open_positions()
    for tid, entry in pending.items():
        if entry["entry_oid"] in resting:
            continue  # price hasn't reached the trigger yet
        try:
            szi = positions.get(entry["coin"])
            if szi and (szi > 0) == entry["is_buy"]:
                promote_entry(client, entry, abs(szi))
                logger.info("ENTRY filled: %s %s size=%s (invoapp trade %s)",
                            entry["coin"], "long" if entry["is_buy"] else "short",
                            entry["hl_size"], tid)
            else:
                entry["entry_oid"] = None
                logger.warning("ENTRY %s: trigger for %s gone without a fill "
                               "(cancelled externally?) - trade stays unmirrored",
                               tid, entry["coin"])
        except OrderError:
            logger.exception("failed to promote filled entry of trade %s", tid)
        save_state(state)


def handle_fill(client, state, fill):
    """Websocket fill dequeued on the main thread: promote the matching entry
    within ~1s instead of waiting for the next poll's backstop sweep."""
    oid = fill.get("oid")
    match = next(((tid, e) for tid, e in state["mirrored"].items()
                  if not e["mirrored"] and e.get("entry_oid") == oid), None)
    if match is None:
        # TP/SL executions and manual trades land here; the poll-time
        # close/reconcile logic picks those up
        logger.debug("fill oid %s (%s) matches no pending entry, ignoring",
                     oid, fill.get("coin"))
        return
    tid, entry = match
    try:
        # The actual position beats the fill's sz (a merged or partial fill
        # would make them differ)
        szi = client.open_positions().get(entry["coin"])
        promote_entry(client, entry, abs(szi) if szi else float(fill["sz"]))
        logger.info("ENTRY filled (ws): %s %s size=%s (invoapp trade %s)",
                    entry["coin"], "long" if entry["is_buy"] else "short",
                    entry["hl_size"], tid)
    finally:
        save_state(state)


def check_daily_loss(client, state):
    """True when equity fell DAILY_LOSS_LIMIT below the UTC day's starting
    equity - the bot must halt. Anchors a fresh baseline at each UTC day."""
    equity = client.margin_summary()[0]
    today = time.strftime("%Y-%m-%d", time.gmtime())
    risk = state.get("risk") or {}
    if risk.get("day") != today:
        state["risk"] = {"day": today, "day_start_equity": equity}
        save_state(state)
        return False
    floor = risk["day_start_equity"] * (1 - config.DAILY_LOSS_LIMIT)
    if equity < floor:
        msg = (f"daily loss limit hit: equity {equity:.2f} below {floor:.2f} "
               f"({config.DAILY_LOSS_LIMIT:.0%} under day start {risk['day_start_equity']:.2f}) - "
               f"bot halted, open positions and TP/SL orders left untouched")
        logger.error(msg)
        print(f"\n!!! {msg} !!!")
        return True
    return False


def reconcile(client, state):
    """Warn when tracked mirrored positions and actual exchange positions diverge."""
    exchange = client.open_positions()
    tracked = {e["coin"] for e in state["mirrored"].values() if e["mirrored"]}
    for coin in sorted(set(exchange) - tracked):
        logger.warning("reconcile: untracked %s position on exchange (size %s) - "
                       "not managed by the bot, opens on this coin will be skipped",
                       coin, exchange[coin])
    for coin in sorted(tracked - set(exchange)):
        logger.warning("reconcile: tracked %s position missing on exchange "
                       "(TP/SL fired or closed manually?)", coin)


def poll_once(client, state, portfolio_id, jwt_token):
    trades = fetch_open_trades(portfolio_id, jwt_token, size=100)
    curr = {str(t["id"]): t for t in trades}

    logger.info("poll: %d open target trades", len(curr))
    # Backstop for the websocket: promote any entry trigger that filled since
    # the last poll before diffing, so a same-poll close sees it as mirrored.
    # In dry-run orders never rest on the exchange, so there's nothing to sweep
    if not config.DRY_RUN:
        check_pending_entries(client, state)
    opens, closes = diff_snapshots(state["snapshot"], curr)
    logger.debug("events: %d open, %d close", len(opens), len(closes))

    for trade in opens:
        try:
            handle_open(client, state, trade)
        except OrderError:
            logger.exception("failed to mirror open of trade %s", trade["id"])
        save_state(state)
    for trade in closes:
        try:
            handle_close(client, state, trade)
        except OrderError:
            logger.exception("failed to mirror close of trade %s", trade["id"])
        save_state(state)

    state["snapshot"] = curr
    save_state(state)
    return state


def main():
    os.makedirs(os.path.dirname(config.LOG_PATH), exist_ok=True)
    # Terminal shows only actions (opens/closes/actual resizes, warnings, errors);
    # the file keeps the full DEBUG trace: poll heartbeat, skipped noise resizes, etc.
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    log_file = logging.FileHandler(config.LOG_PATH)
    log_file.setLevel(logging.DEBUG)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[console, log_file],
    )
    for noisy in ("urllib3", "requests", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logger.info("starting mirror bot (dry_run=%s, poll=%ss)", config.DRY_RUN, config.POLL_INTERVAL_S)

    portfolio_id = os.environ["PORTFOLIO_ID"]
    jwt_token = os.environ["INVOAPP_JWT"]
    # The websocket handler runs on its own thread and must only enqueue;
    # fills are dequeued and handled on this thread between polls. In dry-run
    # no orders rest on the exchange, so there are no fills to react to
    fills = queue.Queue()
    client = HLClient(on_fill=None if config.DRY_RUN else fills.put)
    state = load_state()
    if state is None:
        # Empty snapshot: every currently open target position shows up as an
        # "open" event on the first poll and gets mirrored
        state = {"snapshot": {}, "mirrored": {}}
        logger.info("no saved state at %s: existing target positions will be mirrored on first poll",
                    config.OPEN_POSITIONS_PATH)
    else:
        logger.info("loaded state from %s: %d tracked positions",
                    config.OPEN_POSITIONS_PATH, len(state["mirrored"]))
    if not config.DRY_RUN:
        # Dry-run never places orders, so tracked state and the exchange are
        # unrelated and comparing them would only produce false warnings
        reconcile(client, state)

    while True:
        try:
            # In dry-run mirrored positions never exist on the exchange, so
            # equity is unrelated to the bot's activity and the check is noise
            if not config.DRY_RUN and check_daily_loss(client, state):
                return
            state = poll_once(client, state, portfolio_id, jwt_token)
        except requests.HTTPError as e:
            if _jwt_expired(e):
                return
            logger.exception("invoapp request failed, retrying in %ss", config.RETRY_DELAY_S)
            time.sleep(config.RETRY_DELAY_S)
            try:
                state = poll_once(client, state, portfolio_id, jwt_token)
            except requests.HTTPError as e2:
                if _jwt_expired(e2):
                    return
                logger.exception("retry failed, waiting for next poll")
            except Exception:
                logger.exception("retry failed, waiting for next poll")
        except Exception:
            logger.exception("poll failed, retrying next poll")
        # Wait for the next poll, but wake within ~1s of a websocket fill so
        # TP/SL go out immediately instead of up to a full poll later. With no
        # fills this degrades to a plain sleep until the deadline
        deadline = time.monotonic() + config.POLL_INTERVAL_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                fill = fills.get(timeout=remaining)
            except queue.Empty:
                break
            try:
                handle_fill(client, state, fill)
            except Exception:
                logger.exception("failed to handle websocket fill %s", fill)


def _jwt_expired(e):
    if e.response is None or e.response.status_code != 401:
        return False
    msg = f"invoapp JWT expired or invalid - renew INVOAPP_JWT in {config.SECRETS_PATH} and restart"
    logger.error(msg)
    print(f"\n!!! {msg} !!!")
    return True


if __name__ == "__main__":
    main()
