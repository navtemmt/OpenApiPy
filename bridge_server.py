"""HTTP server infrastructure for receiving MT4/MT5 trade events."""
import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread, Lock

from app_state import logger, notify_error, notify_warning, notify_info
from trade_processor import process_trade_event
from event_normalizer import normalize_trade_event


DEDUPE_WINDOW_MS = 2000
_event_dedupe = {}
_event_dedupe_lock = Lock()

_sync_state_lock = Lock()
_sync_required = True
_last_trade_event_at_ms = 0
_last_sync_completed_at_ms = 0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_int(value, default=0) -> int:
    try:
        return int(value or default)
    except Exception:
        return int(default)


def _safe_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    try:
        raw = str(value).strip().lower()
    except Exception:
        return default
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    return default


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _event_type_from_data(data: dict) -> str:
    return str(
        (data or {}).get("event_type")
        or (data or {}).get("action")
        or (data or {}).get("event")
        or ""
    ).upper()


def _event_context(data: dict, client_ip: str = "", content_length: int = 0, path: str = "") -> dict:
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
        "reason": data.get("reason"),
        "sync_origin": data.get("syncOrigin") or data.get("origin"),
    }
    return {k: v for k, v in ctx.items() if v not in (None, "", [])}


def _dedupe_key(data: dict):
    event_type = _event_type_from_data(data)
    ticket = _safe_int((data or {}).get("ticket", 0), 0)
    symbol = str((data or {}).get("symbol") or "").upper()
    return event_type, ticket, symbol


def _should_drop_duplicate(data: dict) -> bool:
    now = _now_ms()
    key = _dedupe_key(data)

    with _event_dedupe_lock:
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


def _is_startup_sync_event(data: dict) -> bool:
    data = data or {}

    if _safe_bool(data.get("startupSync"), False):
        return True
    if _safe_bool(data.get("startupRecovery"), False):
        return True
    if _safe_bool(data.get("isStartupSync"), False):
        return True
    if _safe_bool(data.get("recovery"), False):
        return True

    sync_origin = str(
        data.get("syncOrigin")
        or data.get("origin")
        or data.get("source")
        or data.get("reason")
        or ""
    ).strip().lower()

    return sync_origin in ("startup", "startupsync", "startuprecovery", "recovery")


def _is_sync_complete_event(data: dict) -> bool:
    return _event_type_from_data(data) == "SYNC_COMPLETE"


def _is_pending_open_event(data: dict) -> bool:
    return _event_type_from_data(data) == "PENDING_OPEN"


def _get_sync_required() -> bool:
    with _sync_state_lock:
        return bool(_sync_required)


def _set_sync_required(value: bool):
    global _sync_required
    with _sync_state_lock:
        _sync_required = bool(value)


def _mark_runtime_event_seen():
    global _last_trade_event_at_ms
    with _sync_state_lock:
        _last_trade_event_at_ms = _now_ms()


def _mark_startup_sync_completed():
    global _last_trade_event_at_ms, _last_sync_completed_at_ms, _sync_required
    with _sync_state_lock:
        now = _now_ms()
        _sync_required = False
        _last_trade_event_at_ms = now
        _last_sync_completed_at_ms = now


def _response_meta() -> dict:
    with _sync_state_lock:
        return {
            "sync_required": bool(_sync_required),
            "dedupe_window_ms": DEDUPE_WINDOW_MS,
            "last_sync_completed_at_ms": int(_last_sync_completed_at_ms),
        }


def _iter_accounts(account_manager):
    if account_manager is None:
        return []

    for attr in ("accounts", "clients", "account_clients", "ctrader_clients"):
        value = getattr(account_manager, attr, None)
        if isinstance(value, dict):
            return list(value.values())
        if isinstance(value, (list, tuple)):
            return list(value)

    return []


def _account_name_of(account) -> str:
    for attr in ("name", "account_name"):
        value = getattr(account, attr, None)
        if value:
            return str(value)
    return ""


def _account_matches_magic(account, magic) -> bool:
    if magic in (None, "", 0, "0"):
        return True

    magic_str = str(magic).strip()

    route_magic = getattr(account, "route_magic_number", None)
    if route_magic is not None and str(route_magic).strip() == magic_str:
        return True

    magic_numbers = getattr(account, "magic_numbers", None)
    if isinstance(magic_numbers, (list, tuple, set)):
        return magic_str in {str(x).strip() for x in magic_numbers}

    return False


def _account_is_symbol_ready(account) -> bool:
    for attr in ("symbols_ready", "symbol_specs_ready", "full_symbol_specs_ready"):
        value = getattr(account, attr, None)
        if value is not None:
            return bool(value)

    client = getattr(account, "client", None)
    if client is not None:
        for attr in ("symbols_ready", "symbol_specs_ready", "full_symbol_specs_ready"):
            value = getattr(client, attr, None)
            if value is not None:
                return bool(value)

    return False


def _startup_pending_accounts_not_ready(account_manager, data: dict):
    accounts = _iter_accounts(account_manager)
    magic = data.get("magic")
    targeted = []

    for account in accounts:
        if _account_matches_magic(account, magic):
            targeted.append(account)

    if not targeted:
        return []

    not_ready = []
    for account in targeted:
        if not _account_is_symbol_ready(account):
            not_ready.append(_account_name_of(account) or "unknown")

    return not_ready


