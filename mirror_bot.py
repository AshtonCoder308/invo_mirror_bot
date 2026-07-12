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
    resizes are deliberately not mirrored"""
    opens = [t for tid, t in curr.items() if tid not in prev]
    closes = [t for tid, t in prev.items() if tid not in curr]
    return opens, closes


def allocate_margin(account_value, free_margin, total_notional, leverage):
    """Risk-checked margin for a new trade, or None when the trade is rejected.

    Each trade gets an equal slice of account value (account_value /
    MAX_OPEN_TRADES); when free margin can't cover a full slice, whatever is
    left gets invested instead"""
    margin = min(account_value / config.MAX_OPEN_TRADES, free_margin)
    notional = min(margin * leverage,
                   config.MAX_POSITION_NOTIONAL_USD,
                   account_value * config.MAX_ACCOUNT_LEVERAGE - total_notional)
    if notional < config.MIN_NOTIONAL_USD:
        return None
    return notional / leverage


def adjust_for_foreign(account_value, total_notional, margin_used, breakdown, foreign_coins):
    """Capital figures with foreign positions stripped out, so the bot only
    plays with what isn't tied up in positions it didn't open. Foreign trades
    must run on isolated margin: an isolated position's marginUsed is its own
    margin balance and already absorbs its unrealized PnL, so subtracting it
    removes both the position's capital and its PnL swings in one term."""
    f_margin = sum(breakdown[c]["margin"] for c in foreign_coins)
    f_notional = sum(breakdown[c]["notional"] for c in foreign_coins)
    bot_av = account_value - f_margin
    bot_free = bot_av - (margin_used - f_margin)  # == the account's real free margin
    return bot_av, bot_free, total_notional - f_notional


def tracked_coins(state):
    return {e["coin"] for e in state["mirrored"].values() if e["mirrored"]}


def handle_open(client, state, trade):
    tid = str(trade["id"])
    coin = normalize_ticker(trade["ticker"])
    if tid in state["mirrored"]:
        # Already mirrored in a previous run whose snapshot wasn't saved yet
        logger.debug(f"OPEN {coin} already tracked, skipping")
        return
    entry = {"coin": coin, "is_buy": trade["direction"] == "long", "mirrored": False,
             "hl_size": 0, "leverage": trade.get("leverage") or 1, "tpsl_oids": [],
             "entry_oid": None, "tp_px": trade.get("price_target"),
             "sl_px": trade.get("stop_loss")}
    # Track the entry before any order goes out: poll_once persists state even
    # when a handler raises, so a failure mid-open must not leave an order or
    # position on the exchange untracked
    state["mirrored"][tid] = entry
    if coin in config.IGNORED_COINS:
        logger.info(f"OPEN {coin} in IGNORED_COINS, skipped")
        return
    if not client.is_listed(coin):
        logger.warning(f"OPEN {coin} not listed on Hyperliquid, skipped")
        return
    szi = client.open_positions().get(coin)
    if szi:
        # Never touch a position the bot didn't open, in either direction:
        # warn and leave it alone (mirrored stays False)
        logger.warning(f"OPEN {coin} existing position size={szi} not opened "
                       "by bot, resolve manually")
        return
    lev = min(entry["leverage"], config.MAX_LEVERAGE)
    account_value, total_notional, margin_used = client.margin_summary()
    breakdown = client.position_breakdown()
    foreign = set(breakdown) - tracked_coins(state)
    bot_av, bot_free, bot_notional = adjust_for_foreign(
        account_value, total_notional, margin_used, breakdown, foreign)
    margin = allocate_margin(bot_av, bot_free, bot_notional, lev)
    if margin is None:
        logger.warning(f"OPEN {coin} rejected by risk limits "
                       f"(capital={bot_av:.2f} free={bot_free:.2f} "
                       f"notional={bot_notional:.2f})")
        return
    # Enter at ENTRY_IMPROVEMENT beyond the target's entry, in our favor
    # (long: below, short: above)
    improve = 1 - config.ENTRY_IMPROVEMENT if entry["is_buy"] else 1 + config.ENTRY_IMPROVEMENT
    trigger_px = trade["entry_price"] * improve
    mid = client.mid(coin)
    already_better = mid <= trigger_px if entry["is_buy"] else mid >= trigger_px
    side = "long" if entry["is_buy"] else "short"
    if already_better:
        # Price is already past the trigger - the exchange would reject the
        # trigger order as "would trigger immediately". Take the even better
        # entry at market and guard it right away
        size = client.open_position(coin, entry["is_buy"], margin, lev)
        entry["hl_size"] = size
        entry["mirrored"] = True
        entry["tpsl_oids"] = client.place_tpsl(coin, entry["is_buy"], size,
                                               entry["tp_px"], entry["sl_px"])
        logger.info(f"OPEN {coin} {side} at market size={size} "
                    f"(mid {mid} past trigger {trigger_px})")
        return
    # Rest the trigger; mirrored stays False until it fills - TP/SL are
    # reduce-only and need a position to exist first
    size = client.round_size(coin, margin * lev / trigger_px)
    if size <= 0:
        raise OrderError(f"{coin} size rounds to 0")
    entry["entry_oid"] = client.place_entry_trigger(coin, entry["is_buy"], size, trigger_px, lev)
    logger.info(f"OPEN {coin} {side} trigger placed size={size} trigger={trigger_px}")


