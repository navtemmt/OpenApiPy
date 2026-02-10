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
from twisted.internet import reactor

from ctrader_utils import convert_mt5_lots_to_ctrader_cents  # kept for compatibility
import ctrader_symbols_impl as symbols_impl
import ctrader_monitor_impl as monitor_impl
import ctrader_auth_impl as auth_impl
import ctrader_trading_impl as trading_impl

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints

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
    # Heartbeat / health (delegated to ctrader_monitor_impl.py)
    # ------------------------------------------------------------------

    def _start_heartbeat(self):
        return monitor_impl.start_heartbeat(self)

    def _send_heartbeat(self):
        return monitor_impl.send_heartbeat(self)

    def _start_health_check(self):
        return monitor_impl.start_health_check(self)

    def _check_connection_health(self):
        return monitor_impl.check_connection_health(self)

    def _stop_periodic_tasks(self):
        return monitor_impl.stop_periodic_tasks(self)

    # ------------------------------------------------------------------
    # Authentication (delegated to ctrader_auth_impl.py)
    # ------------------------------------------------------------------

    def _authenticate_app(self):
        return auth_impl.authenticate_app(self)

    def _on_app_auth_success(self, result):
        return auth_impl.on_app_auth_success(self, result)

    def _authorize_account(self):
        return auth_impl.authorize_account(self)

    def _on_account_auth_success(self, result):
        return auth_impl.on_account_auth_success(self, result)

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

    def send(self, req):
        """Facade for low-level client.send(req) to reduce coupling."""
        return self.client.send(req)

    # ------------------------------------------------------------------
    # Trading (delegated to ctrader_trading_impl.py)
    # ------------------------------------------------------------------

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
        return trading_impl.amend_position(
            self,
            account_id=account_id,
            position_id=position_id,
            sl=sl,
            tp=tp,
            symbol_id=symbol_id,
            stop_loss=stop_loss,
            take_profit=take_profit,
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
        return trading_impl.send_market_order(
            self,
            account_id=account_id,
            symbol_id=symbol_id,
            side=side,
            volume=volume,
            sl=sl,
            tp=tp,
            label=label,
        )

    def modify_position(
        self,
        account_id: int,
        position_id: int,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        symbol_id: Optional[int] = None,
    ):
        return trading_impl.modify_position(
            self,
            account_id=account_id,
            position_id=position_id,
            sl=sl,
            tp=tp,
            symbol_id=symbol_id,
        )

    def close_position(self, *args: Any, **kwargs: Any):
        return trading_impl.close_position(self, *args, **kwargs)

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
