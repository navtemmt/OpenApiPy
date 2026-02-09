\"\"\"CTrader Open API Client Wrapper for MT5→cTrader Copy Trading
Provides high-level trading methods wrapping the low-level OpenApiPy SDK.
\"\"\"
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
from twisted.internet import reactor, task
import time

logger = logging.getLogger(__name__)


class CTraderClient:
    \"\"\"High-level wrapper for cTrader Open API trading operations.\"\"\"
    
    def __init__(self, env: str = \"demo\"):
        \"\"\"Initialize client.
        
        Args:
            env: \"demo\" or \"live\"
        \"\"\"
        load_dotenv()
        
        self.client_id = os.getenv(\"CTRADER_CLIENT_ID\")
        self.client_secret = os.getenv(\"CTRADER_CLIENT_SECRET\")
        
        if not self.client_id or not self.client_secret:
            raise ValueError(\"CTRADER_CLIENT_ID and CTRADER_CLIENT_SECRET must be set in .env\")
        
        # Set host based on environment
        if env == \"live\":
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

        # Dynamic symbol map for this account: NAME -&gt; symbolId
        self.symbol_name_to_id: Dict[str, int] = {}
        # Full symbol details for this account: symbolId -&gt; ProtoOASymbol
        self.symbol_details: Dict[int, object] = {}
        
        # Heartbeat and health check for long-running connections
        self.heartbeat_task = None
        self.health_check_task = None
        self.heartbeat_interval = 30
        self.last_message_time = time.time()
        self.max_idle_time = 120
        
        # Callbacks
        self._on_connect_callback: Optional[Callable] = None
        self._on_message_callback: Optional[Callable] = None
        
        # Setup client callbacks
        self.client.setConnectedCallback(self._handle_connected)
        self.client.setDisconnectedCallback(self._handle_disconnected)
        self.client.setMessageReceivedCallback(self._handle_message)
        
        logger.info(f\"CTraderClient initialized for {env} environment\")
    
    def _handle_connected(self, client):
        \"\"\"Internal: Handle connection event.\"\"\"
        logger.info(\"Connected to cTrader Open API\")
        self.is_connected = True
        self.last_message_time = time.time()
        
        # Authenticate application
        self._authenticate_app()
        
        # Start monitoring tasks
        reactor.callLater(5, self._start_heartbeat)
        reactor.callLater(5, self._start_health_check)
    
    def _handle_disconnected(self, client, reason):
        \"\"\"Internal: Handle disconnection event.\"\"\"
        logger.warning(f\"Disconnected from cTrader: {reason}\")
        self.is_connected = False
        self.is_app_authed = False
        self.is_account_authed = False
        self.symbol_name_to_id.clear()
        self.symbol_details.clear()
        
        # Stop periodic tasks
        self._stop_periodic_tasks()
    
    def _handle_message(self, client, message):
        \"\"\"Internal: Handle incoming messages.\"\"\"
        self.last_message_time = time.time()
        msg_type = Protobuf.extract(message).payloadType
        logger.debug(f\"Received message type: {msg_type}\")
        
        if self._on_message_callback:
            self._on_message_callback(message)
    
    def _start_heartbeat(self):
        \"\"\"Start heartbeat to keep connection alive.\"\"\"
        if self.heartbeat_task is None or not self.heartbeat_task.running:
            self.heartbeat_task = task.LoopingCall(self._send_heartbeat)
            self.heartbeat_task.start(self.heartbeat_interval)
            logger.info(\"Heartbeat started\")
    
    def _send_heartbeat(self):
        \"\"\"Send heartbeat check.\"\"\"
        try:
            if self.is_connected and self.is_app_authed:
                logger.debug(\"Heartbeat OK\")
            else:
                logger.warning(\"Heartbeat: not ready\")
        except Exception as e:
            logger.error(f\"Heartbeat error: {e}\")
    
    def _start_health_check(self):
        \"\"\"Start health check watchdog.\"\"\"
        if self.health_check_task is None or not self.health_check_task.running:
            self.health_check_task = task.LoopingCall(self._check_connection_health)
            self.health_check_task.start(30)
            logger.info(\"Health check started\")
    
    def _check_connection_health(self):
        \"\"\"Check if connection is still alive based on message activity.\"\"\"
        idle_time = time.time() - self.last_message_time
        if idle_time &gt; self.max_idle_time:
            logger.warning(f\"Connection idle for {idle_time:.0f}s\")
    
    def _stop_periodic_tasks(self):
        \"\"\"Stop all periodic tasks.\"\"\"
        if self.heartbeat_task and self.heartbeat_task.running:
            self.heartbeat_task.stop()
            logger.info(\"Heartbeat stopped\")
        if self.health_check_task and self.health_check_task.running:
            self.health_check_task.stop()
            logger.info(\"Health check stopped\")
    
    def _authenticate_app(self):
        \"\"\"Internal: Authenticate the application.\"\"\"
        logger.info(\"Authenticating application...\")
        req = ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret
        d = self.client.send(req)
        d.addCallback(self._on_app_auth_success)
        d.addErrback(self._on_error)
    
    def _on_app_auth_success(self, result):
        \"\"\"Internal: Handle successful app authentication.\"\"\"
        logger.info(\"Application authenticated successfully\")
        self.is_app_authed = True
        
        if self.account_id and self.account_id &gt; 0:
            self._authorize_account()
        else:
            logger.warning(
                \"No valid account credentials set - connection may close \"
                f\"(account_id={self.account_id}, access_token={'set' if self.access_token else 'not set'})\"
            )
        
        if self._on_connect_callback:
            self._on_connect_callback()
    
    def _authorize_account(self):
        \"\"\"Internal: Authorize the trading account after app auth.\"\"\"
        logger.info(f\"Authorizing account {self.account_id}...\")
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = self.account_id
        req.accessToken = self.access_token or \"\"
        d = self.client.send(req)
        d.addCallback(self._on_account_auth_success)
        d.addErrback(self._on_error)
    
    def _on_account_auth_success(self, result):
        \"\"\"Internal: Handle successful account authorization.\"\"\"
        logger.info(f\"Account {self.account_id} authorized successfully\")
        self.is_account_authed = True
        # After account is authorized, load symbol map for this account
        self._load_symbol_map()
    
    def _load_symbol_map(self):
        if not self.account_id:
            return
        logger.info(f\"Loading symbol map for account {self.account_id}...\")
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self.account_id
        d = self.client.send(req)

        def _on_symbols_list(result):
            try:
                extracted = Protobuf.extract(result)
                # DEBUG: dump raw response once to inspect available fields
                logger.info(\"Raw symbols list response: %r\", extracted)
                # Try common container field names
                if hasattr(extracted, \"symbol\"):
                    symbols = extracted.symbol
                elif hasattr(extracted, \"symbolList\"):
                    symbols = extracted.symbolList
                else:
                    logger.error(f\"Unexpected symbols list response: {extracted}\")
                    return
                count = 0
                self.symbol_name_to_id.clear()
                self.symbol_details.clear()
                for s in symbols:
                    name = s.symbolName.upper()
                    self.symbol_name_to_id[name] = s.symbolId
                    self.symbol_details[s.symbolId] = s
                    count += 1
                logger.info(
                    f\"Loaded {count} symbols for account {self.account_id}\"
                )
            except Exception as e:
                logger.error(f\"Failed to build symbol map: {e}\", exc_info=True)
        d.addCallback(_on_symbols_list)
        d.addErrback(self._on_error)

    def get_symbol_id_by_name(self, name: str) -&gt; Optional[int]:
        \"\"\"Get broker symbolId by symbol name (uppercased).\"\"\"
        return self.symbol_name_to_id.get(name.upper())
    
    def round_price_for_symbol(self, symbol_id: int, price: float) -&gt; float:
        \"\"\"Round a price to the symbol's configured digits, capped at 4.\"\"\"
        symbol = self.symbol_details.get(symbol_id)
        digits = 4
        if symbol and hasattr(symbol, \"digits\"):
            try:
                digits = min(4, int(symbol.digits))
            except Exception:
                digits = 4
        factor = 10 ** digits
        return round(price * factor) / factor
    
    def _on_error(self, failure):
        \"\"\"Internal: Handle errors.\"\"\"
        logger.error(f\"Error: {failure}\")
    
    def set_account_credentials(self, account_id: int, access_token: str):
        \"\"\"Set account credentials for authorization.
        Must be called BEFORE connect() to authorize account automatically.
        \"\"\"
        self.account_id = account_id
        self.access_token = access_token
        logger.info(f\"Account credentials set: ID={account_id}\")
    
    def connect(self, on_connect: Optional[Callable] = None):
        \"\"\"Connect to cTrader and authenticate.\"\"\"
        self._on_connect_callback = on_connect
        logger.info(f\"Connecting to {self.host}:{self.port}...\")
        self.client.startService()
    
    def set_message_callback(self, callback: Callable):
        \"\"\"Register callback for all incoming messages.\"\"\"
        self._on_message_callback = callback
    
    def authenticate_account(self, access_token: str):
        \"\"\"Authenticate a trading account (legacy method).\"\"\"
        self.access_token = access_token
        logger.info(\"Authenticating account...\")
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = int(access_token) # Simplified for demo
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
        label: str = \"MT5_Copy\",
    ):
        \"\"\"Send a market order.\"\"\"
        if not self.is_app_authed:
            raise RuntimeError(\"Not authenticated. Call connect() first.\")
        
        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = account_id
        req.symbolId = symbol_id
        req.orderType = ProtoOAOrderType.MARKET
        req.tradeSide = ProtoOATradeSide.BUY if side.lower() == \"buy\" else ProtoOATradeSide.SELL
        req.volume = volume
        
        if sl:
            req.stopLoss = sl
        if tp:
            req.takeProfit = tp
        
        req.label = label
        
        logger.info(f\"Sending market order: {side} {volume} units of symbol {symbol_id}\")
        d = self.client.send(req)
        def _on_order_response(result):
            try:
                extracted = Protobuf.extract(result)
                logger.info(f\"Order response message: {extracted}\")
            except Exception as e:
                logger.warning(f\"Failed to extract order response: {e}; raw={result}\")
        d.addCallback(_on_order_response)
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
        \"\"\"Modify position SL/TP.
        If symbol_id is provided and symbol details are known, SL/TP will be
        rounded to the symbol's price precision (capped at 4 digits) before sending.
        \"\"\"
        if not self.is_app_authed:
            raise RuntimeError(\"Not authenticated. Call connect() first.\")
        
        # Optional per-symbol rounding
        orig_sl, orig_tp = sl, tp
        if symbol_id is not None:
            if sl is not None:
                sl = self.round_price_for_symbol(symbol_id, sl)
            if tp is not None:
                tp = self.round_price_for_symbol(symbol_id, tp)
        
        req = ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = account_id
        req.positionId = position_id
        
        if sl is not None:
            req.stopLoss = sl
        if tp is not None:
            req.takeProfit = tp
        
        logger.info(
            f\"Modifying position {position_id}: \"
            f\"SL={orig_sl} -&gt; {sl}, TP={orig_tp} -&gt; {tp}, symbol_id={symbol_id}\"
        )
        d = self.client.send(req)
        def _on_amend_response(result):
            try:
                extracted = Protobuf.extract(result)
                logger.info(f\"Amend response message: {extracted}\")
            except Exception as e:
                logger.warning(f\"Failed to extract amend response: {e}; raw={result}\")
        d.addCallback(_on_amend_response)
        d.addErrback(self._on_error)
        return d
    
    def close_position(self, account_id: int, position_id: int, volume: int):
        \"\"\"Close a position.\"\"\"
        if not self.is_app_authed:
            raise RuntimeError(\"Not authenticated. Call connect() first.\")
        
        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = account_id
        req.positionId = position_id
        req.volume = volume
        
        logger.info(f\"Closing position {position_id}: {volume} units\")
        d = self.client.send(req)
        d.addErrback(self._on_error)
        return d
    
    def run(self):
        \"\"\"Start the Twisted reactor (blocking call).\"\"\"
        logger.info(\"Starting reactor...\")
        reactor.run()
    
    def stop(self):
        \"\"\"Stop the reactor and disconnect.\"\"\"
        logger.info(\"Stopping reactor...\")
        self._stop_periodic_tasks()


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
    Convert MT5 lots to cTrader volume in *cents of units*, using
    both MT5 contract info and cTrader symbol specs.

    Args:
        mt5_lots: MT5 lot size to convert
        mt5_contract_size: MT5 contract size (units per lot)
        mt5_volume_min: MT5 minimum volume
        mt5_volume_step: MT5 volume step
        lot_size_cents: cTrader lot size in cents
        min_volume_cents: cTrader minimum volume in cents
        max_volume_cents: cTrader maximum volume in cents
        step_volume_cents: cTrader volume step in cents

    Returns:
        Volume in cents of units for cTrader
    """
    # 1) Underlying units represented on MT5 side
    mt5_units = mt5_lots * mt5_contract_size

    if lot_size_cents <= 0:
        units_per_lot_ctrader = mt5_contract_size or 1.0
    else:
        units_per_lot_ctrader = lot_size_cents / 100.0

    # 2) Map MT5 units into cTrader "lots" for this symbol
    if units_per_lot_ctrader <= 0:
        target_lots_ctrader = mt5_lots
    else:
        target_lots_ctrader = mt5_units / units_per_lot_ctrader

    # 3) Convert cTrader lots back to units, then to cents-of-units
    target_units = target_lots_ctrader * units_per_lot_ctrader
    target_cents = int(round(target_units * 100))

    # 4) Clamp to broker [min, max] in cents
    if min_volume_cents and min_volume_cents > 0:
        target_cents = max(target_cents, min_volume_cents)
    if max_volume_cents and max_volume_cents > 0:
        target_cents = min(target_cents, max_volume_cents)

    # 5) Snap to stepVolume in cents
    if step_volume_cents and step_volume_cents > 0:
        base = min_volume_cents if (min_volume_cents and min_volume_cents > 0) else 0
        steps = (target_cents - base) / step_volume_cents
        steps = round(steps)
        target_cents = base + int(steps) * step_volume_cents

    return max(target_cents, min_volume_cents or 0)
