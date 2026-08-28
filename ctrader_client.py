#!/usr/bin/env python3
"""
CTrader Open API Client Wrapper for MT5→cTrader Copy Trading
Provides high-level trading methods wrapping the low-level OpenApiPy SDK.
"""

import json
import os
import time
import threading
from typing import Optional, Callable, Dict, Any, Iterable, List

from dotenv import load_dotenv
from twisted.internet import reactor

from app_state import logger, notify_info, notify_warning, notify_error
from ctrader_utils import convert_mt5_lots_to_ctrader_cents  # kept for compatibility
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

PROTO_OA_SPOT_EVENT_TYPE = ProtoOASpotEvent().payloadType


class _SharedTokenState:
    """
    Process-wide token coordinator keyed by canonical token_state_file.

    Goal:
    - If multiple CTraderClient instances share the same token state file,
      they also share one in-memory token snapshot and one refresh lock.
    - This does not require changing external call sites.
    """

    def __init__(self, state_file: str):
        self.state_file = os.path.normpath(state_file)
        self.lock = threading.RLock()
        self.access_token: str = ""
        self.refresh_token: str = ""
        self.expires_at: Optional[int] = None
        self.updated_at: int = 0
        self.last_source: str = "init"


_SHARED_TOKEN_STATES: Dict[str, _SharedTokenState] = {}
_SHARED_TOKEN_STATES_LOCK = threading.RLock()


def _get_shared_token_state(state_file: Optional[str]) -> Optional[_SharedTokenState]:
    if not state_file:
        return None

    norm = os.path.normpath(state_file)
    with _SHARED_TOKEN_STATES_LOCK:
        state = _SHARED_TOKEN_STATES.get(norm)
        if state is None:
            state = _SharedTokenState(norm)
            _SHARED_TOKEN_STATES[norm] = state
        return state