def handle_close(client, state, trade):
    tid = str(trade["id"])
    entry = state["mirrored"].pop(tid, None)
    if not entry:
        logger.debug(f"CLOSE {normalize_ticker(trade['ticker'])} not tracked, nothing to do")
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
                logger.warning(f"CLOSE {entry['coin']} entry trigger fired "
                               "between polls, position closed")
                return
        logger.info(f"CLOSE {entry['coin']} entry trigger cancelled (never filled)")
        return
    if not entry["mirrored"]:
        logger.debug(f"CLOSE {entry['coin']} was not mirrored, nothing to do")
        return
    # In dry-run mirrored positions never exist on the exchange, so the check
    # would always trip
    if not config.DRY_RUN and entry["coin"] not in client.open_positions():
        # Already closed on the exchange (TP/SL fired, manual close): the
        # account already matches the target - just clean up leftover triggers
        client.cancel_orders(entry["coin"], entry["tpsl_oids"])
        logger.warning(f"CLOSE {entry['coin']} already closed on exchange, "
                       "state cleaned up")
        return
    client.cancel_orders(entry["coin"], entry["tpsl_oids"])
    client.close_position(entry["coin"], entry["hl_size"])
    logger.info(f"CLOSE {entry['coin']} size={entry['hl_size']}")


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
    pending = [e for e in state["mirrored"].values()
               if not e["mirrored"] and e.get("entry_oid") is not None]
    if not pending:
        return
    resting = client.open_orders()
    positions = client.open_positions()
    for entry in pending:
        if entry["entry_oid"] in resting:
            continue  # price hasn't reached the trigger yet
        try:
            szi = positions.get(entry["coin"])
            if szi and (szi > 0) == entry["is_buy"]:
                promote_entry(client, entry, abs(szi))
                side = "long" if entry["is_buy"] else "short"
                logger.info(f"ENTRY {entry['coin']} {side} filled size={entry['hl_size']}")
            else:
                entry["entry_oid"] = None
                logger.warning(f"ENTRY {entry['coin']} trigger gone without a fill "
                               "(cancelled externally?), stays unmirrored")
        except OrderError as e:
            logger.error(f"ENTRY {entry['coin']} promote failed: {e}")
        save_state(state)


def handle_fill(client, state, fill):
    """Websocket fill dequeued on the main thread: promote the matching entry
    within ~1s instead of waiting for the next poll's backstop sweep."""
    oid = fill.get("oid")
    entry = next((e for e in state["mirrored"].values()
                  if not e["mirrored"] and e.get("entry_oid") == oid), None)
    if entry is None:
        # TP/SL executions and manual trades land here; the poll-time
        # close/reconcile logic picks those up
        logger.debug(f"Fill {fill.get('coin')} oid={oid} matches no pending entry, ignored")
        return
    try:
        # The actual position beats the fill's sz (a merged or partial fill
        # would make them differ)
        szi = client.open_positions().get(entry["coin"])
        promote_entry(client, entry, abs(szi) if szi else float(fill["sz"]))
        side = "long" if entry["is_buy"] else "short"
        logger.info(f"ENTRY {entry['coin']} {side} filled (ws) size={entry['hl_size']}")
    finally:
        save_state(state)


def retry_unmirrored(state):
    """Drop unmirrored entries with no resting trigger (risk-rejected, skipped,
    vanished orders) from state at startup: the first poll then replays them
    through handle_open, which re-applies every guard under fresh conditions.
    Entries with a resting trigger or an open position are left alone."""
    retry = [tid for tid, e in state["mirrored"].items()
             if not e["mirrored"] and e.get("entry_oid") is None]
    for tid in retry:
        del state["mirrored"][tid]
        state["snapshot"].pop(tid, None)
    if retry:
        save_state(state)
        logger.info(f"Retrying {len(retry)} unmirrored trades on first poll")