class MT5BridgeHandler(BaseHTTPRequestHandler):
    account_manager = None
    server_version = "MT5Bridge/1.0"

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, status_code: int, payload: dict):
        full_payload = dict(payload or {})
        full_payload.update(_response_meta())

        body = _json_bytes(full_payload)

        logger.info(
            "HTTP JSON response | path=%s status=%s bytes=%s payload=%s",
            self.path,
            int(status_code),
            len(body),
            body.decode("utf-8", errors="replace"),
        )

        self.send_response(int(status_code))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.warning(
                "Client disconnected before response write | path=%s error=%s",
                self.path,
                e,
            )

    def _read_json_body(self) -> dict:
        content_length = _safe_int(self.headers.get("Content-Length", 0), 0)
        if content_length <= 0:
            raise ValueError("Empty request body")

        raw_body = self.rfile.read(content_length)
        try:
            body = raw_body.decode("utf-8")
        except Exception as e:
            raise ValueError(f"Request body is not valid UTF-8: {e}") from e

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e

        if not isinstance(data, dict):
            raise ValueError("JSON payload must be an object")

        return data

    def do_POST(self):
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
            is_startup_sync = _is_startup_sync_event(data)
            is_sync_complete = _is_sync_complete_event(data)
            is_pending_open = _is_pending_open_event(data)

            logger.info(
                "Received trade event: event_type=%s ticket=%s symbol=%s client_ip=%s startup_sync=%s sync_complete=%s",
                event_type,
                ticket,
                symbol,
                client_ip,
                is_startup_sync,
                is_sync_complete,
            )

            notify_info(
                event="bridge_trade_event_received",
                message="Trade event received by bridge server",
                startup_sync=is_startup_sync,
                sync_complete=is_sync_complete,
                sync_required=_get_sync_required(),
                **_event_context(
                    data,
                    client_ip=client_ip,
                    content_length=content_length,
                    path=self.path,
                ),
            )

            if is_sync_complete:
                logger.info(
                    "SYNC_COMPLETE received | client_ip=%s path=%s sync_required_before=%s last_sync_completed_before=%s",
                    client_ip,
                    self.path,
                    _get_sync_required(),
                    _last_sync_completed_at_ms,
                )

                _mark_startup_sync_completed()

                logger.info(
                    "SYNC_COMPLETE applied | client_ip=%s path=%s sync_required_after=%s last_sync_completed_after=%s",
                    client_ip,
                    self.path,
                    _get_sync_required(),
                    _last_sync_completed_at_ms,
                )

                notify_info(
                    event="bridge_startup_sync_completed",
                    message="Startup sync completed; bridge no longer requires sync",
                    startup_sync=is_startup_sync,
                    sync_complete=True,
                    sync_required=_get_sync_required(),
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
                        "message": "Startup sync completed",
                        "duplicate": False,
                        "event_type": "SYNC_COMPLETE",
                        "ticket": ticket,
                        "startup_sync": is_startup_sync,
                        "sync_complete": True,
                        "sync_required": False,
                        "last_sync_completed_at_ms": _last_sync_completed_at_ms,
                    },
                )
                return

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
                    startup_sync=is_startup_sync,
                    sync_complete=False,
                    sync_required=_get_sync_required(),
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
                        "startup_sync": is_startup_sync,
                        "sync_complete": False,
                    },
                )
                return

            if is_startup_sync and is_pending_open:
                not_ready_accounts = _startup_pending_accounts_not_ready(self.account_manager, data)
                if not_ready_accounts:
                    logger.warning(
                        "Deferring startup pending event | ticket=%s symbol=%s magic=%s target_accounts=%s reason=symbol_specs_not_ready",
                        ticket,
                        symbol,
                        data.get("magic"),
                        not_ready_accounts,
                    )
                    notify_warning(
                        event="bridge_startup_pending_deferred",
                        message="Startup pending order deferred because cTrader symbol specs are not ready",
                        startup_sync=True,
                        sync_complete=False,
                        sync_required=_get_sync_required(),
                        target_accounts=",".join(not_ready_accounts),
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
                            "status": "retry",
                            "message": "Startup pending order deferred; cTrader symbol specs are not ready",
                            "duplicate": False,
                            "retry_required": True,
                            "retry_reason": "symbol_specs_not_ready",
                            "event_type": event_type,
                            "ticket": ticket,
                            "startup_sync": True,
                            "sync_complete": False,
                            "target_accounts": not_ready_accounts,
                        },
                    )
                    return

            process_trade_event(data, self.account_manager)

            _mark_runtime_event_seen()

            notify_info(
                event="bridge_trade_event_processed",
                message="Trade event processed",
                startup_sync=is_startup_sync,
                sync_complete=False,
                sync_required=_get_sync_required(),
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
                    "startup_sync": is_startup_sync,
                    "sync_complete": False,
                },
            )

        except ValueError as e:
            logger.warning("Rejected request: %s", e)
            notify_warning(
                event="bridge_bad_request",
                message=str(e),
                client_ip=client_ip,
                path=self.path,
                content_length=content_length,
                sync_required=_get_sync_required(),
            )
            self._send_json(
                400,
                {
                    "status": "error",
                    "message": str(e),
                },
            )

        except Exception as e:
            logger.error("Error processing request: %s", e, exc_info=True)
            notify_error(
                event="bridge_request_processing_failed",
                message=str(e),
                exc=e,
                client_ip=client_ip,
                path=self.path,
                content_length=content_length,
                sync_required=_get_sync_required(),
            )
            self._send_json(
                500,
                {
                    "status": "error",
                    "message": "Internal server error",
                },
            )

    def do_GET(self):
        if self.path == "/health":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "MT5-to-cTrader Bridge",
                    "last_trade_event_at_ms": _last_trade_event_at_ms,
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


def _serve_http(host, port, account_manager):
    MT5BridgeHandler.account_manager = account_manager
    server = HTTPServer((host, int(port)), MT5BridgeHandler)
    logger.info("HTTP server listening on %s:%s", host, int(port))
    notify_info(
        event="bridge_http_server_started",
        message="HTTP bridge server started",
        host=host,
        port=int(port),
        sync_required=_get_sync_required(),
    )
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
