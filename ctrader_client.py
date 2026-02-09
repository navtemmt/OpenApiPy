#!/usr/bin/env python3
"""
CTrader Open API Client Wrapper for MT5→cTrader Copy Trading
Provides high-level trading methods wrapping the low-level OpenApiPy SDK.
"""

import os
import time
import logging
from typing import Optional, Callable, Dict

from dotenv import load_dotenv
from twisted.internet import reactor, task

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAAccountAuthReq,
    ProtoOANewOrderReq,
    ProtoOAAmendPositionSLTPReq,
    ProtoOAClosePositionReq,
    ProtoOASymbolsListReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAOrderType,
    ProtoOATradeSide,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class CTraderClient:
    """High-level wrapper for cTrader Open API trading operations."""

    def __init__(self, env: str = "demo"):
        """
        Args:
            env: "demo" or "live"
        """
        load_dotenv()

        self.client_id = os.getenv("CTRADER_CLIENT_ID")
        self.client_secret = os.getenv("CTRADER_CLIENT_SECRET")

        if not self.client_id or not self.client_secret:
            raise ValueError("CTRADER_CLIENT_ID and CTRADER_CLIENT_SECRET must be set in .env")

        # Host selection
        self.host = (
            EndPoints.PROTOBUF_LIVE_HOST
            if env == "live"
            else EndPoints.PROTOBUF_DEMO_HOST
        )
        self.port = EndPoints.PROTOBUF_PORT

        self.client = Client(self.host, self.port, TcpProtocol)

        self.is_connected = False
        self.is_app_authed = False
        self.is_account_authed = False

        self.account_id: Optional[int] = None
        self.access_token: Optional[str] = None

        # Symbol maps
        self.symbol_name_to_id: Dict[str, int] = {}
        self.symbol_details: Dict[int, object] = {}

        # Health monitoring
        self.heartbeat_task = None
        self.health_check_task = None
        self.heartbeat_interval = 30
        self.last_message_time = time.time()
        self.max_idle_time = 120

        # Callbacks
        self._on_connect_callback: Optional[Callable] = None
        self._on_message_callback: Optional[Callable] = None

        # Wire SDK callbacks
        self.client.setConnectedCallback(self._handle_connected)
        self.client.setDisconnectedCallback(self._handle_disconnected)
        self.client.setMessageReceivedCallback(self._handle_message)

        logger.info("CTraderClient initialized (%s)", env)

    # ------------------------------------------------------------------
    # Internal connection handlers
    # ------------------------------------------------------------------

    def _handle_connected(self, client):
        logger.info("Connected to cTrader Open API")
        self.is_connected = True
        self.last_message_time = time.time()

        self._authenticate_app()

        reactor.callLater(5, self._start_heartbeat)
        reactor.callLater(5, self._start_health_check)

    def _handle_disconnected(self, client, reason):
        logger.warning("Disconnected from cTrader: %s", reason)
        self.is_connected = False
        self.is_app_authed = False
        self.is_account_authed = False
        self.symbol_name_to_id.clear()
        self.symbol_details.clear()
        self._stop_periodic_tasks()

    def _handle_message(self, client, message):
        self.last_message_time = time.time()
        try:
            msg = Protobuf.extract(message)
            logger.debug("Received message type: %s", msg.payloadType)
        except Exception:
            logger.debug("Received raw message: %r", message)

        if self._on_message_callback:
            try:
                self._on_message_callback(message)
            except Exception:
                logger.exception("User message callback crashed")

    # ------------------------------------------------------------------
    # Heartbeat / health
    # ------------------------------------------------------------------

    def _start_heartbeat(self):
        if self.heartbeat_task is None or not self.heartbeat_task.running:
            self.heartbeat_task = task.LoopingCall(self._send_heartbeat)
            self.heartbeat_task.start(self.heartbeat_interval, now=False)
            logger.info("Heartbeat started")

    def _send_heartbeat(self):
        if self.is_connected and self.is_app_authed:
            logger.debug("Heartbeat OK")
        else:
            logger.debug("Heartbeat: not ready")

    def _start_health_check(self):
        if self.health_check_task is None or not self.health_check_task.running:
            self.health_check_task = task.LoopingCall(self._check_connection_health)
            self.health_check_task.start(30, now=False)
            logger.info("Health check started")

    def _check_connection_health(self):
        idle = time.time() - self.last_message_time
        if idle > self.max_idle_time:
            logger.warning("Connection idle for %.0fs", idle)

    def _stop_periodic_tasks(self):
        if self.heartbeat_task and self.heartbeat_task.running:
            self.heartbeat_task.stop()
            logger.info("Heartbeat stopped")
        if self.health_check_task and self.health_check_task.running:
            self.health_check_task.stop()
            logger.info("Health check stopped")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _authenticate_app(self):
        logger.info("Authenticating application...")
        req = ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret

        d = self.client.send(req)
        d.addCallback(self._on_app_auth_success)
        d.addErrback(self._on_error)

    def _on_app_auth_success(self, result):
        logger.info("Application authenticated successfully")
        self.is_app_authed = True

        if self.account_id and self.access_token:
            self._authorize_account()
        else:
            logger.warning(
                "Account credentials not set yet "
                "(call set_account_credentials before connect())"
            )

        if self._on_connect_callback:
            try:
                self._on_connect_callback()
            except Exception:
                logger.exception("on_connect callback crashed")

    def _authorize_account(self):
        logger.info("Authorizing account %s...", self.account_id)
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = int(self.account_id)
        req.accessToken = self.access_token

        d = self.client.send(req)
        d.addCallback(self._on_account_auth_success)
        d.addErrback(self._on_error)

    def _on_account_auth_success(self, result):
        logger.info("Account %s authorized successfully", self.account_id)
        self.is_account_authed = True
        self._load_symbol_map()

    # ------------------------------------------------------------------
    # Symbols
    # ------------------------------------------------------------------

    def _load_symbol_map(self):
        if not self.account_id:
            return

        logger.info("Loading symbols for account %s...", self.account_id)
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = int(self.account_id)

        d = self.client.send(req)
        d.addCallback(self._on_symbols_list)
        d.addErrback(self._on_error)

    def _on_symbols_list(self, result):
        try:
            msg = Protobuf.extract(result)

            # Correct field in Open API is usually: msg.symbol
            symbols = getattr(msg, "symbol", None)
            if not symbols:
                logger.error("SymbolsList response has no symbols field: %r", msg)
                return

            self.symbol_name_to_id.clear()
            self.symbol_details.clear()

            for s in symbols:
                name = s.symbolName.upper()
                self.symbol_name_to_id[name] = s.symbolId
                self.symbol_details[s.symbolId] = s

            logger.info("Loaded %d symbols", len(self.symbol_name_to_id))

        except Exception:
            logger.exception("Failed parsing symbols list")

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_symbol_id_by_name(self, name: str) -> Optional[int]:
        return self.symbol_name_to_id.get(name.upper())

    def round_price_for_symbol(self, symbol_id: int, price: float) -> float:
        symbol = self.symbol_details.get(symbol_id)
        digits = 4
        if symbol and hasattr(symbol, "digits"):
            try:
                digits = min(4, int(symbol.digits))
            except Exception:
                pass
        factor = 10 ** digits
        return round(price * factor) / factor

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def _on_error(self, failure):
        logger.error("Deferred error: %s", failure)
        try:
            failure.printTraceback()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_account_credentials(self, account_id: int, access_token: str):
        """
        Must be called BEFORE connect() if you want auto account auth.
        """
        self.account_id = int(account_id)
        self.access_token = access_token
        logger.info("Account credentials set: %s", account_id)

    def connect(self, on_connect: Optional[Callable] = None):
        self._on_connect_callback = on_connect
        logger.info("Connecting to %s:%s...", self.host, self.port)
        self.client.startService()

    def set_message_callback(self, callback: Callable):
        self._on_message_callback = callback

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

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

        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = int(account_id)
        req.symbolId = int(symbol_id)
        req.orderType = ProtoOAOrderType.MARKET
        req.tradeSide = (
            ProtoOATradeSide.BUY if side.lower() == "buy" else ProtoOATradeSide.SELL
        )
        req.volume = int(volume)

        if sl is not None:
            req.stopLoss = float(sl)
        if tp is not None:
            req.takeProfit = float(tp)

        req.label = label

        logger.info(
            "Sending market order: %s %s units of symbol %s",
            side, volume, symbol_id
        )

        d = self.client.send(req)

        def _on_resp(result):
            try:
                logger.info("Order response: %r", Protobuf.extract(result))
            except Exception:
                logger.warning("Order response (raw): %r", result)

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
            position_id, orig_sl, sl, orig_tp, tp
        )

        d = self.client.send(req)

        def _on_resp(result):
            try:
                logger.info("Amend response: %r", Protobuf.extract(result))
            except Exception:
                logger.warning("Amend response (raw): %r", result)

        d.addCallback(_on_resp)
        d.addErrback(self._on_error)
        return d

    def close_position(self, account_id: int, position_id: int, volume: int):
        if not self.is_account_authed:
            raise RuntimeError("Account not authenticated yet")

        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = int(account_id)
        req.positionId = int(position_id)
        req.volume = int(volume)

        logger.info("Closing position %s: %s units", position_id, volume)

        d = self.client.send(req)
        d.addErrback(self._on_error)
        return d

    # ------------------------------------------------------------------
    # Reactor control
    # ------------------------------------------------------------------

    def run(self):
        logger.info("Starting reactor...")
        if not reactor.running:
            reactor.run()

    def stop(self):
        logger.info("Stopping reactor...")
        self._stop_periodic_tasks()
        if reactor.running:
            reactor.stop()