def reconcile(client, state):
    """Warn when tracked mirrored positions and actual exchange positions
    diverge. Untracked exchange positions are handled separately by
    confirm_foreign_positions."""
    exchange = client.open_positions()
    for coin in sorted(tracked_coins(state) - set(exchange)):
        logger.warning(f"Reconcile {coin} tracked but missing on exchange "
                       "(TP/SL fired or closed manually?)")


def confirm_foreign_positions(client, state, ask=input):
    """List positions on the exchange that the bot didn't open and have the
    user confirm they are to be ignored: they are never traded, and the bot's
    capital is what remains after their equity. Returns False to abort startup."""
    breakdown = client.position_breakdown()
    foreign = set(breakdown) - tracked_coins(state)
    if not foreign:
        return True
    sizes = client.open_positions()
    print("\nPositions on the exchange not opened by the bot:")
    for coin in sorted(foreign):
        b = breakdown[coin]
        print(f"  {coin:8} size={sizes[coin]} notional=${b['notional']:.2f} "
              f"margin=${b['margin']:.2f} uPnL=${b['upnl']:+.2f}")
    account_value, total_notional, margin_used = client.margin_summary()
    bot_av, bot_free, _ = adjust_for_foreign(
        account_value, total_notional, margin_used, breakdown, foreign)
    print(f"Bot trading capital: ${bot_av:.2f} (free margin available: ${bot_free:.2f})")
    print("NOTE: foreign positions must use ISOLATED margin.")
    answer = ask("Ignore these positions and trade only with the remaining capital? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        logger.info("Foreign positions not confirmed, exiting")
        return False
    logger.info(f"Ignoring foreign positions {sorted(foreign)}, "
                f"bot capital {bot_av:.2f}")
    return True


def poll_once(client, state, portfolio_id, jwt_token):
    trades = fetch_open_trades(portfolio_id, jwt_token, size=100)
    curr = {str(t["id"]): t for t in trades}

    logger.info(f"Poll {len(curr)} open target trades")
    # Backstop for the websocket: promote any entry trigger that filled since
    # the last poll before diffing, so a same-poll close sees it as mirrored.
    # In dry-run orders never rest on the exchange, so there's nothing to sweep
    if not config.DRY_RUN:
        check_pending_entries(client, state)
    opens, closes = diff_snapshots(state["snapshot"], curr)
    logger.debug(f"Events {len(opens)} open, {len(closes)} close")

    for trade in opens:
        try:
            handle_open(client, state, trade)
        except OrderError as e:
            logger.error(f"OPEN {normalize_ticker(trade['ticker'])} failed: {e}")
        save_state(state)
    for trade in closes:
        try:
            handle_close(client, state, trade)
        except OrderError as e:
            logger.error(f"CLOSE {normalize_ticker(trade['ticker'])} failed: {e}")
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
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[console, log_file],
    )
    for noisy in ("urllib3", "requests", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logger.info(f"Starting mirror bot dry_run={config.DRY_RUN} poll={config.POLL_INTERVAL_S}s")

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
        logger.info(f"No saved state at {config.OPEN_POSITIONS_PATH}, existing "
                    "target positions will be mirrored on first poll")
    else:
        logger.info(f"Loaded {len(state['mirrored'])} tracked positions")
        retry_unmirrored(state)
    if not config.DRY_RUN:
        # Dry-run never places orders, so tracked state and the exchange are
        # unrelated and comparing them would only produce false warnings
        reconcile(client, state)
        if not confirm_foreign_positions(client, state):
            return

    while True:
        try:
            state = poll_once(client, state, portfolio_id, jwt_token)
        except requests.HTTPError as e:
            if _jwt_expired(e):
                return
            logger.exception(f"Invoapp request failed, retrying in {config.RETRY_DELAY_S}s")
            time.sleep(config.RETRY_DELAY_S)
            try:
                state = poll_once(client, state, portfolio_id, jwt_token)
            except requests.HTTPError as e2:
                if _jwt_expired(e2):
                    return
                logger.exception("Retry failed, waiting for next poll")
            except Exception:
                logger.exception("Retry failed, waiting for next poll")
        except Exception:
            logger.exception("Poll failed, retrying next poll")
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
                logger.exception(f"Fill {fill.get('coin')} handling failed")


def _jwt_expired(e):
    if e.response is None or e.response.status_code != 401:
        return False
    msg = f"Invoapp JWT expired or invalid - renew INVOAPP_JWT in {config.SECRETS_PATH} and restart"
    logger.error(msg)
    print(f"\n!!! {msg} !!!")
    return True


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Mirror bot interrupted, remember positions still open on exchange")