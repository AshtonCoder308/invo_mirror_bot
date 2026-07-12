import logging
import os

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

import config

logger = logging.getLogger(__name__)


class OrderError(Exception):
    pass


def _check(result):
    """Raise on failure; an 'ok' response can still carry per-order errors."""
    if result.get("status") != "ok":
        raise OrderError(f"Request failed {result}")
    statuses = result["response"]["data"]["statuses"]
    for status in statuses:
        if "error" in status:
            raise OrderError(f"Order rejected {status['error']}")
    return statuses


class HLClient:
    def __init__(self, on_fill=None):
        # PUBLIC_KEY = main wallet address; TESTNET_PRIVATE_KEY = API/agent wallet key
        self.address = os.environ["PUBLIC_KEY"]
        wallet = eth_account.Account.from_key(os.environ["TESTNET_PRIVATE_KEY"])
        # The websocket is only needed to push fills to on_fill; without a
        # callback, skip it entirely
        self.info = Info(config.BASE_URL, skip_ws=on_fill is None)
        self.exchange = Exchange(wallet, config.BASE_URL, account_address=self.address)
        self.assets = {a["name"]: a for a in self.info.meta()["universe"]}
        if on_fill is not None:
            self._subscribe_fills(on_fill)
        logger.info(f"Connected to {config.BASE_URL}")

    def _subscribe_fills(self, on_fill):
        def handler(msg):
            data = msg.get("data") or {}
            if data.get("isSnapshot"):
                # Historical fills replayed on (re)connect, not new activity
                return
            for fill in data.get("fills", []):
                # Runs on the websocket thread: on_fill must only enqueue,
                # never touch client or bot state
                on_fill(fill)

        self.info.subscribe({"type": "userFills", "user": self.address}, handler)

    def is_listed(self, coin):
        return coin in self.assets

    def open_orders(self):
        """Oids of every order currently resting on the account."""
        return {o["oid"] for o in self.info.open_orders(self.address)}

    def open_positions(self):
        """Coin -> signed size for every position actually open on the account."""
        state = self.info.user_state(self.address)
        positions = {}
        for ap in state["assetPositions"]:
            szi = float(ap["position"]["szi"])
            if szi:
                positions[ap["position"]["coin"]] = szi
        return positions

    def position_breakdown(self):
        """Coin -> margin used, notional and unrealized PnL for every open
        position; lets the bot strip foreign positions out of its capital."""
        state = self.info.user_state(self.address)
        breakdown = {}
        for ap in state["assetPositions"]:
            pos = ap["position"]
            if float(pos["szi"]):
                breakdown[pos["coin"]] = {"margin": float(pos["marginUsed"]),
                                          "notional": float(pos["positionValue"]),
                                          "upnl": float(pos["unrealizedPnl"])}
        return breakdown

    def margin_summary(self):
        """(available, total_notional, margin_used). available is the unified
        account's spendable USDC - the spot balance's total minus hold, which
        is exactly the UI's "Available balance": the exchange already deducts
        everything committed to positions (isolated buckets, cross usage,
        resting-order holds) via the hold field."""
        ms = self.info.user_state(self.address)["marginSummary"]
        available = sum(float(b["total"]) - float(b["hold"])
                        for b in self.info.spot_user_state(self.address)["balances"]
                        if b["coin"] == "USDC")
        return (available, float(ms["totalNtlPos"]), float(ms["totalMarginUsed"]))

    def mid(self, coin):
        return float(self.info.all_mids()[coin])

    def round_size(self, coin, sz):
        return round(sz, self.assets[coin]["szDecimals"])

    def round_price(self, coin, px):
        # Perp prices: max 5 significant figures and at most 6 - szDecimals decimals
        px = float(f"{px:.5g}")
        return round(px, 6 - self.assets[coin]["szDecimals"])

    def open_position(self, coin, is_buy, margin_usd, leverage):
        """Market-open with margin_usd collateral at the given leverage. Returns filled size."""
        leverage = min(leverage or 1, config.MAX_LEVERAGE, self.assets[coin]["maxLeverage"])
        size = self.round_size(coin, margin_usd * leverage / self.mid(coin))
        if size <= 0:
            raise OrderError(f"{coin} size rounds to 0")
        side = "long" if is_buy else "short"
        if config.DRY_RUN:
            logger.info(f"DRY RUN Open {coin} {side} size={size} lev={leverage}x")
            return size
        # is_cross=False: bot positions run on isolated margin so each trade's
        # risk is contained to its own bucket and never the shared balance
        self.exchange.update_leverage(leverage, coin, is_cross=False)
        result = self.exchange.market_open(coin, is_buy, size, None, config.SLIPPAGE)
        logger.debug(f"Open {coin} response {result}")
        _check(result)
        logger.info(f"Open {coin} {side} size={size} lev={leverage}x")
        return size

    def increase_position(self, coin, is_buy, size):
        if config.DRY_RUN:
            logger.info(f"DRY RUN Increase {coin} size={size}")
            return
        result = self.exchange.market_open(coin, is_buy, size, None, config.SLIPPAGE)
        logger.debug(f"Increase {coin} response {result}")
        _check(result)
        logger.info(f"Increase {coin} size={size}")

    def close_position(self, coin, size=None):
        """Market-close `size` of the coin's position (all of it when size is None)."""
        if config.DRY_RUN:
            logger.info(f"DRY RUN Close {coin} size={size or 'all'}")
            return
        result = self.exchange.market_close(coin, size, None, config.SLIPPAGE)
        if result is None:  # SDK returns None when there is no position to close
            logger.warning(f"Close {coin} no position on exchange")
            return
        logger.debug(f"Close {coin} response {result}")
        _check(result)
        logger.info(f"Close {coin} size={size or 'all'}")

    def place_entry_trigger(self, coin, is_buy, size, trigger_px, leverage):
        """Resting trigger order that market-opens when price reaches trigger_px.
        tpsl='tp' triggers on a fall for buys and a rise for sells, which is the
        better-price direction for an entry. Returns the resting oid."""
        leverage = min(leverage or 1, config.MAX_LEVERAGE, self.assets[coin]["maxLeverage"])
        trigger_px = self.round_price(coin, trigger_px)
        # Market execution on trigger is still slippage-capped by the limit px
        limit_px = self.round_price(
            coin, trigger_px * (1 + config.SLIPPAGE if is_buy else 1 - config.SLIPPAGE))
        side = "long" if is_buy else "short"
        if config.DRY_RUN:
            logger.info(f"DRY RUN Entry trigger {coin} {side} size={size} "
                        f"trigger={trigger_px} lev={leverage}x")
            return -1
        # is_cross=False: bot positions run on isolated margin so each trade's
        # risk is contained to its own bucket and never the shared balance
        self.exchange.update_leverage(leverage, coin, is_cross=False)
        order_type = {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": "tp"}}
        result = self.exchange.order(coin, is_buy, size, limit_px, order_type, reduce_only=False)
        logger.debug(f"Entry trigger {coin} response {result}")
        for status in _check(result):
            if "resting" in status:
                logger.info(f"Entry trigger {coin} {side} size={size} "
                            f"trigger={trigger_px} lev={leverage}x")
                return status["resting"]["oid"]
        raise OrderError(f"Entry trigger {coin} did not rest {result}")

    def place_tpsl(self, coin, position_is_buy, size, tp_px=None, sl_px=None):
        """Reduce-only trigger orders guarding an open position. Returns resting oids."""
        oids = []
        for px, kind in ((tp_px, "tp"), (sl_px, "sl")):
            if not px:
                continue
            px = self.round_price(coin, float(px))
            order_type = {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": kind}}
            if config.DRY_RUN:
                logger.info(f"DRY RUN {kind.upper()} trigger {coin} px={px}")
                continue
            result = self.exchange.order(coin, not position_is_buy, size, px, order_type, reduce_only=True)
            logger.debug(f"{kind.upper()} trigger {coin} response {result}")
            for status in _check(result):
                if "resting" in status:
                    logger.info(f"{kind.upper()} trigger {coin} px={px}")
                    oids.append(status["resting"]["oid"])
        return oids

    def cancel_orders(self, coin, oids):
        for oid in oids:
            if config.DRY_RUN:
                logger.info(f"DRY RUN Cancel {coin} oid={oid}")
                continue
            result = self.exchange.cancel(coin, oid)
            # A trigger may have already fired; log instead of raising
            if result.get("status") != "ok":
                logger.warning(f"Cancel {coin} oid={oid} failed {result}")
