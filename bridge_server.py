"""HTTP server infrastructure for receiving MT4/MT5 trade events."""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Lock, Thread

from app_state import logger, notify_error, notify_info, notify_warning
from event_normalizer import normalize_trade_event
from trade_processor import process_trade_event

DEDUPE_WINDOW_MS = 2000
_MAX_DEDUPE_CACHE_SIZE = 2000

_event_dedupe: dict[tuple[str, int, str], int] = {}
_event_dedupe_lock = Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value or default)
    except Exception:
        return int(default)


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _event_type_from_data(data: dict) -> str:
    return str(
        (data or {}).get("event_type")
        or (data or {}).get("action")
        or (data or {}).get("event")
        or ""
    ).upper()


def _event_context(
    data: dict,
    client_ip: str = "",
    content_length: int = 0,
    path: str = "",
) -> dict:
    data = data or {}
    ctx = {
        "client_ip": client_ip,
        "path": path,
        "content_length": int(content_length or 0),
        "event_type": _event_type_from_data(data),
        "ticket": _safe_int(data.get("ticket", 0), 0),
        "symbol": data.get("symbol"),
        "magic": data.get("magic"),
        "account_name": data.get("account_name"),
    }
    return {k: v for k, v in ctx.items() if v not in (None, "", [])}


def _dedupe_key(data: dict) -> tuple[str, int, str]:
    event_type = _event_type_from_data(data)
    ticket = _safe_int((data or {}).get("ticket", 0), 0)
    symbol = str((data or {}).get("symbol") or "").upper()
    return event_type, ticket, symbol


def _should_drop_duplicate(data: dict) -> bool:
    now = _now_ms()
    key = _dedupe_key(data)

    with _event_dedupe_lock:
        if len(_event_dedupe) > _MAX_DEDUPE_CACHE_SIZE:
            cutoff = now - (DEDUPE_WINDOW_MS * 4)
            for dedupe_key, ts in list(_event_dedupe.items()):
                if ts < cutoff:
                    _event_dedupe.pop(dedupe_key, None)

        last = _event_dedupe.get(key)
        if last is not None and (now - last) < DEDUPE_WINDOW_MS:
            return True

        _event_dedupe[key] = now
        return False


class MT5BridgeHandler(BaseHTTPRequestHandler):
    account_manager = None
    server_version = "MT5Bridge/1.0"

    def log_message(self, format: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, status_code: int, payload: dict) -> None:
        body = _json_bytes(payload)
        self.send_response(int(status_code))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        content_length = _safe_int(self.headers.get("Content-Length", 0), 0)
        if content_length <= 0:
            raise ValueError("Empty request body")

        raw_body = self.rfile.read(content_length)

        try:
            body = raw_body.decode("utf-8")
        except Exception as exc:
            raise ValueError(f"Request body is not valid UTF-8: {exc}") from exc

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError("JSON payload must be an object")

        return data

    def do_POST(self) -> None:
        client_ip = self.address_string()
        content_length = _safe_int(self.headers.get("Content-Length", 0), 0)

        try:
            raw_data = self._read_json_body()
            data = normalize_trade_event(raw_data)

            if not isinstance(data, dict):
                raise ValueError("Normalized event must be a dictionary")

            event_type = _event_type_from_data(data)
            ticket = _safe_int(data.get("ticket", 0), 0)
            symbol = data.get("symbol")

            logger.info(
                "Received trade event: event_type=%s ticket=%s symbol=%s client_ip=%s",
                event_type,
                ticket,
                symbol,
                client_ip,
            )

            notify_info(
                event="bridge_trade_event_received",
                message="Trade event received by bridge server",
                **_event_context(
                    data,
                    client_ip=client_ip,
                    content_length=content_length,
                    path=self.path,
                ),
            )

            if _should_drop_duplicate(data):
                logger.info(
                    "Dropped duplicate trade event: event_type=%s ticket=%s symbol=%s client_ip=%s",
                    event_type,
                    ticket,
                    symbol,
                    client_ip,
                )
                notify_warning(
                    event="bridge_trade_event_duplicate_dropped",
                    message="Duplicate trade event dropped",
                    **_event_context(
                        data,
                        client_ip=client_ip,
                        content_length=content_length,
                        path=self.path,
                    ),
                )
                self._send_json(
                    200,
                    {
                        "status": "success",
                        "message": "Duplicate trade event dropped",
                        "duplicate": True,
                        "event_type": event_type,
                        "ticket": ticket,
                    },
                )
                return

            process_trade_event(data, self.account_manager)

            notify_info(
                event="bridge_trade_event_processed",
                message="Trade event processed",
                **_event_context(
                    data,
                    client_ip=client_ip,
                    content_length=content_length,
                    path=self.path,
                ),
            )

            self._send_json(
                200,
                {
                    "status": "success",
                    "message": "Trade event processed",
                    "duplicate": False,
                    "event_type": event_type,
                    "ticket": ticket,
                },
            )

        except ValueError as exc:
            logger.warning("Rejected request: %s", exc)
            notify_warning(
                event="bridge_bad_request",
                message=str(exc),
                client_ip=client_ip,
                path=self.path,
                content_length=content_length,
            )
            self._send_json(
                400,
                {
                    "status": "error",
                    "message": str(exc),
                },
            )

        except Exception as exc:
            logger.error("Error processing request: %s", exc, exc_info=True)
            notify_error(
                event="bridge_request_processing_failed",
                message=str(exc),
                exc=exc,
                client_ip=client_ip,
                path=self.path,
                content_length=content_length,
            )
            self._send_json(
                500,
                {
                    "status": "error",
                    "message": "Internal server error",
                },
            )

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "MT5-to-cTrader Bridge",
                    "dedupe_window_ms": DEDUPE_WINDOW_MS,
                },
            )
            return

        self._send_json(
            404,
            {
                "status": "error",
                "message": "Not found",
            },
        )


def _serve_http(host: str, port: int, account_manager) -> None:
    MT5BridgeHandler.account_manager = account_manager
    server = HTTPServer((host, int(port)), MT5BridgeHandler)

    logger.info("HTTP server listening on %s:%s", host, int(port))
    notify_info(
        event="bridge_http_server_started",
        message="HTTP bridge server started",
        host=host,
        port=int(port),
    )

    server.serve_forever()


def run_http_servers(host: str, ports: list[int], account_manager):
    threads: list[Thread] = []

    for port in ports:
        thread = Thread(
            target=_serve_http,
            args=(host, int(port), account_manager),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    return threads


def run_http_server(host: str, port: int, account_manager) -> None:
    _serve_http(host, port, account_manager)