# ----------------------------------------------------------------------
# Utility
# ----------------------------------------------------------------------

def convert_mt5_lots_to_ctrader_cents(
    mt5_lots: float,
    mt5_contract_size: float,
    mt5_volume_min: float,
    mt5_volume_step: float,
    lot_size_cents: int,
    min_volume_cents: int,
    max_volume_cents: int,
    step_volume_cents: int,
) -> int:
    """
    Convert MT5 lots to cTrader volume in *cents of units*.

    Returns:
        Volume in cents of units for cTrader
    """
    # 1) MT5 underlying units
    mt5_units = mt5_lots * mt5_contract_size

    if lot_size_cents <= 0:
        units_per_lot_ctrader = mt5_contract_size or 1.0
    else:
        units_per_lot_ctrader = lot_size_cents / 100.0

    # 2) Map MT5 units to cTrader lots
    if units_per_lot_ctrader <= 0:
        target_lots_ctrader = mt5_lots
    else:
        target_lots_ctrader = mt5_units / units_per_lot_ctrader

    # 3) Back to units → cents
    target_units = target_lots_ctrader * units_per_lot_ctrader
    target_cents = int(round(target_units * 100))

    # 4) Clamp
    if min_volume_cents and min_volume_cents > 0:
        target_cents = max(target_cents, min_volume_cents)
    if max_volume_cents and max_volume_cents > 0:
        target_cents = min(target_cents, max_volume_cents)

    # 5) Snap to step
    if step_volume_cents and step_volume_cents > 0:
        base = min_volume_cents if (min_volume_cents and min_volume_cents > 0) else 0
        steps = round((target_cents - base) / step_volume_cents)
        target_cents = base + int(steps) * step_volume_cents

    return max(target_cents, min_volume_cents or 0)
