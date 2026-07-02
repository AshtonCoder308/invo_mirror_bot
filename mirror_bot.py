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


def handle_open(client, state, trade):
    tid = str(trade["id"])
    coin = normalize_ticker(trade["ticker"])
    entry = {"coin": coin, "is_buy": trade["direction"] == "long", "mirrored": False,
             "hl_size": 0, "leverage": trade.get("leverage") or 1, "tpsl_oids": []}
    if not client.is_listed(coin):
        logger.warning("skip %s (%s, asset_type=%s): %s not listed on Hyperliquid",
                       tid, trade["ticker"], trade.get("asset_type"), coin)
    else:
        size = client.open_position(coin, entry["is_buy"], config.FIXED_MARGIN_USD, entry["leverage"])
        entry["hl_size"] = size
        entry["mirrored"] = True
        entry["tpsl_oids"] = client.place_tpsl(
            coin, entry["is_buy"], size, trade.get("price_target"), trade.get("stop_loss"))
        logger.info("OPEN mirrored: %s %s (invoapp trade %s)", coin, "long" if entry["is_buy"] else "short", tid)
    state["mirrored"][tid] = entry


def handle_close(client, state, trade):
    tid = str(trade["id"])
    entry = state["mirrored"].pop(tid, None)
    if not entry or not entry["mirrored"]:
        logger.info("CLOSE %s: was not mirrored, nothing to do", tid)
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
    ratio = new_trade["position_size"] / old_trade["position_size"]
    new_size = client.round_size(coin, entry["hl_size"] * ratio)
    delta = new_size - entry["hl_size"]
    if abs(delta) * client.mid(coin) < config.MIN_NOTIONAL_USD:
        logger.info("RESIZE %s: delta %s below minimum notional, skipped", coin, delta)
        return
    if delta > 0:
        client.increase_position(coin, entry["is_buy"], delta)
    else:
        client.close_position(coin, -delta)
    entry["hl_size"] = new_size
    logger.info("RESIZE mirrored: %s %s -> %s (invoapp trade %s)", coin, entry["hl_size"] - delta, new_size, tid)


def poll_once(client, state, portfolio_id, jwt_token):
    trades = fetch_open_trades(portfolio_id, jwt_token, size=100)
    curr = {str(t["id"]): t for t in trades}

    if state is None:
        # First poll after startup: record existing target positions as an in-memory
        # baseline and place no trades; only changes from here on are mirrored
        state = {"snapshot": curr, "mirrored": {
            str(t["id"]): {"coin": normalize_ticker(t["ticker"]), "is_buy": t["direction"] == "long",
                           "mirrored": False, "hl_size": 0, "leverage": t.get("leverage") or 1, "tpsl_oids": []}
            for t in trades}}
        logger.info("baseline recorded: %d existing target positions (not mirrored)", len(trades))
        return state

    opens, closes, resizes = diff_snapshots(state["snapshot"], curr)
    logger.info("poll: %d open target trades | events: %d open, %d close, %d resize",
                len(curr), len(opens), len(closes), len(resizes))

    for trade in opens:
        try:
            handle_open(client, state, trade)
        except OrderError:
            logger.exception("failed to mirror open of trade %s", trade["id"])
    for trade in closes:
        try:
            handle_close(client, state, trade)
        except OrderError:
            logger.exception("failed to mirror close of trade %s", trade["id"])
    for old_trade, new_trade in resizes:
        try:
            handle_resize(client, state, old_trade, new_trade)
        except OrderError:
            logger.exception("failed to mirror resize of trade %s", new_trade["id"])

    state["snapshot"] = curr
    return state


def main():
    os.makedirs(os.path.dirname(config.LOG_PATH), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(config.LOG_PATH)],
    )
    logger.info("starting mirror bot (dry_run=%s, poll=%ss)", config.DRY_RUN, config.POLL_INTERVAL_S)

    portfolio_id = os.environ["PORTFOLIO_ID"]
    jwt_token = os.environ["INVOAPP_JWT"]
    client = HLClient()
    state = None  # in-memory only; every start re-baselines on the first poll

    while True:
        try:
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
