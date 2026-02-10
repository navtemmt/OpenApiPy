#!/usr/bin/env python3
"""
CTrader Open API Client Wrapper for MT5→cTrader Copy Trading
Provides high-level trading methods wrapping the low-level OpenApiPy SDK.
"""

import os
import time
import logging
from typing import Optional, Callable, Dict, Any

from dotenv import load_dotenv
from twisted.internet import reactor, task

from ctrader_utils import convert_mt5_lots_to_ctrader_cents  # kept for compatibility
import ctrader_symbols_impl as symbols_impl

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAAccountAuthReq,
    ProtoOANewOrderReq,
    ProtoOAAmendPositionSLTPReq,
    ProtoOAClosePositionReq,
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
        load_dotenv()

        self.client_id = os.getenv("CTRADER_CLIENT_ID")
        self.client_secret = os.getenv("CTRADER_CLIENT_SECRET")

        if not self.client_id or not self.client_secret:
            raise ValueError("CTRADER_CLIENT_ID and CTRADER_CLIENT_SECRET must be set in .env")

        self.host = EndPoints.PROTOBUF_LIVE_HOST if env == "live" else EndPoints.PROTOBUF_DEMO_HOST
        self.port = EndPoints.PROTOBUF_PORT

        self.client = Client(self.host, self.port, TcpProtocol)

        self.is_connected = False
        self.is_app_authed = False
        self.is_account_authed = False

        self.account_id: Optional[int] = None
        self.access_token: Optional[str] = None

        # Symbol maps (populated after account auth)
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
            logger.debug("Received message type: %s", getattr(msg, "payloadType", None))
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
                "Account credentials not set yet (call set_account_credentials before connect())"
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
    # Symbols (delegated to ctrader_symbols_impl.py)
    # ------------------------------------------------------------------

    def _load_symbol_map(self):
        return symbols_impl.load_symbol_map(self)

    def _on_symbols_list(self, result):
        return symbols_impl.on_symbols_list(self, result)

    # ------------------------------------------------------------------
    # Public helpers (delegated to ctrader_symbols_impl.py)
    # ------------------------------------------------------------------

    def get_symbol_id_by_name(self, name: str) -> Optional[int]:
        return symbols_impl.get_symbol_id_by_name(self, name)

    def round_price_for_symbol(self, symbol_id: int, price: float) -> float:
        return symbols_impl.round_price_for_symbol(self, symbol_id, price)

    def snap_volume_for_symbol(self, symbol_id: int, volume_cents: int) -> int:
        return symbols_impl.snap_volume_for_symbol(self, symbol_id, volume_cents)

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

    def amend_position(
        self,
        account_id: int,
        position_id: int,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        symbol_id: Optional[int] = None,
        # compatibility keywords used by app_state:
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ):
        if stop_loss is not None:
            sl = stop_loss
        if take_profit is not None:
            tp = take_profit

        return self.modify_position(
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

        d = self.client.send(req)

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
