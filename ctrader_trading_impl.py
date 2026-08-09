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
    ProtoOAAmendOrderReq,
    ProtoOAAmendPositionSLTPReq,
    ProtoOAClosePositionReq,
    ProtoOACancelOrderReq,
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


def _read_attr_or_key(obj, name: str, default=None):
    if obj is None:
        return default
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


def _first_non_empty(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _resolve_symbol_name_from_id(self, symbol_id: Optional[int]) -> Optional[str]:
    if symbol_id is None:
        return None

    try:
        symbol = self.symbol_details.get(int(symbol_id)) if hasattr(self, "symbol_details") else None
        name = getattr(symbol, "symbolName", None) if symbol is not None else None
        if name:
            return str(name)
    except Exception:
        pass

    try:
        broker_symbol_map = getattr(self, "symbol_name_to_id", {}) or {}
        for name, sid in broker_symbol_map.items():
            if int(sid) == int(symbol_id):
                return str(name)
    except Exception:
        pass

    return None


def _extract_response_context(
    self,
    extracted,
    fallback_account_id: Optional[int] = None,
    fallback_symbol_id: Optional[int] = None,
    fallback_label: Optional[str] = None,
):
    order = _read_attr_or_key(extracted, "order", None)
    position = _read_attr_or_key(extracted, "position", None)
    deal = _read_attr_or_key(extracted, "deal", None)

    order_trade_data = _read_attr_or_key(order, "tradeData", None)
    position_trade_data = _read_attr_or_key(position, "tradeData", None)

    symbol_id = _first_non_empty(
        _read_attr_or_key(order_trade_data, "symbolId", None),
        _read_attr_or_key(position_trade_data, "symbolId", None),
        _read_attr_or_key(deal, "symbolId", None),
        fallback_symbol_id,
    )

    label = _first_non_empty(
        _read_attr_or_key(order_trade_data, "label", None),
        _read_attr_or_key(position_trade_data, "label", None),
        fallback_label,
    )

    ticket = _parse_mt5_ticket_from_label(label) if label else None
    symbol_name = _resolve_symbol_name_from_id(self, symbol_id) if symbol_id is not None else None

    return {
        "account_id": _first_non_empty(
            _read_attr_or_key(extracted, "ctidTraderAccountId", None),
            fallback_account_id,
        ),
        "ticket": ticket,
        "symbol_id": symbol_id,
        "symbol_name": symbol_name,
        "label": label,
        "execution_type": _read_attr_or_key(extracted, "executionType", None),
        "order_id": _read_attr_or_key(order, "orderId", None),
        "position_id": _read_attr_or_key(position, "positionId", None),
        "deal_id": _read_attr_or_key(deal, "dealId", None),
    }


def _format_context(context: dict) -> str:
    parts = []

    if context.get("account_id") is not None:
        parts.append(f"account_id={context['account_id']}")
    if context.get("ticket") is not None:
        parts.append(f"ticket={context['ticket']}")
    if context.get("symbol_name"):
        parts.append(f"symbol={context['symbol_name']}")
    if context.get("symbol_id") is not None:
        parts.append(f"symbolId={context['symbol_id']}")
    if context.get("position_id") is not None:
        parts.append(f"positionId={context['position_id']}")
    if context.get("order_id") is not None:
        parts.append(f"orderId={context['order_id']}")
    if context.get("deal_id") is not None:
        parts.append(f"dealId={context['deal_id']}")
    if context.get("execution_type"):
        parts.append(f"execType={context['execution_type']}")
    if context.get("label"):
        parts.append(f"label={context['label']}")

    return " ".join(parts)


def _normalize_pending_type(pending_type: str) -> str:
    ptype = str(pending_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "limit": "limit",
        "stop": "stop",
        "stop_limit": "stop_limit",
        "stoplimit": "stop_limit",
        "buy_limit": "limit",
        "sell_limit": "limit",
        "buylimit": "limit",
        "selllimit": "limit",
        "buy_stop": "stop",
        "sell_stop": "stop",
        "buystop": "stop",
        "sellstop": "stop",
        "buy_stop_limit": "stop_limit",
        "sell_stop_limit": "stop_limit",
        "buystoplimit": "stop_limit",
        "sellstoplimit": "stop_limit",
    }
    return aliases.get(ptype, ptype)


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

    ticket = _parse_mt5_ticket_from_label(label)
    symbol_name = _resolve_symbol_name_from_id(self, symbol_id) or "UNKNOWN"

    logger.info(
        "Sending market order: account_id=%s ticket=%s symbol=%s symbolId=%s side=%s volume=%s sl=%s tp=%s label=%s",
        account_id,
        ticket,
        symbol_name,
        symbol_id,
        side,
        volume,
        sl,
        tp,
        label,
    )

    d = self.send(req)

    def _on_resp(result):
        try:
            extracted = Protobuf.extract(result)
            context = _extract_response_context(
                self,
                extracted,
                fallback_account_id=account_id,
                fallback_symbol_id=symbol_id,
                fallback_label=label,
            )
            logger.info("Order response: %s\n%s", _format_context(context), extracted)
        except Exception:
            logger.warning(
                "Order response (raw): account_id=%s ticket=%s symbol=%s symbolId=%s label=%s raw=%r",
                account_id,
                ticket,
                symbol_name,
                symbol_id,
                label,
                result,
            )

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

    ptype = _normalize_pending_type(pending_type)
    if ptype not in ("limit", "stop", "stop_limit"):
        raise ValueError(f"Unsupported pending_type: {pending_type}")

    volume = self.snap_volume_for_symbol(symbol_id, int(volume))

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

    ticket = _parse_mt5_ticket_from_label(label)
    symbol_name = _resolve_symbol_name_from_id(self, symbol_id) or "UNKNOWN"

    logger.info(
        "Sending pending order: account_id=%s ticket=%s symbol=%s symbolId=%s type=%s side=%s vol=%s stop=%s limit=%s SL=%s TP=%s exp=%s label=%s",
        account_id,
        ticket,
        symbol_name,
        symbol_id,
        ptype,
        side,
        volume,
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
            context = _extract_response_context(
                self,
                extracted,
                fallback_account_id=account_id,
                fallback_symbol_id=symbol_id,
                fallback_label=label,
            )
            logger.info("Pending order response: %s\n%s", _format_context(context), extracted)
        except Exception:
            logger.warning(
                "Pending order response (raw): account_id=%s ticket=%s symbol=%s symbolId=%s label=%s raw=%r",
                account_id,
                ticket,
                symbol_name,
                symbol_id,
                label,
                result,
            )

    d.addCallback(_on_resp)
    d.addErrback(self._on_error)
    return d


def amend_pending_order(
    self,
    account_id: int,
    order_id: int,
    symbol_id: int,
    side: str,
    volume: int,
    pending_type: str,
    stop_price: Optional[float] = None,
    limit_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    expiration_ms: Optional[int] = None,
):
    """
    Amend an existing pending order via ProtoOAAmendOrderReq.
    cTrader Open API supports amending pending orders with ProtoOAAmendOrderReq. [web:307][web:319]
    """
    if not self.is_account_authed:
        raise RuntimeError("Account not authenticated yet")

    ptype = _normalize_pending_type(pending_type)
    if ptype not in ("limit", "stop", "stop_limit"):
        raise ValueError(f"Unsupported pending_type: {pending_type}")

    account_id = int(account_id)
    order_id = int(order_id)
    symbol_id = int(symbol_id)
    volume = self.snap_volume_for_symbol(symbol_id, int(volume))

    stop_price = float(stop_price or 0.0)
    limit_price = float(limit_price or 0.0)

    if stop_price > 0:
        stop_price = self.round_price_for_symbol(symbol_id, stop_price)
    else:
        stop_price = None

    if limit_price > 0:
        limit_price = self.round_price_for_symbol(symbol_id, limit_price)
    else:
        limit_price = None

    if stop_loss is not None and float(stop_loss) > 0:
        stop_loss = self.round_price_for_symbol(symbol_id, float(stop_loss))
    else:
        stop_loss = None

    if take_profit is not None and float(take_profit) > 0:
        take_profit = self.round_price_for_symbol(symbol_id, float(take_profit))
    else:
        take_profit = None

    req = ProtoOAAmendOrderReq()
    req.ctidTraderAccountId = account_id
    req.orderId = order_id
    req.volume = int(volume)

    if ptype == "limit":
        if limit_price is None:
            raise ValueError("LIMIT amend requires limit_price > 0")
        req.orderType = ProtoOAOrderType.LIMIT
        req.limitPrice = float(limit_price)
    elif ptype == "stop":
        if stop_price is None:
            raise ValueError("STOP amend requires stop_price > 0")
        req.orderType = ProtoOAOrderType.STOP
        req.stopPrice = float(stop_price)
    else:
        if stop_price is None:
            raise ValueError("STOP_LIMIT amend requires stop_price > 0")
        if limit_price is None:
            raise ValueError("STOP_LIMIT amend requires limit_price > 0")
        req.orderType = ProtoOAOrderType.STOP_LIMIT
        req.stopPrice = float(stop_price)
        req.limitPrice = float(limit_price)

    req.tradeSide = ProtoOATradeSide.BUY if str(side).lower() == "buy" else ProtoOATradeSide.SELL
    req.symbolId = symbol_id

    if stop_loss is not None:
        req.stopLoss = float(stop_loss)
    if take_profit is not None:
        req.takeProfit = float(take_profit)

    if expiration_ms is not None and int(expiration_ms) > 0:
        req.timeInForce = ProtoOATimeInForce.GOOD_TILL_DATE
        req.expirationTimestamp = int(expiration_ms)

    symbol_name = _resolve_symbol_name_from_id(self, symbol_id) or "UNKNOWN"

    logger.info(
        "Amending pending order: account_id=%s orderId=%s symbol=%s symbolId=%s type=%s side=%s vol=%s stop=%s limit=%s SL=%s TP=%s exp=%s",
        account_id,
        order_id,
        symbol_name,
        symbol_id,
        ptype,
        side,
        volume,
        stop_price,
        limit_price,
        stop_loss,
        take_profit,
        int(expiration_ms or 0) if expiration_ms is not None else 0,
    )

    d = self.send(req)

    def _on_resp(result):
        try:
            extracted = Protobuf.extract(result)
            context = _extract_response_context(
                self,
                extracted,
                fallback_account_id=account_id,
                fallback_symbol_id=symbol_id,
            )
            if context.get("order_id") is None:
                context["order_id"] = int(order_id)
            logger.info("Amend pending order response: %s\n%s", _format_context(context), extracted)
        except Exception:
            logger.warning(
                "Amend pending order response (raw): account_id=%s orderId=%s symbol=%s symbolId=%s raw=%r",
                account_id,
                order_id,
                symbol_name,
                symbol_id,
                result,
            )

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

    logger.info("Cancelling pending order: account_id=%s orderId=%s", account_id, order_id)

    d = self.send(req)

    def _on_resp(result):
        try:
            extracted = Protobuf.extract(result)
            context = _extract_response_context(
                self,
                extracted,
                fallback_account_id=account_id,
            )
            if context.get("order_id") is None:
                context["order_id"] = int(order_id)
            logger.info("Cancel order response: %s\n%s", _format_context(context), extracted)
        except Exception:
            logger.warning(
                "Cancel order response (raw): account_id=%s orderId=%s raw=%r",
                account_id,
                order_id,
                result,
            )

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

    symbol_name = _resolve_symbol_name_from_id(self, symbol_id) if symbol_id is not None else None

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
        "Modifying position: account_id=%s positionId=%s symbol=%s symbolId=%s SL %s→%s TP %s→%s",
        account_id,
        position_id,
        symbol_name,
        symbol_id,
        orig_sl,
        sl,
        orig_tp,
        tp,
    )

    d = self.send(req)

    def _on_resp(result):
        try:
            extracted = Protobuf.extract(result)
            context = _extract_response_context(
                self,
                extracted,
                fallback_account_id=account_id,
                fallback_symbol_id=symbol_id,
            )
            if context.get("position_id") is None:
                context["position_id"] = int(position_id)
            logger.info("Amend response: %s\n%s", _format_context(context), extracted)
        except Exception:
            logger.warning(
                "Amend response (raw): account_id=%s positionId=%s symbol=%s symbolId=%s raw=%r",
                account_id,
                position_id,
                symbol_name,
                symbol_id,
                result,
            )

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

    symbol_name = None
    if symbol_id is not None:
        symbol_id = int(symbol_id)
        symbol_name = _resolve_symbol_name_from_id(self, symbol_id)
        volume = self.snap_volume_for_symbol(symbol_id, volume)

    req = ProtoOAClosePositionReq()
    req.ctidTraderAccountId = account_id
    req.positionId = position_id
    req.volume = volume

    logger.info(
        "Closing position: account_id=%s positionId=%s symbol=%s symbolId=%s volume=%s",
        account_id,
        position_id,
        symbol_name,
        symbol_id,
        volume,
    )

    d = self.send(req)

    def _on_resp(result):
        try:
            extracted = Protobuf.extract(result)
            context = _extract_response_context(
                self,
                extracted,
                fallback_account_id=account_id,
                fallback_symbol_id=symbol_id,
            )
            if context.get("position_id") is None:
                context["position_id"] = int(position_id)
            logger.info("Close response: %s\n%s", _format_context(context), extracted)
        except Exception:
            logger.warning(
                "Close response (raw): account_id=%s positionId=%s symbol=%s symbolId=%s volume=%s raw=%r",
                account_id,
                position_id,
                symbol_name,
                symbol_id,
                volume,
                result,
            )

    d.addCallback(_on_resp)
    d.addErrback(self._on_error)
    return d
