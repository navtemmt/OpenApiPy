"""HTTP server infrastructure for receiving MT5 trade events.
Simplified HTTP request handler that delegates to trade processor.
"""
import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

from app_state import logger
from trade_processor import process_trade_event


# --- PATCH: lightweight HTTP-layer de-dupe ---
# Protects against accidental duplicate POSTs (e.g., TT + polling, retries, multi-chart).
DEDUPE_WINDOW_MS = 1500
_event_dedupe = {}  # key -> last_seen_ms


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dedupe_key(data: dict):
    event_type = (data.get("event_type") or data.get("action") or data.get("event") or "").upper()
    ticket = int(data.get("ticket", 0) or 0)
    # Include symbol when present to avoid rare collisions on ticket=0 / malformed payloads
    symbol = str(data.get("symbol") or "")
    return event_type, ticket, symbol


def _should_drop_duplicate(data: dict) -> bool:
    now = _now_ms()
    key = _dedupe_key(data)

    # prune occasionally (cheap)
    if len(_event_dedupe) > 2000:
        cutoff = now - (DEDUPE_WINDOW_MS * 4)
        for k, ts in list(_event_dedupe.items()):
            if ts < cutoff:
                _event_dedupe.pop(k, None)

    last = _event_dedupe.get(key)
    if last is not None and (now - last) < DEDUPE_WINDOW_MS:
        return True

    _event_dedupe[key] = now
    return False


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

            # Backward compatibility: normalize field names
            # Support: 'action' (MT5), 'event' (old), 'event_type' (new)
            if "event_type" not in data:
                if "action" in data:
                    data["event_type"] = str(data["action"]).upper()
                elif "event" in data:
                    data["event_type"] = str(data["event"]).upper()

            # Normalize 'type' to 'side' (MT5 sends 'type': 'BUY'/'SELL')
            if "type" in data and "side" not in data:
                data["side"] = data["type"]

            logger.info(
                f"Received trade event: {data.get('event_type')} "
                f"for ticket {data.get('ticket')}"
            )

            # Drop duplicates fast (still return 200 so MT5 won't retry)
            if _should_drop_duplicate(data):
                logger.info(
                    f"Dropped duplicate trade event: {data.get('event_type')} "
                    f"ticket={data.get('ticket')} symbol={data.get('symbol')}"
                )
            else:
                process_trade_event(data, self.account_manager)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"status": "success", "message": "Trade event processed"}).encode("utf-8")
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
                json.dumps({"status": "ok", "service": "MT5$to cTrader Bridge"}).encode("utf-8")
            )
        else:
            self.send_error(404, "Not found")


def run_http_server(host, port, account_manager):
    """Start the HTTP server with the given configuration."""
    MT5BridgeHandler.account_manager = account_manager
    server = HTTPServer((host, port), MT5BridgeHandler)
    logger.info(f"HTTP server listening on {host}:{port}")
    server.serve_forever()
