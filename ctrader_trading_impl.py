#!/usr/bin/env python3
"""
Trading helpers extracted from ctrader_client.py.

Design goal: reduce ctrader_client.py size without breaking API/attribute names.
All functions operate on the CTraderClient instance ("self") and keep using:
  - self.is_account_authed
  - self.snap_volume_for_symbol(), self.round_price_for_symbol()
  - self.send(req)  (facade over low-level client.send)
  - self._on_error  (errback)
"""

import logging
from typing import Optional, Any

from ctrader_open_api import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOANewOrderReq,
    ProtoOAAmendPositionSLTPReq,
    ProtoOAClosePositionReq,
    ProtoOACancelOrderReq,   # <-- ADDED
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAOrderType,
    ProtoOATradeSide,
    ProtoOATimeInForce,
)

logger = logging.getLogger(__name__)


def _parse_mt5_ticket_from_label(label: str) -> Optional[int]:
    """
    Expected label format: 'MT5_<ticket>' (e.g., MT5_1468550799).
    Returns int ticket if parsable, else None.
    """
    if not label:
        return None
    s = str(label).strip()
    if not s.startswith("MT5_"):
        return None
    tail = s[4:]
    if not tail.isdigit():
        return None
    try:
        return int(tail)
    except Exception:
        return None


def amend_position(
    self,
    account_id: int,
    position_id: int,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    symbol_id: Optional[int] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
):
    # Keep compatibility keywords used by app_state
    if stop_loss is not None:
        sl = stop_loss
    if take_profit is not None:
        tp = take_profit

    return modify_position(
        self,
        account_id=account_id,
        position_id=position_id,
        sl=sl,
        tp=tp,
        symbol_id=symbol_id,
    )


def send_market_order(
    self,
    account_id: int,
    symbol_id: int,
    side: str,
    volume: int,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    label: str = "MT5_Copy",
):
    if not self.is_account_authed:
        raise RuntimeError("Account not authenticated yet")

    volume = self.snap_volume_for_symbol(symbol_id, volume)

    req = ProtoOANewOrderReq()
    req.ctidTraderAccountId = int(account_id)
    req.symbolId = int(symbol_id)
    req.orderType = ProtoOAOrderType.MARKET
    req.tradeSide = ProtoOATradeSide.BUY if side.lower() == "buy" else ProtoOATradeSide.SELL
    req.volume = int(volume)

    if sl is not None and float(sl) > 0.0:
        req.stopLoss = float(sl)
    if tp is not None and float(tp) > 0.0:
        req.takeProfit = float(tp)

    req.label = label

    logger.info("Sending market order: %s %s units of symbol %s", side, volume, symbol_id)

    d = self.send(req)

    def _on_resp(result):
        try:
            logger.info("Order response: %r", Protobuf.extract(result))
        except Exception:
            logger.warning("Order response (raw): %r", result)

    d.addCallback(_on_resp)
    d.addErrback(self._on_error)
    return d


def send_pending_order(
    self,
    account_id: int,
    symbol_id: int,
    side: str,
    volume: int,
    pending_type: str,
    stop_price: float = 0.0,
    limit_price: float = 0.0,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    label: str = "MT5_Pending",
    expiration_ms: int = 0,
):
    """
    Create pending order (LIMIT / STOP / STOP_LIMIT) via ProtoOANewOrderReq.
    """
    if not self.is_account_authed:
        raise RuntimeError("Account not authenticated yet")

    ptype = (pending_type or "").strip().lower()
    if ptype not in ("limit", "stop", "stop_limit"):
        raise ValueError(f"Unsupported pending_type: {pending_type}")

    volume = self.snap_volume_for_symbol(symbol_id, int(volume))

    # Round all prices to symbol precision
    stop_price = float(stop_price or 0.0)
    limit_price = float(limit_price or 0.0)
    if stop_price > 0:
        stop_price = self.round_price_for_symbol(symbol_id, stop_price)
    if limit_price > 0:
        limit_price = self.round_price_for_symbol(symbol_id, limit_price)
    if sl is not None and float(sl) > 0:
        sl = self.round_price_for_symbol(symbol_id, float(sl))
    if tp is not None and float(tp) > 0:
        tp = self.round_price_for_symbol(symbol_id, float(tp))

    req = ProtoOANewOrderReq()
    req.ctidTraderAccountId = int(account_id)
    req.symbolId = int(symbol_id)
    req.tradeSide = ProtoOATradeSide.BUY if side.lower() == "buy" else ProtoOATradeSide.SELL
    req.volume = int(volume)
    req.label = str(label)

    if ptype == "limit":
        if not (limit_price and float(limit_price) > 0.0):
            raise ValueError("LIMIT pending order requires limit_price > 0")
        req.orderType = ProtoOAOrderType.LIMIT
        req.limitPrice = float(limit_price)
    elif ptype == "stop":
        if not (stop_price and float(stop_price) > 0.0):
            raise ValueError("STOP pending order requires stop_price > 0")
        req.orderType = ProtoOAOrderType.STOP
        req.stopPrice = float(stop_price)
    else:
        if not (stop_price and float(stop_price) > 0.0):
            raise ValueError("STOP_LIMIT pending order requires stop_price > 0")
        if not (limit_price and float(limit_price) > 0.0):
            raise ValueError("STOP_LIMIT pending order requires limit_price > 0")
        req.orderType = ProtoOAOrderType.STOP_LIMIT
        req.stopPrice = float(stop_price)
        req.limitPrice = float(limit_price)

    if sl is not None and float(sl) > 0.0:
        req.stopLoss = float(sl)
    if tp is not None and float(tp) > 0.0:
        req.takeProfit = float(tp)

    if expiration_ms and int(expiration_ms) > 0:
        req.timeInForce = ProtoOATimeInForce.GOOD_TILL_DATE
        req.expirationTimestamp = int(expiration_ms)

    logger.info(
        "Sending pending order: type=%s side=%s vol=%s symbol=%s stop=%s limit=%s SL=%s TP=%s exp=%s label=%s",
        ptype,
        side,
        volume,
        symbol_id,
        stop_price,
        limit_price,
        sl,
        tp,
        int(expiration_ms or 0),
        label,
    )

    d = self.send(req)

    def _on_resp(result):
        try:
            extracted = Protobuf.extract(result)
            logger.info("Pending order response: %r", extracted)

            # OPTIONAL: if you want to update a map here, you can:
            # label = getattr(getattr(getattr(extracted, "order", None), "tradeData", None), "label", None)
            # order_id = getattr(getattr(extracted, "order", None), "orderId", None)
            # ticket = _parse_mt5_ticket_from_label(label or "")
            # if ticket and order_id:
            #     self.pending_order_ids_by_ticket[ticket] = int(order_id)

        except Exception:
            logger.warning("Pending order response (raw): %r", result)

    d.addCallback(_on_resp)
    d.addErrback(self._on_error)
    return d


