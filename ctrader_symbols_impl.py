#!/usr/bin/env python3
"""
Symbol-related helpers extracted from ctrader_client.py.

NOTE:
On this broker/API feed, ProtoOASymbolsListReq response does NOT include lotSize/minVolume/maxVolume/stepVolume.
It only contains identification and descriptive fields (verified by ListFields dump).
So this module only maintains:
  - name -> symbolId map
  - symbolId -> symbol object (basic metadata)
"""

import logging
from typing import Optional

from ctrader_open_api import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASymbolsListReq

logger = logging.getLogger(__name__)


def load_symbol_map(self, debug_dump: bool = False) -> None:
    if not getattr(self, "account_id", None):
        return

    logger.info("Loading symbols for account %s...", self.account_id)
    req = ProtoOASymbolsListReq()
    req.ctidTraderAccountId = int(self.account_id)

    d = self.client.send(req)
    d.addCallback(lambda result: on_symbols_list(self, result, debug_dump=debug_dump))
    d.addErrback(self._on_error)


def on_symbols_list(self, result, debug_dump: bool = False) -> None:
    try:
        msg = Protobuf.extract(result)
        symbols = getattr(msg, "symbol", None)
        if not symbols:
            logger.error("SymbolsList response has no symbols field: %r", msg)
            return

        self.symbol_name_to_id.clear()
        self.symbol_details.clear()

        for s in symbols:
            try:
                name = (getattr(s, "symbolName", "") or "").upper()
                sid = int(getattr(s, "symbolId", 0) or 0)
                if not name or not sid:
                    continue

                self.symbol_name_to_id[name] = sid
                self.symbol_details[sid] = s

                if debug_dump and name in ("EURAUD", "XAUUSD"):
                    try:
                        if hasattr(s, "ListFields"):
                            fields = [(f.name, v) for f, v in s.ListFields()]
                            logger.info("DBG SYMBOL %s id=%s fields=%s", name, sid, fields)
                        else:
                            logger.info("DBG SYMBOL %s id=%s repr=%r", name, sid, s)
                    except Exception as e:
                        logger.info("DBG SYMBOL dump failed for %s: %s", name, e)

            except Exception:
                continue

        logger.info("Loaded %d symbols", len(self.symbol_name_to_id))
    except Exception:
        logger.exception("Failed parsing symbols list")


def get_symbol_id_by_name(self, name: str) -> Optional[int]:
    return self.symbol_name_to_id.get((name or "").upper())


def get_symbol_category_id(self, symbol_id: int) -> Optional[int]:
    s = self.symbol_details.get(int(symbol_id))
    if s is None:
        return None
    try:
        return int(getattr(s, "symbolCategoryId", 0) or 0) or None
    except Exception:
        return None


def round_price_for_symbol(self, symbol_id: int, price: float) -> float:
    # digits also not present in your dump, so keep conservative rounding here
    # (you can improve later if you obtain digits elsewhere)
    return float(price)
