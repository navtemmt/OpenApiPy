"""MT5 to cTrader Copy Trading Bridge Server

Receives trade events from MT5 EA via JSON and forwards to cTrader.
"""
import json
import logging
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from twisted.internet import reactor

from ctrader_client import CTraderClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class MT5BridgeHandler(BaseHTTPRequestHandler):
    """HTTP request handler for MT5 trade events."""
    
    # Class-level reference to cTrader client
    ctrader_client = None
    
    def log_message(self, format, *args):
        """Override to use Python logging instead of printing."""
        logger.info(f"{self.address_string()} - {format%args}")
    
    def do_POST(self):
        """Handle POST request with trade event JSON."""
        try:
            # Read request body
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            
            # Parse JSON
            trade_event = json.loads(post_data.decode('utf-8'))
            logger.info(f"Received trade event: {trade_event}")
            
            # Process the trade event
            self._process_trade_event(trade_event)
            
            # Send success response
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = json.dumps({"status": "success", "message": "Trade event received"})
            self.wfile.write(response.encode('utf-8'))
            
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {e}")
            self.send_error(400, "Invalid JSON")
        except Exception as e:
            logger.error(f"Error processing request: {e}", exc_info=True)
            self.send_error(500, str(e))
    
    def do_GET(self):
        """Handle GET request (health check)."""
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        response = json.dumps({
            "status": "online",
            "service": "MT5 to cTrader Bridge",
            "version": "1.0.0"
        })
        self.wfile.write(response.encode('utf-8'))
    
    def _process_trade_event(self, event):
        """Process trade event and forward to cTrader.
        
        MT5 EA sends events with 'action' field (OPEN/MODIFY/CLOSE).
        
        Expected event format from MT5:
        {
            "action": "OPEN|MODIFY|CLOSE",
            "ticket": 12345,
            "symbol": "EURUSD",
            "type": "BUY|SELL",
            "volume": 0.01,
            "price": 1.19779,
            "sl": 0.0,
            "tp": 0.0,
            "magic": 0
        }
        """
        if not self.ctrader_client or not self.ctrader_client.is_app_authed:
            logger.warning("cTrader client not ready, queueing trade event")
            # TODO: Implement queue for events received before cTrader is ready
            return
        
        # Get action from either 'action' or 'event' field, normalize to lowercase
        event_type = event.get('action', event.get('event'))
        if event_type:
            event_type = event_type.lower()
        
        if event_type == 'open':
            self._handle_open(event)
        elif event_type == 'modify':
            self._handle_modify(event)
        elif event_type == 'close':
            self._handle_close(event)
        else:
            logger.error(f"Unknown event type: {event_type}")
    
    def _handle_open(self, event):
        """Handle new order event."""
        # TODO: Map MT5 symbol to cTrader symbol_id
        # TODO: Convert lots to units (MT5 uses lots, cTrader uses units)
        # TODO: Get account_id from configuration
        
        ticket = event.get('ticket')
        symbol = event.get('symbol')
        # MT5 EA sends 'type' (BUY/SELL), not 'side'
        side = event.get('type', event.get('side', 'BUY')).lower()
        # MT5 EA sends 'volume' (in lots), not 'lots'
        volume = event.get('volume', event.get('lots', 0.01))
        sl = event.get('sl', 0.0)
        tp = event.get('tp', 0.0)
        magic = event.get('magic', 0)
        
        logger.info(f"Opening {side} order: {volume} lots of {symbol} (ticket #{ticket}, magic {magic})")
        
        # Example mapping (you'll need to implement proper symbol/account lookup)
        # account_id = 12345  # Your cTrader account ID
        # symbol_id = 1  # Symbol ID for EURUSD on cTrader
        # volume_units = int(volume * 100000)  # Convert lots to units (for forex)
        
        # Uncomment when cTrader is active:
        # self.ctrader_client.send_market_order(
        #     account_id=account_id,
        #     symbol_id=symbol_id,
        #     side=side,
        #     volume=volume_units,
        #     sl=sl if sl > 0 else None,
        #     tp=tp if tp > 0 else None,
        #     label=f"MT5_{ticket}"
        # )
        
        logger.info(f"Order forwarded to cTrader (placeholder)")
    
    def _handle_modify(self, event):
        """Handle position modification event."""
        ticket = event.get('ticket')
        sl = event.get('sl', 0.0)
        tp = event.get('tp', 0.0)
        
        logger.info(f"Modifying position {ticket}: SL={sl}, TP={tp}")
        
        # TODO: Look up cTrader position_id from MT5 ticket mapping
        # position_id = get_ctrader_position_id(ticket)
        # account_id = get_account_id()
        
        # Uncomment when cTrader is active:
        # self.ctrader_client.modify_position(
        #     account_id=account_id,
        #     position_id=position_id,
        #     sl=sl if sl > 0 else None,
        #     tp=tp if tp > 0 else None
        # )
        
        logger.info(f"Position modification forwarded to cTrader (placeholder)")
    
    def _handle_close(self, event):
        """Handle position close event."""
        ticket = event.get('ticket')
        volume = event.get('volume', event.get('lots', 0))
        
        logger.info(f"Closing position {ticket}: {volume} lots")
        
        # TODO: Look up cTrader position_id and convert lots to units
        # position_id = get_ctrader_position_id(ticket)
        # account_id = get_account_id()
        # volume_units = int(volume * 100000)
        
        # Uncomment when cTrader is active:
        # self.ctrader_client.close_position(
        #     account_id=account_id,
        #     position_id=position_id,
        #     volume=volume_units
        # )
        
        logger.info(f"Position close forwarded to cTrader (placeholder)")


def run_http_server(host='127.0.0.1', port=3140):
    """Run HTTP server in a separate thread."""
    server = HTTPServer((host, port), MT5BridgeHandler)
    logger.info(f"MT5 Bridge Server listening on {host}:{port}")
    logger.info(f"Waiting for trade events from MT5 EA...")
    server.serve_forever()


def main():
    """Main entry point for the bridge server."""
    logger.info("Starting MT5 to cTrader Copy Trading Bridge")
    
    # Initialize cTrader client
    logger.info("Initializing cTrader client...")
    ctrader = CTraderClient(env="demo")  # Change to "live" when ready
    
    # Set cTrader client reference in handler
    MT5BridgeHandler.ctrader_client = ctrader
    
    # Define callback for when cTrader connection is ready
    def on_ctrader_connected():
        logger.info("cTrader client authenticated and ready")
        # TODO: Authenticate account if needed
        # ctrader.authenticate_account(access_token="your_token")
    
    # Connect to cTrader
    logger.info("Connecting to cTrader Open API...")
    ctrader.connect(on_connect=on_ctrader_connected)
    
    # Start HTTP server in a separate thread
    server_thread = Thread(target=run_http_server, daemon=True)
    server_thread.start()
    
    # Run Twisted reactor (blocks here)
    logger.info("Starting Twisted reactor...")
    reactor.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutting down bridge server...")
        reactor.stop()
