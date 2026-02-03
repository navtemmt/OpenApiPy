"""HTTP server infrastructure for receiving MT5 trade events.
Simplified HTTP request handler that delegates to trade processor.
"""
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from app_state import logger
from trade_processor import process_trade_event


class MT5BridgeHandler(BaseHTTPRequestHandler):
    """HTTP request handler for MT5 trade events."""
    account_manager = None

    def log_message(self, format, *args):
        """Override to use Python logging instead of printing."""
        logger.info(f"{self.address_string()} - {format % args}")

    def do_POST(self):
        """Handle POST request with trade event JSON."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
            
            # Backward compatibility: accept both 'event' and 'event_type'
            if 'event' in data and 'event_type' not in data:
                data['event_type'] = data['event']

            logger.info(
                f"Received trade event: {data.get('event_type')} "
                f"for ticket {data.get('ticket')}"
            )

            process_trade_event(data, self.account_manager)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"status": "success", "message": "Trade event processed"}
                ).encode("utf-8")
            )

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON received: {e}")
            self.send_error(400, "Invalid JSON")

        except Exception as e:
            logger.error(f"Error processing request: {e}")
            self.send_error(500, "Internal server error")

    def do_GET(self):
        """Handle GET request - health check endpoint."""
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"status": "ok", "service": "MT54to cTrader Bridge"}
                ).encode("utf-8")
            )
        else:
            self.send_error(404, "Not found")


def run_http_server(host, port, account_manager):
    """Start the HTTP server with the given configuration."""
    MT5BridgeHandler.account_manager = account_manager
    server = HTTPServer((host, port), MT5BridgeHandler)
    logger.info(f"HTTP server listening on {host}:{port}")
    server.serve_forever()
