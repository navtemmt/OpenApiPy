"""HTTP server infrastructure for receiving MT4/MT5 trade events."""
import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

from app_state import logger
from trade_processor import process_trade_event
from event_normalizer import normalize_trade_event


DEDUPE_WINDOW_MS = 2000
_event_dedupe = {}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dedupe_key(data: dict):
    event_type = (data.get("event_type") or data.get("action") or data.get("event") or "").upper()
    ticket = int(data.get("ticket", 0) or 0)
    return event_type, ticket


def _should_drop_duplicate(data: dict) -> bool:
    now = _now_ms()
    key = _dedupe_key(data)

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
    account_manager = None

    def log_message(self, format, *args):
        logger.info(f"{self.address_string()} - {format % args}")

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)

            data = normalize_trade_event(data)

            logger.info(
                f"Received trade event: {data.get('event_type')} "
                f"for ticket {data.get('ticket')}"
            )

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
            logger.error(f"Error processing request: {e}", exc_info=True)
            self.send_error(500, "Internal server error")

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"status": "ok", "service": "MT5-to-cTrader Bridge"}).encode("utf-8")
            )
        else:
            self.send_error(404, "Not found")


def _serve_http(host, port, account_manager):
    MT5BridgeHandler.account_manager = account_manager
    server = HTTPServer((host, port), MT5BridgeHandler)
    logger.info(f"HTTP server listening on {host}:{port}")
    server.serve_forever()


def run_http_servers(host, ports, account_manager):
    threads = []

    for port in ports:
        t = Thread(
            target=_serve_http,
            args=(host, int(port), account_manager),
            daemon=True,
        )
        t.start()
        threads.append(t)

    return threads


def run_http_server(host, port, account_manager):
    _serve_http(host, port, account_manager)
