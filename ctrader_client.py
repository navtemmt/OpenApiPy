#!/usr/bin/env python3
"""
CTrader Open API Client Wrapper for MT5→cTrader Copy Trading
Provides high-level trading methods wrapping the low-level OpenApiPy SDK.
"""

import os
import time
import logging
from typing import Optional, Callable, Dict, Any, Iterable, Set

from dotenv import load_dotenv
from twisted.internet import reactor

import ctrader_symbols_impl as symbols_impl
import ctrader_monitor_impl as monitor_impl
import ctrader_auth_impl as auth_impl
import ctrader_trading_impl as trading_impl

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOASubscribeSpotsReq,
    ProtoOAUnsubscribeSpotsReq,
    ProtoOASpotEvent,
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

        self.symbol_name_to_id: Dict[str, int] = {}
        self.symbol_details: Dict[int, object] = {}

        self.spot_quotes: Dict[int, Dict[str, Any]] = {}
        self._pending_spot_subscriptions: Set[int] = set()

        self.heartbeat_task = None
        self.health_check_task = None
        self.heartbeat_interval = 30
        self.last_message_time = time.time()
        self.max_idle_time = 120

        self._on_connect_callback: Optional[Callable] = None
        self._on_message_callback: Optional[Callable] = None

        self.client.setConnectedCallback(self._handle_connected)
        self.client.setDisconnectedCallback(self._handle_disconnected)
        self.client.setMessageReceivedCallback(self._handle_message)

        logger.info("CTraderClient initialized (%s)", env)

    # ------------------------------------------------------------------
    # Connection handlers
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
        self.spot_quotes.clear()
        self._stop_periodic_tasks()

    def _handle_message(self, client, message):
        self.last_message_time = time.time()

        extracted = None
        try:
            extracted = Protobuf.extract(message)
        except Exception:
            logger.debug("Raw message received (extract failed)")

        # Spot handling
        try:
            if isinstance(extracted, ProtoOASpotEvent):
                self._on_spot_event(extracted)
            else:
                inner = getattr(extracted, "payload", None)
                if isinstance(inner, ProtoOASpotEvent):
                    self._on_spot_event(inner)
        except Exception:
            logger.debug("Spot processing error", exc_info=True)

        if self._on_message_callback:
            try:
                self._on_message_callback(message)
            except Exception:
                logger.exception("User message callback crashed")

    def _on_spot_event(self, spot_event: ProtoOASpotEvent):
        updated = 0
        for s in getattr(spot_event, "spot", []):
            symbol_id = int(getattr(s, "symbolId", 0) or 0)
            if not symbol_id:
                continue

            bid = float(getattr(s, "bid", 0.0) or 0.0)
            ask = float(getattr(s, "ask", 0.0) or 0.0)
            ts = int(getattr(s, "timestamp", 0) or 0)

            self.spot_quotes[symbol_id] = {"bid": bid, "ask": ask, "ts": ts}
            updated += 1

        if updated:
            logger.info("Received %d spot updates", updated)

    # ------------------------------------------------------------------
    # Spot subscriptions (FIXED)
    # ------------------------------------------------------------------

    def subscribe_spots(self, account_id: int, symbol_ids: Iterable[int]):
        ids = {int(x) for x in symbol_ids if int(x) > 0}

        if not self.is_account_authed:
            logger.warning("Account not authorized yet — queuing spot subscription")
            self._pending_spot_subscriptions.update(ids)
            return

        if not ids:
            return

        req = ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = int(account_id)
        req.symbolId.extend(list(ids))

        logger.info("Sending spot subscription for %d symbols", len(ids))
        self.client.send(req)

        # diagnostic timeout check
        reactor.callLater(5, self._check_spot_stream_health, ids)

    def _check_spot_stream_health(self, ids: Set[int]):
        missing = [sid for sid in ids if sid not in self.spot_quotes]
        if missing:
            logger.warning(
                "No quotes received for symbol IDs %s after 5s (possible broker stream disabled)",
                missing,
            )

    def unsubscribe_spots(self, account_id: int, symbol_ids: Iterable[int]):
        ids = [int(x) for x in symbol_ids if int(x) > 0]
        if not ids:
            return

        req = ProtoOAUnsubscribeSpotsReq()
        req.ctidTraderAccountId = int(account_id)
        req.symbolId.extend(ids)
        self.client.send(req)

    def get_last_quote(self, symbol_id: int) -> Optional[Dict[str, Any]]:
        return self.spot_quotes.get(int(symbol_id))

    # ------------------------------------------------------------------
    # Called by auth layer AFTER account auth success
    # ------------------------------------------------------------------

    def _on_account_authenticated(self):
        if self._pending_spot_subscriptions:
            logger.info(
                "Processing %d queued spot subscriptions",
                len(self._pending_spot_subscriptions),
            )
            self.subscribe_spots(
                self.account_id,
                self._pending_spot_subscriptions,
            )
            self._pending_spot_subscriptions.clear()

    # ------------------------------------------------------------------
    # Authentication delegation
    # ------------------------------------------------------------------

    def _authenticate_app(self):
        return auth_impl.authenticate_app(self)

    def _on_app_auth_success(self, result):
        return auth_impl.on_app_auth_success(self, result)

    def _authorize_account(self):
        return auth_impl.authorize_account(self)

    def _on_account_auth_success(self, result):
        auth_impl.on_account_auth_success(self, result)
        self._on_account_authenticated()

    # ------------------------------------------------------------------
    # Symbols
    # ------------------------------------------------------------------

    def _load_symbol_map(self):
        return symbols_impl.load_symbol_map(self)

    def _on_symbols_list(self, result):
        return symbols_impl.on_symbols_list(self, result)

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
        return self.client.send(req)

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
