"""CTrader Open API Client Wrapper for MT5→cTrader Copy Trading

Provides high-level trading methods wrapping the low-level OpenApiPy SDK.
"""
import os
import logging
from typing import Optional, Callable, Dict
from dotenv import load_dotenv

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAAccountAuthReq,
    ProtoOANewOrderReq,
    ProtoOAAmendPositionSLTPReq,
    ProtoOAClosePositionReq,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOASymbolsListReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAOrderType,
    ProtoOATradeSide,
)
from twisted.internet import reactor

logger = logging.getLogger(__name__)


class CTraderClient:
    """High-level wrapper for cTrader Open API trading operations."""
    
    def __init__(self, env: str = "demo"):
        """Initialize client.
        
        Args:
            env: "demo" or "live"
        """
        load_dotenv()
        
        self.client_id = os.getenv("CTRADER_CLIENT_ID")
        self.client_secret = os.getenv("CTRADER_CLIENT_SECRET")
        
        if not self.client_id or not self.client_secret:
            raise ValueError("CTRADER_CLIENT_ID and CTRADER_CLIENT_SECRET must be set in .env")
        
        # Set host based on environment
        if env == "live":
            self.host = EndPoints.PROTOBUF_LIVE_HOST
        else:
            self.host = EndPoints.PROTOBUF_DEMO_HOST
        
        self.port = EndPoints.PROTOBUF_PORT
        self.client = Client(self.host, self.port, TcpProtocol)
        
        self.is_connected = False
        self.is_app_authed = False
        self.is_account_authed = False
        self.account_id: Optional[int] = None
        self.access_token: Optional[str] = None

        # Dynamic symbol map for this account: NAME -> symbolId
        self.symbol_name_to_id: Dict[str, int] = {}

        # New: full symbol details for this account: symbolId -> ProtoOASymbol
        self.symbol_details: Dict[int, object] = {}
        
        # Callbacks
        self._on_connect_callback: Optional[Callable] = None
        self._on_message_callback: Optional[Callable] = None
        
        # Setup client callbacks
        self.client.setConnectedCallback(self._handle_connected)
        self.client.setDisconnectedCallback(self._handle_disconnected)
        self.client.setMessageReceivedCallback(self._handle_message)
        
        logger.info(f"CTraderClient initialized for {env} environment")
    
    def _handle_connected(self, client):
        """Internal: Handle connection event."""
        logger.info("Connected to cTrader Open API")
        self.is_connected = True
        
        # Authenticate application
        self._authenticate_app()
    
    def _handle_disconnected(self, client, reason):
        """Internal: Handle disconnection event."""
        logger.warning(f"Disconnected from cTrader: {reason}")
        self.is_connected = False
        self.is_app_authed = False
        self.is_account_authed = False
        self.symbol_name_to_id.clear()
        self.symbol_details.clear()
    
    def _handle_message(self, client, message):
        """Internal: Handle incoming messages."""
        msg_type = Protobuf.extract(message).payloadType
        logger.debug(f"Received message type: {msg_type}")
        
        if self._on_message_callback:
            self._on_message_callback(message)
    
    def _authenticate_app(self):
        """Internal: Authenticate the application."""
        logger.info("Authenticating application...")
        req = ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret
        d = self.client.send(req)
        d.addCallback(self._on_app_auth_success)
        d.addErrback(self._on_error)
    
    def _on_app_auth_success(self, result):
        """Internal: Handle successful app authentication."""
        logger.info("Application authenticated successfully")
        self.is_app_authed = True
        
        if self.account_id and self.account_id > 0:
            self._authorize_account()
        else:
            logger.warning(
                "No valid account credentials set - connection may close "
                f"(account_id={self.account_id}, access_token={'set' if self.access_token else 'not set'})"
            )
        
        if self._on_connect_callback:
            self._on_connect_callback()
    
    def _authorize_account(self):
        """Internal: Authorize the trading account after app auth."""
        logger.info(f"Authorizing account {self.account_id}...")
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = self.account_id
        req.accessToken = self.access_token or ""
        d = self.client.send(req)
        d.addCallback(self._on_account_auth_success)
        d.addErrback(self._on_error)
    
    def _on_account_auth_success(self, result):
        """Internal: Handle successful account authorization."""
        logger.info(f"Account {self.account_id} authorized successfully")
        self.is_account_authed = True

        # After account is authorized, load symbol map for this account
        self._load_symbol_map()
    
    def _load_symbol_map(self):
        """Fetch all symbols for this account and build NAME -> symbolId map."""
        if not self.account_id:
            return
        logger.info(f"Loading symbol map for account {self.account_id}...")
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self.account_id
        d = self.client.send(req)

        def _on_symbols_list(result):
            try:
                extracted = Protobuf.extract(result)
                count = 0
                self.symbol_name_to_id.clear()
                self.symbol_details.clear()
                # extracted.symbol is a repeated ProtoOASymbol
                for s in extracted.symbol:
                    name = s.symbolName.upper()
                    self.symbol_name_to_id[name] = s.symbolId
                    self.symbol_details[s.symbolId] = s
                    count += 1
                logger.info(f"Loaded {count} symbols for account {self.account_id}")
            except Exception as e:
                logger.error(f"Failed to build symbol map: {e}", exc_info=True)

        d.addCallback(_on_symbols_list)
        d.addErrback(self._on_error)

    def get_symbol_id_by_name(self, name: str) -> Optional[int]:
        """Get broker symbolId by symbol name (uppercased)."""
        return self.symbol_name_to_id.get(name.upper())
    
    def _on_error(self, failure):
        """Internal: Handle errors."""
        logger.error(f"Error: {failure}")
    
    def set_account_credentials(self, account_id: int, access_token: str):
        """Set account credentials for authorization.
        
        Must be called BEFORE connect() to authorize account automatically.
        """
        self.account_id = account_id
        self.access_token = access_token
        logger.info(f"Account credentials set: ID={account_id}")
    
    def connect(self, on_connect: Optional[Callable] = None):
        """Connect to cTrader and authenticate."""
        self._on_connect_callback = on_connect
        logger.info(f"Connecting to {self.host}:{self.port}...")
        self.client.startService()
    
    def set_message_callback(self, callback: Callable):
        """Register callback for all incoming messages."""
        self._on_message_callback = callback
    
    def authenticate_account(self, access_token: str):
        """Authenticate a trading account (legacy method)."""
        self.access_token = access_token
        logger.info("Authenticating account...")
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = int(access_token)  # Simplified for demo
        req.accessToken = access_token
        d = self.client.send(req)
        d.addErrback(self._on_error)
    
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
        """Send a market order."""
        if not self.is_app_authed:
            raise RuntimeError("Not authenticated. Call connect() first.")
        
        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = account_id
        req.symbolId = symbol_id
        req.orderType = ProtoOAOrderType.MARKET
        req.tradeSide = ProtoOATradeSide.BUY if side.lower() == "buy" else ProtoOATradeSide.SELL
        req.volume = volume
        
        if sl:
            req.stopLoss = sl
        if tp:
            req.takeProfit = tp
        
        req.label = label
        
        logger.info(f"Sending market order: {side} {volume} units of symbol {symbol_id}")
        d = self.client.send(req)

        def _on_order_response(result):
            try:
                extracted = Protobuf.extract(result)
                logger.info(f"Order response message: {extracted}")
            except Exception as e:
                logger.warning(f"Failed to extract order response: {e}; raw={result}")

        d.addCallback(_on_order_response)
        d.addErrback(self._on_error)
        return d
    
    def modify_position(
        self,
        account_id: int,
        position_id: int,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ):
        """Modify position SL/TP."""
        if not self.is_app_authed:
            raise RuntimeError("Not authenticated. Call connect() first.")
        
        req = ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = account_id
        req.positionId = position_id
        
        if sl:
            req.stopLoss = sl
        if tp:
            req.takeProfit = tp
        
        logger.info(f"Modifying position {position_id}: SL={sl}, TP={tp}")
        d = self.client.send(req)
        d.addErrback(self._on_error)
        return d
    
    def close_position(self, account_id: int, position_id: int, volume: int):
        """Close a position."""
        if not self.is_app_authed:
            raise RuntimeError("Not authenticated. Call connect() first.")
        
        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = account_id
        req.positionId = position_id
        req.volume = volume
        
        logger.info(f"Closing position {position_id}: {volume} units")
        d = self.client.send(req)
        d.addErrback(self._on_error)
        return d
    
    def run(self):
        """Start the Twisted reactor (blocking call)."""
        logger.info("Starting reactor...")
        reactor.run()
    
    def stop(self):
        """Stop the reactor and disconnect."""
        logger.info("Stopping reactor...")
        reactor.stop()
