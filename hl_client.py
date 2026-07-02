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
        raise OrderError(f"request failed: {result}")
    statuses = result["response"]["data"]["statuses"]
    for status in statuses:
        if "error" in status:
            raise OrderError(f"order rejected: {status['error']}")
    return statuses


class HLClient:
    def __init__(self):
        # PUBLIC_KEY = main wallet address; TESTNET_PRIVATE_KEY = API/agent wallet key
        self.address = os.environ["PUBLIC_KEY"]
        wallet = eth_account.Account.from_key(os.environ["TESTNET_PRIVATE_KEY"])
        self.info = Info(config.BASE_URL, skip_ws=True)
        self.exchange = Exchange(wallet, config.BASE_URL, account_address=self.address)
        self.assets = {a["name"]: a for a in self.info.meta()["universe"]}
        logger.info("connected to %s as %s (%d assets)", config.BASE_URL, self.address, len(self.assets))

    def is_listed(self, coin):
        return coin in self.assets

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
            raise OrderError(f"{coin}: size rounds to 0")
        side = "long" if is_buy else "short"
        if config.DRY_RUN:
            logger.info("DRY RUN: open %s %s size=%s lev=%sx", side, coin, size, leverage)
            return size
        self.exchange.update_leverage(leverage, coin)
        result = self.exchange.market_open(coin, is_buy, size, None, config.SLIPPAGE)
        logger.info("open %s %s size=%s lev=%sx -> %s", side, coin, size, leverage, result)
        _check(result)
        return size

    def increase_position(self, coin, is_buy, size):
        if config.DRY_RUN:
            logger.info("DRY RUN: increase %s by %s", coin, size)
            return
        result = self.exchange.market_open(coin, is_buy, size, None, config.SLIPPAGE)
        logger.info("increase %s by %s -> %s", coin, size, result)
        _check(result)

    def close_position(self, coin, size=None):
        """Market-close `size` of the coin's position (all of it when size is None)."""
        if config.DRY_RUN:
            logger.info("DRY RUN: close %s size=%s", coin, size or "all")
            return
        result = self.exchange.market_close(coin, size, None, config.SLIPPAGE)
        if result is None:  # SDK returns None when there is no position to close
            logger.warning("close %s: no position on exchange", coin)
            return
        logger.info("close %s size=%s -> %s", coin, size or "all", result)
        _check(result)

    def place_tpsl(self, coin, position_is_buy, size, tp_px=None, sl_px=None):
        """Reduce-only trigger orders guarding an open position. Returns resting oids."""
        oids = []
        for px, kind in ((tp_px, "tp"), (sl_px, "sl")):
            if not px:
                continue
            px = self.round_price(coin, float(px))
            order_type = {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": kind}}
            if config.DRY_RUN:
                logger.info("DRY RUN: %s trigger for %s at %s", kind, coin, px)
                continue
            result = self.exchange.order(coin, not position_is_buy, size, px, order_type, reduce_only=True)
            logger.info("%s trigger for %s at %s -> %s", kind, coin, px, result)
            for status in _check(result):
                if "resting" in status:
                    oids.append(status["resting"]["oid"])
        return oids

    def cancel_orders(self, coin, oids):
        for oid in oids:
            if config.DRY_RUN:
                logger.info("DRY RUN: cancel %s oid=%s", coin, oid)
                continue
            result = self.exchange.cancel(coin, oid)
            # A trigger may have already fired; log instead of raising
            if result.get("status") != "ok":
                logger.warning("cancel %s oid=%s failed: %s", coin, oid, result)