def cancel_pending_order(self, account_id: int, order_id: int):
    """
    Cancel an existing pending order by cTrader orderId.
    """
    if not self.is_account_authed:
        raise RuntimeError("Account not authenticated yet")

    req = ProtoOACancelOrderReq()
    req.ctidTraderAccountId = int(account_id)
    req.orderId = int(order_id)

    logger.info("Cancelling pending orderId=%s on account %s", order_id, account_id)

    d = self.send(req)

    def _on_resp(result):
        try:
            logger.info("Cancel order response: %r", Protobuf.extract(result))
        except Exception:
            logger.warning("Cancel order response (raw): %r", result)

    d.addCallback(_on_resp)
    d.addErrback(self._on_error)
    return d


def modify_position(
    self,
    account_id: int,
    position_id: int,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    symbol_id: Optional[int] = None,
):
    if not self.is_account_authed:
        raise RuntimeError("Account not authenticated yet")

    orig_sl, orig_tp = sl, tp

    if sl is not None and float(sl) <= 0.0:
        sl = None
    if tp is not None and float(tp) <= 0.0:
        tp = None

    if symbol_id is not None:
        if sl is not None:
            sl = self.round_price_for_symbol(symbol_id, sl)
        if tp is not None:
            tp = self.round_price_for_symbol(symbol_id, tp)

    req = ProtoOAAmendPositionSLTPReq()
    req.ctidTraderAccountId = int(account_id)
    req.positionId = int(position_id)

    if sl is not None:
        req.stopLoss = float(sl)
    if tp is not None:
        req.takeProfit = float(tp)

    logger.info(
        "Modifying position %s: SL %s→%s, TP %s→%s",
        position_id,
        orig_sl,
        sl,
        orig_tp,
        tp,
    )

    d = self.send(req)

    def _on_resp(result):
        try:
            logger.info("Amend response: %r", Protobuf.extract(result))
        except Exception:
            logger.warning("Amend response (raw): %r", result)

    d.addCallback(_on_resp)
    d.addErrback(self._on_error)
    return d


def close_position(self, *args: Any, **kwargs: Any):
    """
    Compatible close.

    Requires:
      (account_id, position_id, volume[, symbol_id])
    Accepts alt keyword names:
      pos_id, position, qty, volume_cents
    """
    account_id = kwargs.get("account_id")
    position_id = kwargs.get("position_id", kwargs.get("pos_id", kwargs.get("position")))
    volume = kwargs.get("volume", kwargs.get("qty", kwargs.get("volume_cents")))
    symbol_id = kwargs.get("symbol_id")

    if account_id is None and len(args) >= 1:
        account_id = args[0]
    if position_id is None and len(args) >= 2:
        position_id = args[1]
    if volume is None and len(args) >= 3:
        volume = args[2]
    if symbol_id is None and len(args) >= 4:
        symbol_id = args[3]

    if account_id is None or position_id is None or volume is None:
        raise TypeError("close_position requires (account_id, position_id, volume[, symbol_id])")

    if not self.is_account_authed:
        raise RuntimeError("Account not authenticated yet")

    account_id = int(account_id)
    position_id = int(position_id)
    volume = int(volume)

    if symbol_id is not None:
        symbol_id = int(symbol_id)
        volume = self.snap_volume_for_symbol(symbol_id, volume)

    req = ProtoOAClosePositionReq()
    req.ctidTraderAccountId = account_id
    req.positionId = position_id
    req.volume = volume

    logger.info("Closing position %s: %s units", position_id, volume)

    d = self.send(req)
    d.addErrback(self._on_error)
    return d
