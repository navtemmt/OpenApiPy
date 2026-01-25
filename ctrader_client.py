"""CTrader Open API Client Wrapper for MT5→cTrader Copy Trading

Provides high-level trading methods wrapping the low-level OpenApiPy SDK.
"""
import os
import sys
import logging
from typing import Optional, Callable
from dotenv import load_dotenv

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAAccountAuthReq,
    ProtoOANewOrderReq,
    ProtoOAAmendPositionSLTPReq,
    ProtoOAClosePositionReq,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAOrderType,
    ProtoOATradeSide,
)
from twisted.internet import reactor

logger = logging.getLogger(__name__)


class CTraderClient:
    """High-level wrapper for cTrader Open API trading operations."""
    
    def __init__(self, env="demo"):
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
        self.account_id = None
        self.access_token = None
        
        # Callbacks
        self._on_connect_callback = None
        self._on_message_callback = None
        
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
    
    def _handle_message(self, client, message):
        """Internal: Handle incoming messages."""
        msg_type = Protobuf.extract(message).payloadType
        
        # Log all messages for debugging
        logger.debug(f"Received message type: {msg_type}")
        
        # Call user callback if registered
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
        
        if self._on_connect_callback:
            self._on_connect_callback()
    
    def _on_error(self, failure):
        """Internal: Handle errors."""
        logger.error(f"Error: {failure}")
    
    def connect(self, on_connect: Optional[Callable] = None):
        """Connect to cTrader and authenticate.
        
        Args:
            on_connect: Callback to execute after successful connection and auth
        """
        self._on_connect_callback = on_connect
        logger.info(f"Connecting to {self.host}:{self.port}...")
        self.client.startService()
    
    def set_message_callback(self, callback: Callable):
        """Register callback for all incoming messages.
        
        Args:
            callback: Function to call with each message
        """
        self._on_message_callback = callback
    
    def authenticate_account(self, access_token: str):
        """Authenticate a trading account.
        
        Args:
            access_token: OAuth access token for the account
        """
        self.access_token = access_token
        logger.info("Authenticating account...")
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = int(access_token)  # Simplified for demo
        req.accessToken = access_token
        d = self.client.send(req)
        d.addErrback(self._on_error)
    
    def send_market_order(self, account_id: int, symbol_id: int, side: str, 
                         volume: int, sl: Optional[float] = None, 
                         tp: Optional[float] = None, label: str = "MT5_Copy"):
        """Send a market order.
        
        Args:
            account_id: cTrader account ID
            symbol_id: Symbol ID from cTrader
            side: "buy" or "sell"
            volume: Volume in units (not lots)
            sl: Stop loss price (optional)
            tp: Take profit price (optional)
            label: Order label/comment
        
        Returns:
            Deferred that fires when order response received
        """
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
        d.addErrback(self._on_error)
        return d
    
    def modify_position(self, account_id: int, position_id: int, 
                       sl: Optional[float] = None, tp: Optional[float] = None):
        """Modify position SL/TP.
        
        Args:
            account_id: cTrader account ID
            position_id: Position ID to modify
            sl: New stop loss price (optional)
            tp: New take profit price (optional)
        
        Returns:
            Deferred that fires when response received
        """
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
        """Close a position.
        
        Args:
            account_id: cTrader account ID
            position_id: Position ID to close
            volume: Volume to close (units)
        
        Returns:
            Deferred that fires when response received
        """
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
