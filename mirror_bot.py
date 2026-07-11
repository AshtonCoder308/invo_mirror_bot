import json
import logging
import os
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
    """Compare trade-id-keyed snapshots. Returns (opens, closes, resizes);
    resizes are (old_trade, new_trade) pairs."""
    opens = [t for tid, t in curr.items() if tid not in prev]
    closes = [t for tid, t in prev.items() if tid not in curr]
    resizes = [
        (prev[tid], t)
        for tid, t in curr.items()
        if tid in prev
        and prev[tid].get("position_size")
        and t.get("position_size") != prev[tid].get("position_size")
    ]
    return opens, closes, resizes


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
             "target_size": trade["position_size"]}
    # Track the entry before any order goes out: poll_once persists state even
    # when a handler raises, so a failure after open_position (e.g. TP/SL
    # rejected) must not leave an open exchange position untracked
    state["mirrored"][tid] = entry
    if not client.is_listed(coin):
        logger.warning("skip %s (%s, asset_type=%s): %s not listed on Hyperliquid",
                       tid, trade["ticker"], trade.get("asset_type"), coin)
    else:
        szi = client.open_positions().get(coin)
        if szi and (szi > 0) == entry["is_buy"]:
            # Exchange already holds this coin in the right direction (lost
            # state file, manual trade, ...): adopt it instead of doubling up -
            # a second order would just merge into the same position anyway
            entry["hl_size"] = abs(szi)
            entry["mirrored"] = True
            entry["tpsl_oids"] = client.place_tpsl(
                coin, entry["is_buy"], entry["hl_size"],
                trade.get("price_target"), trade.get("stop_loss"))
            logger.warning("OPEN %s: adopted existing %s position (size %s) instead of opening",
                           tid, coin, szi)
        elif szi:
            # Exchange holds the coin in the wrong direction: never trade over
            # it, just warn and leave it alone (mirrored stays False)
            logger.warning("OPEN %s: exchange holds %s in the opposite direction (size %s), "
                           "skipping - resolve manually", tid, coin, szi)
        else:
            lev = min(entry["leverage"], config.MAX_LEVERAGE)
            account_value, total_notional, margin_used = client.margin_summary()
            margin = allocate_margin(account_value, account_value - margin_used, total_notional, lev)
            if margin is None:
                logger.warning("OPEN %s: rejected by risk limits - no capital left for %s "
                               "(account %.2f, free %.2f, open notional %.2f)",
                               tid, coin, account_value, account_value - margin_used, total_notional)
            else:
                size = client.open_position(coin, entry["is_buy"], margin, lev)
                entry["hl_size"] = size
                entry["mirrored"] = True
                entry["tpsl_oids"] = client.place_tpsl(
                    coin, entry["is_buy"], size, trade.get("price_target"), trade.get("stop_loss"))
                logger.info("OPEN mirrored: %s %s margin=%.2f (invoapp trade %s)",
                            coin, "long" if entry["is_buy"] else "short", margin, tid)


def handle_close(client, state, trade):
    tid = str(trade["id"])
    entry = state["mirrored"].pop(tid, None)
    if not entry or not entry["mirrored"]:
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


def handle_resize(client, state, old_trade, new_trade):
    tid = str(new_trade["id"])
    entry = state["mirrored"].get(tid)
    if not entry or not entry["mirrored"]:
        return
    coin = entry["coin"]
    # Compare against the target size we last scaled to, not the previous poll:
    # positionSize is mark-to-market, so poll-to-poll it always differs a little.
    # Noise oscillates around the baseline; deliberate scaling accumulates past it.
    baseline = entry.setdefault("target_size", old_trade["position_size"])
    ratio = new_trade["position_size"] / baseline
    if abs(ratio - 1) < config.RESIZE_THRESHOLD:
        logger.debug("RESIZE %s: %.2f%% change below threshold, treated as price noise",
                     coin, (ratio - 1) * 100)
        return
    new_size = client.round_size(coin, entry["hl_size"] * ratio)
    delta = new_size - entry["hl_size"]
    mid = client.mid(coin)
    if abs(delta) * mid < config.MIN_NOTIONAL_USD:
        logger.debug("RESIZE %s: delta %s below minimum notional, skipped", coin, delta)
        return
    if delta > 0:
        # An increase adds exposure, so it obeys the same notional caps as an
        # open, plus what free margin can actually collateralize at the
        # position's leverage; clamp rather than skip so the mirror tracks as
        # closely as allowed
        account_value, total_notional, margin_used = client.margin_summary()
        lev = min(entry["leverage"], config.MAX_LEVERAGE)
        headroom = min(config.MAX_POSITION_NOTIONAL_USD - entry["hl_size"] * mid,
                       account_value * config.MAX_ACCOUNT_LEVERAGE - total_notional,
                       (account_value - margin_used) * lev)
        if delta * mid > headroom:
            clamped = client.round_size(coin, max(headroom, 0.0) / mid)
            logger.warning("RESIZE %s: increase clamped by risk caps (%s -> %s)",
                           coin, delta, clamped)
            delta = clamped
            if delta * mid < config.MIN_NOTIONAL_USD:
                # Nothing sendable survives the caps; still adopt the new
                # baseline so the same increase isn't retried every poll
                entry["target_size"] = new_trade["position_size"]
                return
            new_size = entry["hl_size"] + delta
        client.increase_position(coin, entry["is_buy"], delta)
    else:
        client.close_position(coin, -delta)
    entry["hl_size"] = new_size
    entry["target_size"] = new_trade["position_size"]
    logger.info("RESIZE mirrored: %s %s -> %s (invoapp trade %s)", coin, entry["hl_size"] - delta, new_size, tid)


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
                       "not managed by the bot, opens on this coin will halt",
                       coin, exchange[coin])
    for coin in sorted(tracked - set(exchange)):
        logger.warning("reconcile: tracked %s position missing on exchange "
                       "(TP/SL fired or closed manually?)", coin)


def poll_once(client, state, portfolio_id, jwt_token):
    trades = fetch_open_trades(portfolio_id, jwt_token, size=100)
    curr = {str(t["id"]): t for t in trades}

    logger.info("poll: %d open target trades", len(curr))
    opens, closes, resizes = diff_snapshots(state["snapshot"], curr)
    logger.debug("events: %d open, %d close, %d resize", len(opens), len(closes), len(resizes))

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
    for old_trade, new_trade in resizes:
        try:
            handle_resize(client, state, old_trade, new_trade)
        except OrderError:
            logger.exception("failed to mirror resize of trade %s", new_trade["id"])
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
    client = HLClient()
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
        time.sleep(config.POLL_INTERVAL_S)


def _jwt_expired(e):
    if e.response is None or e.response.status_code != 401:
        return False
    msg = f"invoapp JWT expired or invalid - renew INVOAPP_JWT in {config.SECRETS_PATH} and restart"
    logger.error(msg)
    print(f"\n!!! {msg} !!!")
    return True


if __name__ == "__main__":
    main()