class CTraderClient:
    """High-level wrapper for cTrader Open API trading operations."""

    def __init__(
        self,
        env: str = "demo",
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ):
        load_dotenv()

        self.env = env
        self.client_id = client_id or os.getenv("CTRADER_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("CTRADER_CLIENT_SECRET")

        if not self.client_id or not self.client_secret:
            raise ValueError(
                "client_id and client_secret must be provided "
                "(either via constructor or CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET in .env)"
            )

        self.host = (
            EndPoints.PROTOBUF_LIVE_HOST
            if str(env).strip().lower() == "live"
            else EndPoints.PROTOBUF_DEMO_HOST
        )
        self.port = EndPoints.PROTOBUF_PORT

        self.client = Client(self.host, self.port, TcpProtocol)

        self.default_request_timeout = self._env_int("CTRADER_REQUEST_TIMEOUT_SEC", 30)
        self.auth_timeout_sec = self._env_int("CTRADER_AUTH_TIMEOUT_SEC", 30)

        try:
            self.client.reactor = reactor
        except Exception:
            pass

        self._raw_client_send = self.client.send
        self.client.send = self._client_send_with_timeout

        self.is_connected = False
        self.is_app_authed = False
        self.is_account_authed = False

        self.account_id: Optional[int] = None
        self.account_name: Optional[str] = None

        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expires_at: Optional[int] = None
        self.current_token_source: Optional[str] = None

        self.bootstrap_access_token: Optional[str] = None
        self.bootstrap_refresh_token: Optional[str] = None
        self.token_state_file: Optional[str] = None
        self.shared_token_state: Optional[_SharedTokenState] = None

        self.auth_failed = False
        self.auth_failure_reason: Optional[str] = None
        self._auth_recovery_steps = set()

        self.symbol_name_to_id: Dict[str, int] = {}
        self.symbol_details: Dict[int, object] = {}

        self.spot_quotes: Dict[int, Dict[str, Any]] = {}

        self.heartbeat_task = None
        self.health_check_task = None
        self._heartbeat_start_call = None
        self._healthcheck_start_call = None
        self.heartbeat_interval = 30
        self.last_message_time = time.time()
        self.max_idle_time = 120

        self._on_connect_callback: Optional[Callable] = None
        self._on_message_callback: Optional[Callable] = None
        self._state_lock = threading.RLock()

        self.client.setConnectedCallback(self._handle_connected)
        self.client.setDisconnectedCallback(self._handle_disconnected)
        self.client.setMessageReceivedCallback(self._handle_message)

        logger.info(
            "CTraderClient initialized (%s) host=%s port=%s request_timeout=%ss auth_timeout=%ss client_id_present=%s client_secret_present=%s",
            env,
            self.host,
            self.port,
            self.default_request_timeout,
            self.auth_timeout_sec,
            bool(self.client_id),
            bool(self.client_secret),
        )

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
            return value if value > 0 else default
        except Exception:
            return default

    @staticmethod
    def _env_bool(name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")

    @staticmethod
    def _mask_token(token: Optional[str]) -> str:
        if not token:
            return "<empty>"
        if len(token) <= 12:
            return token[:4] + "..."
        return f"{token[:6]}...{token[-4:]}"

    @staticmethod
    def _to_int(value, default=0) -> int:
        try:
            return int(float(value))
        except Exception:
            return int(default)

    @staticmethod
    def _normalize_string(value: Optional[str]) -> str:
        try:
            return str(value or "").strip()
        except Exception:
            return ""

    def _default_token_state_file(self) -> str:
        token_dir = os.getenv("CTRADER_TOKEN_STATE_DIR", "runtime_tokens")
        base_name = (self.account_name or str(self.account_id or "unknown")).strip()
        return os.path.normpath(os.path.join(token_dir, f"{base_name}.json"))

    def _normalize_token_state_file(self, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        return os.path.normpath(path)

    def _normalize_symbol_ids(self, symbol_ids: Iterable[int]) -> List[int]:
        normalized = []
        seen = set()

        for raw in list(symbol_ids or []):
            try:
                sid = int(raw)
            except Exception:
                continue
            if sid <= 0 or sid in seen:
                continue
            seen.add(sid)
            normalized.append(sid)

        return normalized

    def _client_context(self, **extra) -> Dict[str, Any]:
        ctx = {
            "account_name": self.account_name,
            "account_id": self.account_id,
            "environment": self.env,
            "host": self.host,
            "token_state_file": self.token_state_file,
            "token_source": self.current_token_source,
            "is_connected": self.is_connected,
            "is_app_authed": self.is_app_authed,
            "is_account_authed": self.is_account_authed,
        }
        ctx.update(extra)
        return ctx

    def _sync_from_shared_state(self, reason: str = "sync") -> bool:
        shared = self.shared_token_state
        if not shared:
            return False

        with shared.lock:
            if not (shared.access_token or shared.refresh_token):
                return False

            changed = (
                (self.access_token or "") != (shared.access_token or "")
                or (self.refresh_token or "") != (shared.refresh_token or "")
                or (self.token_expires_at or None) != (shared.expires_at or None)
            )

            self.access_token = shared.access_token or ""
            self.refresh_token = shared.refresh_token or ""
            self.token_expires_at = shared.expires_at
            self.current_token_source = f"shared:{shared.last_source}"

        if changed:
            logger.info(
                "[%s] Synced runtime tokens from shared state reason=%s access_token=%s refresh_present=%s expires_at=%s state_file=%s",
                self.account_name or self.account_id,
                reason,
                self._mask_token(self.access_token),
                bool(self.refresh_token),
                self.token_expires_at,
                self.token_state_file,
            )
        return changed

    def _publish_to_shared_state(self, source: str = "runtime") -> None:
        shared = self.shared_token_state
        if not shared:
            return

        with shared.lock:
            shared.access_token = self.access_token or ""
            shared.refresh_token = self.refresh_token or ""
            shared.expires_at = self.token_expires_at
            shared.updated_at = int(time.time())
            shared.last_source = source

    def _apply_runtime_tokens(
        self,
        access_token: Optional[str],
        refresh_token: Optional[str] = None,
        expires_at: Optional[int] = None,
        source: str = "runtime",
        persist: bool = False,
        publish_shared: bool = True,
    ) -> None:
        with self._state_lock:
            self.access_token = access_token or ""
            self.refresh_token = refresh_token or self.refresh_token or ""
            self.token_expires_at = int(expires_at) if expires_at else None
            self.current_token_source = source
            self.auth_failed = False
            self.auth_failure_reason = None

            if publish_shared:
                self._publish_to_shared_state(source=source)

            logger.info(
                "[%s] Runtime tokens updated source=%s access_token=%s refresh_present=%s expires_at=%s",
                self.account_name or self.account_id,
                source,
                self._mask_token(self.access_token),
                bool(self.refresh_token),
                self.token_expires_at,
            )

            if persist:
                self._save_token_state(source=source)

    def _load_token_state(self) -> Optional[dict]:
        if not self.token_state_file:
            return None

        try:
            with open(self.token_state_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                raise ValueError("token state payload is not a JSON object")
            return payload
        except FileNotFoundError:
            return None
        except Exception as e:
            notify_error(
                event="ctrader_token_state_read_failed",
                message=f"Failed to read token state file: {self.token_state_file}",
                exc=e,
                **self._client_context(),
            )
            return None

    def _save_token_state(self, source: str = "runtime") -> None:
        if not self.token_state_file:
            return

        shared = self.shared_token_state
        lock = shared.lock if shared else None

        def _write():
            parent = os.path.dirname(self.token_state_file)
            if parent:
                os.makedirs(parent, exist_ok=True)

            payload = {
                "account_id": self.account_id,
                "account_name": self.account_name,
                "access_token": self.access_token or "",
                "refresh_token": self.refresh_token or "",
                "expires_at": self.token_expires_at,
                "updated_at": int(time.time()),
                "source": source,
            }

            tmp_path = self.token_state_file + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.token_state_file)

            logger.info(
                "[%s] Token state saved file=%s source=%s access_token=%s refresh_present=%s",
                self.account_name or self.account_id,
                self.token_state_file,
                source,
                self._mask_token(self.access_token),
                bool(self.refresh_token),
            )

        try:
            if lock:
                with lock:
                    _write()
            else:
                _write()
        except Exception as e:
            notify_error(
                event="ctrader_token_state_save_failed",
                message=f"Failed to save token state file: {self.token_state_file}",
                exc=e,
                **self._client_context(source=source),
            )

    def _use_bootstrap_tokens(self, source: str = "env_fallback") -> bool:
        access_token = self.bootstrap_access_token or ""
        refresh_token = self.bootstrap_refresh_token or ""

        if not access_token and not refresh_token:
            notify_warning(
                event="ctrader_bootstrap_tokens_missing",
                message="No bootstrap .env tokens available for fallback",
                **self._client_context(source=source),
            )
            return False

        same_access = (access_token or "") == (self.access_token or "")
        same_refresh = (refresh_token or "") == (self.refresh_token or "")
        if same_access and same_refresh:
            notify_warning(
                event="ctrader_bootstrap_tokens_same_as_runtime",
                message="Bootstrap .env tokens are identical to current runtime tokens; skipping fallback",
                **self._client_context(source=source),
            )
            return False

        self._apply_runtime_tokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=None,
            source=source,
            persist=False,
            publish_shared=True,
        )
        return True

    def _load_startup_tokens(self) -> None:
        force_env = self._env_bool("CTRADER_FORCE_ENV_TOKENS", False)
        clear_state = self._env_bool("CTRADER_CLEAR_TOKEN_STATE_ON_START", False)

        if not self.token_state_file:
            self.token_state_file = self._default_token_state_file()
        else:
            self.token_state_file = self._normalize_token_state_file(self.token_state_file)

        self.shared_token_state = _get_shared_token_state(self.token_state_file)

        if clear_state and self.token_state_file and os.path.exists(self.token_state_file):
            try:
                if self.shared_token_state:
                    with self.shared_token_state.lock:
                        os.remove(self.token_state_file)
                        self.shared_token_state.access_token = ""
                        self.shared_token_state.refresh_token = ""
                        self.shared_token_state.expires_at = None
                        self.shared_token_state.updated_at = int(time.time())
                        self.shared_token_state.last_source = "cleared_on_start"
                else:
                    os.remove(self.token_state_file)

                notify_warning(
                    event="ctrader_token_state_cleared_on_start",
                    message="Deleted token state file on startup due to CTRADER_CLEAR_TOKEN_STATE_ON_START=1",
                    **self._client_context(),
                )
            except FileNotFoundError:
                pass
            except Exception as e:
                notify_error(
                    event="ctrader_token_state_clear_failed",
                    message=f"Failed to delete token state file on startup: {self.token_state_file}",
                    exc=e,
                    **self._client_context(),
                )

        if self._sync_from_shared_state(reason="startup_preload"):
            return

        if force_env:
            notify_warning(
                event="ctrader_force_env_tokens",
                message="FORCE_ENV_TOKENS enabled; ignoring token state file and using .env bootstrap tokens",
                **self._client_context(),
            )
            self._apply_runtime_tokens(
                access_token=self.bootstrap_access_token or "",
                refresh_token=self.bootstrap_refresh_token or "",
                expires_at=None,
                source="env",
                persist=True,
                publish_shared=True,
            )
            return

        state = self._load_token_state()
        if state:
            state_access = state.get("access_token") or ""
            state_refresh = state.get("refresh_token") or self.bootstrap_refresh_token or ""
            expires_at = state.get("expires_at")
            try:
                expires_at = int(expires_at) if expires_at else None
            except Exception:
                expires_at = None

            self._apply_runtime_tokens(
                access_token=state_access or self.bootstrap_access_token or "",
                refresh_token=state_refresh,
                expires_at=expires_at,
                source="state_file",
                persist=False,
                publish_shared=True,
            )
            logger.info(
                "[%s] Loaded tokens from state file file=%s access_token=%s refresh_present=%s expires_at=%s",
                self.account_name or self.account_id,
                self.token_state_file,
                self._mask_token(self.access_token),
                bool(self.refresh_token),
                self.token_expires_at,
            )
            return

        self._apply_runtime_tokens(
            access_token=self.bootstrap_access_token or "",
            refresh_token=self.bootstrap_refresh_token or "",
            expires_at=None,
            source="env",
            persist=True,
            publish_shared=True,
        )
        logger.info(
            "[%s] Token state file missing/unusable; using .env bootstrap tokens file=%s access_token=%s refresh_present=%s",
            self.account_name or self.account_id,
            self.token_state_file,
            self._mask_token(self.access_token),
            bool(self.refresh_token),
        )

    def _sync_runtime_tokens_before_auth(self) -> None:
        self._sync_from_shared_state(reason="pre_auth")

    def _cancel_delayed_starts(self):
        for attr_name in ("_heartbeat_start_call", "_healthcheck_start_call"):
            delayed = getattr(self, attr_name, None)
            try:
                if delayed is not None and delayed.active():
                    delayed.cancel()
            except Exception:
                pass
            setattr(self, attr_name, None)

    def with_shared_token_lock(self, fn: Callable[..., Any], *args: Any, **kwargs: Any):
        shared = self.shared_token_state
        if not shared:
            return fn(*args, **kwargs)

        with shared.lock:
            self._sync_from_shared_state(reason="lock_enter")
            return fn(*args, **kwargs)

    def _client_send_with_timeout(self, req, timeout=None):
        effective_timeout = timeout
        if effective_timeout is None:
            effective_timeout = self.default_request_timeout

        req_name = type(req).__name__

        if effective_timeout:
            try:
                return self._raw_client_send(req, timeout=effective_timeout)
            except TypeError:
                logger.debug(
                    "Low-level client.send() does not accept timeout kwarg for %s; falling back",
                    req_name,
                )
            except Exception as e:
                notify_error(
                    event="ctrader_send_with_timeout_failed",
                    message=f"Low-level client.send(timeout={effective_timeout}) failed for {req_name}",
                    exc=e,
                    **self._client_context(request_type=req_name, timeout=effective_timeout),
                )
                raise

        d = self._raw_client_send(req)

        if effective_timeout and hasattr(d, "addTimeout"):
            try:
                d.addTimeout(effective_timeout, reactor)
            except Exception:
                logger.debug(
                    "Unable to attach addTimeout(%s) for %s",
                    effective_timeout,
                    req_name,
                    exc_info=True,
                )

        return d

    def _handle_connected(self, client):
        notify_info(
            event="ctrader_connected",
            message="Connected to cTrader Open API",
            **self._client_context(),
        )

        self.is_connected = True
        self.last_message_time = time.time()
        self._cancel_delayed_starts()
        self._stop_periodic_tasks()

        self._sync_runtime_tokens_before_auth()
        self._authenticate_app()

        self._heartbeat_start_call = reactor.callLater(5, self._start_heartbeat)
        self._healthcheck_start_call = reactor.callLater(5, self._start_health_check)

        if self._on_connect_callback:
            try:
                self._on_connect_callback()
            except Exception as e:
                notify_error(
                    event="ctrader_on_connect_callback_crashed",
                    message="on_connect callback crashed",
                    exc=e,
                    **self._client_context(),
                )

    def _handle_disconnected(self, client, reason):
        notify_warning(
            event="ctrader_disconnected",
            message=f"Disconnected from cTrader: {reason}",
            **self._client_context(reason=str(reason)),
        )

        self.is_connected = False
        self.is_app_authed = False
        self.is_account_authed = False
        self.symbol_name_to_id.clear()
        self.symbol_details.clear()
        self.spot_quotes.clear()

        self._cancel_delayed_starts()
        self._stop_periodic_tasks()

        if self.auth_failed:
            notify_error(
                event="ctrader_auth_failed_disconnected",
                message="Bot remains stopped for trading due to auth failure",
                **self._client_context(reason=self.auth_failure_reason),
            )

    def _handle_message(self, client, message):
        self.last_message_time = time.time()

        extracted = None
        payload_type = None
        try:
            extracted = Protobuf.extract(message)
            payload_type = getattr(extracted, "payloadType", None)
            logger.debug(
                "Received message payloadType=%s type=%s",
                payload_type,
                type(extracted).__name__ if extracted is not None else None,
            )
        except Exception:
            logger.debug("Received raw message (extract failed): %r", message)
            extracted = None

        try:
            if payload_type == PROTO_OA_SPOT_EVENT_TYPE and extracted is not None:
                logger.debug("ROUTE: ProtoOASpotEvent by payloadType")
                self._on_spot_event(extracted)
        except Exception:
            logger.debug("Failed to process spot event", exc_info=True)

        if self._on_message_callback:
            try:
                self._on_message_callback(message)
            except Exception as e:
                notify_error(
                    event="ctrader_user_message_callback_crashed",
                    message="User message callback crashed",
                    exc=e,
                    **self._client_context(),
                )

    def _on_spot_event(self, spot_event: ProtoOASpotEvent):
        spots = list(getattr(spot_event, "spot", []))
        if not spots:
            return

        try:
            for s in spots:
                symbol_id = int(getattr(s, "symbolId", 0) or 0)
                bid_raw = getattr(s, "bid", 0)
                ask_raw = getattr(s, "ask", 0)
                ts = int(getattr(s, "timestamp", 0) or 0)

                if not symbol_id:
                    continue

                bid = float(bid_raw or 0.0)
                ask = float(ask_raw or 0.0)
                self.spot_quotes[symbol_id] = {"bid": bid, "ask": ask, "ts": ts}
        except Exception:
            logger.debug("spot event parse error", exc_info=True)

    def _start_heartbeat(self):
        return monitor_impl.start_heartbeat(self)

    def _send_heartbeat(self):
        return monitor_impl.send_heartbeat(self)

    def _start_health_check(self):
        return monitor_impl.start_health_check(self)

    def _check_connection_health(self):
        return monitor_impl.check_connection_health(self)

    def _stop_periodic_tasks(self):
        return monitor_impl.stop_periodic_tasks(self)

    def _authenticate_app(self):
        return auth_impl.authenticate_app(self)

    def _on_app_auth_success(self, result):
        return auth_impl.on_app_auth_success(self, result)

    def _authorize_account(self):
        self._sync_runtime_tokens_before_auth()
        return auth_impl.authorize_account(self)

    def _on_account_auth_success(self, result):
        return auth_impl.on_account_auth_success(self, result)

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

    def subscribe_spots(self, account_id: int, symbol_ids: Iterable[int]):
        normalized_ids = self._normalize_symbol_ids(symbol_ids)
        if not normalized_ids:
            notify_warning(
                event="ctrader_subscribe_spots_empty",
                message="subscribe_spots called with no valid symbol ids",
                **self._client_context(account_id=account_id),
            )
            return None

        req = ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = int(account_id)
        req.symbolId.extend(normalized_ids)
        return self.send(req)

    def unsubscribe_spots(self, account_id: int, symbol_ids: Iterable[int]):
        normalized_ids = self._normalize_symbol_ids(symbol_ids)
        if not normalized_ids:
            notify_warning(
                event="ctrader_unsubscribe_spots_empty",
                message="unsubscribe_spots called with no valid symbol ids",
                **self._client_context(account_id=account_id),
            )
            return None

        req = ProtoOAUnsubscribeSpotsReq()
        req.ctidTraderAccountId = int(account_id)
        req.symbolId.extend(normalized_ids)
        return self.send(req)

    def get_last_quote(self, symbol_id: int) -> Optional[Dict[str, Any]]:
        try:
            return self.spot_quotes.get(int(symbol_id))
        except Exception:
            return None

    def _on_error(self, failure):
        logger.error("Deferred error: %s", failure)
        try:
            failure.printTraceback()
        except Exception:
            pass

    def set_account_credentials(
        self,
        account_id: int,
        access_token: str,
        refresh_token: str = "",
        token_state_file: Optional[str] = None,
        account_name: Optional[str] = None,
    ):
        self.account_id = int(account_id)
        self.account_name = account_name or str(account_id)

        self.bootstrap_access_token = access_token or ""
        self.bootstrap_refresh_token = refresh_token or ""
        self.token_state_file = self._normalize_token_state_file(
            token_state_file or self._default_token_state_file()
        )
        self.shared_token_state = _get_shared_token_state(self.token_state_file)

        self._load_startup_tokens()

        logger.info(
            "[%s] Account credentials set account_id=%s token_source=%s state_file=%s bootstrap_access=%s bootstrap_refresh_present=%s shared_state=%s",
            self.account_name,
            self.account_id,
            self.current_token_source,
            self.token_state_file,
            self._mask_token(self.bootstrap_access_token),
            bool(self.bootstrap_refresh_token),
            bool(self.shared_token_state),
        )

    def connect(self, on_connect: Optional[Callable] = None):
        self._on_connect_callback = on_connect
        logger.info("Connecting to %s:%s...", self.host, self.port)
        self.client.startService()

    def set_message_callback(self, callback: Callable):
        self._on_message_callback = callback

    def send(self, req, timeout=None):
        return self._client_send_with_timeout(req, timeout=timeout)

    def amend_position(
        self,
        account_id: int,
        position_id: int,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        symbol_id: Optional[int] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ):
        return trading_impl.amend_position(
            self,
            account_id=account_id,
            position_id=position_id,
            sl=sl,
            tp=tp,
            symbol_id=symbol_id,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

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
        return trading_impl.send_market_order(
            self,
            account_id=account_id,
            symbol_id=symbol_id,
            side=side,
            volume=volume,
            sl=sl,
            tp=tp,
            label=label,
        )

    def send_pending_order(self, *args: Any, **kwargs: Any):
        return trading_impl.send_pending_order(self, *args, **kwargs)

    def amend_pending_order(
        self,
        account_id: int,
        order_id: int,
        symbol_id: int,
        side: str,
        volume: int,
        pending_type: str,
        stop_price: Optional[float] = None,
        limit_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        expiration_ms: Optional[int] = None,
    ):
        return trading_impl.amend_pending_order(
            self,
            account_id=account_id,
            order_id=order_id,
            symbol_id=symbol_id,
            side=side,
            volume=volume,
            pending_type=pending_type,
            stop_price=stop_price,
            limit_price=limit_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            expiration_ms=expiration_ms,
        )

    def cancel_pending_order(self, account_id: int, order_id: int):
        return trading_impl.cancel_pending_order(
            self,
            account_id=account_id,
            order_id=order_id,
        )

    def modify_position(
        self,
        account_id: int,
        position_id: int,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        symbol_id: Optional[int] = None,
    ):
        return trading_impl.modify_position(
            self,
            account_id=account_id,
            position_id=position_id,
            sl=sl,
            tp=tp,
            symbol_id=symbol_id,
        )

    def close_position(self, *args: Any, **kwargs: Any):
        return trading_impl.close_position(self, *args, **kwargs)

    def run(self):
        logger.info("Starting reactor...")
        if not reactor.running:
            reactor.run()

    def stop(self):
        logger.info("Stopping reactor...")
        self._cancel_delayed_starts()
        self._stop_periodic_tasks()
        if reactor.running:
            reactor.stop()
