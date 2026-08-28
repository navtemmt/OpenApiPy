#!/usr/bin/env python3
"""
Authentication helpers extracted from ctrader_client.py.

Move-only refactor: keep CTraderClient attribute names unchanged.
Uses:
  - self.client_id, self.client_secret
  - self.account_id, self.access_token, self.refresh_token
  - self.client (low-level OpenApiPy Client)
  - self.is_app_authed, self.is_account_authed
  - self._on_error, self._load_symbol_map
  - self._on_connect_callback

Shared-token aware update:
  - coordinates refresh through CTraderClient.with_shared_token_lock()
  - re-syncs from shared runtime token state before refresh and auth
  - persists refreshed tokens through self._apply_runtime_tokens(..., persist=True)
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from twisted.internet.defer import TimeoutError as TwistedTimeoutError

from ctrader_open_api import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAApplicationAuthRes,
    ProtoOAAccountAuthReq,
    ProtoOAAccountAuthRes,
)

logger = logging.getLogger(__name__)

DEFAULT_AUTH_TIMEOUT_SEC = 15
TOKEN_REFRESH_URL = "https://openapi.ctrader.com/apps/token"


def _notify_ctx(self, **extra):
    ctx = {
        "account_name": getattr(self, "account_name", None),
        "account_id": getattr(self, "account_id", None),
        "environment": getattr(self, "env", None),
        "host": getattr(self, "host", None),
        "token_source": getattr(self, "current_token_source", None),
        "is_connected": getattr(self, "is_connected", None),
        "is_app_authed": getattr(self, "is_app_authed", None),
        "is_account_authed": getattr(self, "is_account_authed", None),
    }
    ctx.update(extra)
    return ctx


def _notify_info(self, message: str, event: str, **extra):
    fn = getattr(self, "notify_info", None)
    if callable(fn):
        try:
            return fn(event=event, message=message, **_notify_ctx(self, **extra))
        except Exception:
            pass


def _notify_warning(self, message: str, event: str, **extra):
    fn = getattr(self, "notify_warning", None)
    if callable(fn):
        try:
            return fn(event=event, message=message, **_notify_ctx(self, **extra))
        except Exception:
            pass


def _notify_error(self, message: str, event: str, exc=None, **extra):
    fn = getattr(self, "notify_error", None)
    if callable(fn):
        try:
            return fn(event=event, message=message, exc=exc, **_notify_ctx(self, **extra))
        except Exception:
            pass


def _get_auth_timeout_sec(self) -> int:
    try:
        v = int(
            getattr(self, "auth_timeout_sec", DEFAULT_AUTH_TIMEOUT_SEC)
            or DEFAULT_AUTH_TIMEOUT_SEC
        )
        return v if v > 0 else DEFAULT_AUTH_TIMEOUT_SEC
    except Exception:
        return DEFAULT_AUTH_TIMEOUT_SEC


def _mask_token(token: str) -> str:
    if not token:
        return "<empty>"
    if len(token) <= 12:
        return token[:4] + "..."
    return f"{token[:6]}...{token[-4:]}"


def _failure_text(failure) -> str:
    try:
        return failure.getErrorMessage() or str(failure)
    except Exception:
        return str(failure)


def _sync_shared_tokens(self, reason: str = "auth_impl") -> None:
    """
    Best-effort sync from shared token coordinator if ctrader_client.py exposes it.
    """
    try:
        sync_fn = getattr(self, "_sync_from_shared_state", None)
        if callable(sync_fn):
            sync_fn(reason=reason)
    except Exception:
        logger.debug(
            "[%s] Shared token sync failed reason=%s",
            getattr(self, "account_name", None) or getattr(self, "account_id", None),
            reason,
            exc_info=True,
        )


def _run_with_shared_token_lock(self, fn, *args, **kwargs):
    """
    Best-effort shared lock wrapper if ctrader_client.py exposes it.
    Falls back to direct execution.
    """
    wrapper = getattr(self, "with_shared_token_lock", None)
    if callable(wrapper):
        return wrapper(fn, *args, **kwargs)
    return fn(*args, **kwargs)


def _mark_auth_dead(self, reason: str) -> None:
    self.is_account_authed = False
    self.auth_failed = True
    self.auth_failure_reason = reason

    logger.critical(
        "[%s] Account auth unrecoverable. Bot is NOT authorized and will not copy trades "
        "until credentials are fixed. account_id=%s reason=%s",
        getattr(self, "account_name", None) or getattr(self, "account_id", None),
        getattr(self, "account_id", None),
        reason,
    )
    _notify_error(
        self,
        event="ctrader_account_auth_dead",
        message="Account authentication became unrecoverable",
        reason=reason,
    )


def _refresh_access_token(self, reason: str = "") -> bool:
    """
    Refresh access token under shared-token lock so multiple clients sharing the same
    token_state_file do not race refreshes against each other.
    """

    def _do_refresh() -> bool:
        _sync_shared_tokens(self, reason="before_refresh")

        refresh_token = getattr(self, "refresh_token", "") or ""
        if not refresh_token:
            logger.warning(
                "[%s] Cannot refresh token: refresh_token missing. reason=%s",
                getattr(self, "account_name", None) or getattr(self, "account_id", None),
                reason,
            )
            _notify_warning(
                self,
                event="ctrader_refresh_missing_refresh_token",
                message="Cannot refresh cTrader token because refresh_token is missing",
                reason=reason,
            )
            return False

        client_id = getattr(self, "client_id", "") or ""
        client_secret = getattr(self, "client_secret", "") or ""
        if not client_id or not client_secret:
            logger.error(
                "[%s] Cannot refresh token: client_id/client_secret missing",
                getattr(self, "account_name", None) or getattr(self, "account_id", None),
            )
            _notify_error(
                self,
                event="ctrader_refresh_missing_client_credentials",
                message="Cannot refresh cTrader token because client credentials are missing",
            )
            return False

        timeout_sec = _get_auth_timeout_sec(self)

        params = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        url = f"{TOKEN_REFRESH_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            data=b"",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        logger.warning(
            "[%s] Attempting token refresh. account_id=%s token_source=%s refresh_token=%s reason=%s",
            getattr(self, "account_name", None) or getattr(self, "account_id", None),
            getattr(self, "account_id", None),
            getattr(self, "current_token_source", None),
            _mask_token(refresh_token),
            reason,
        )
        _notify_warning(
            self,
            event="ctrader_refresh_attempt",
            message="Attempting cTrader token refresh",
            reason=reason,
            refresh_token=_mask_token(refresh_token),
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
            payload = json.loads(raw)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            logger.error(
                "[%s] Token refresh HTTPError status=%s body=%s",
                getattr(self, "account_name", None) or getattr(self, "account_id", None),
                getattr(e, "code", None),
                body,
            )
            _notify_error(
                self,
                event="ctrader_refresh_http_error",
                message="Token refresh HTTP error",
                exc=e,
                http_status=getattr(e, "code", None),
                response_body=body[:1000],
            )
            return False
        except Exception as e:
            logger.exception(
                "[%s] Token refresh request failed: %s",
                getattr(self, "account_name", None) or getattr(self, "account_id", None),
                e,
            )
            _notify_error(
                self,
                event="ctrader_refresh_request_failed",
                message="Token refresh request failed",
                exc=e,
                reason=reason,
            )
            return False

        if not isinstance(payload, dict):
            logger.error(
                "[%s] Token refresh response is not a JSON object payload=%r",
                getattr(self, "account_name", None) or getattr(self, "account_id", None),
                payload,
            )
            _notify_error(
                self,
                event="ctrader_refresh_invalid_payload",
                message="Token refresh response is not a JSON object",
                response_type=type(payload).__name__,
            )
            return False

        new_access_token = payload.get("accessToken") or payload.get("access_token") or ""
        new_refresh_token = payload.get("refreshToken") or payload.get("refresh_token") or ""
        expires_in = payload.get("expiresIn") or payload.get("expires_in") or 0

        try:
            expires_in = int(expires_in or 0)
        except Exception:
            expires_in = 0

        if not new_access_token:
            logger.error(
                "[%s] Token refresh response missing access token payload=%s",
                getattr(self, "account_name", None) or getattr(self, "account_id", None),
                payload,
            )
            _notify_error(
                self,
                event="ctrader_refresh_missing_access_token",
                message="Token refresh response did not include an access token",
                payload_preview=str(payload)[:1000],
            )
            return False

        expires_at = int(time.time()) + expires_in if expires_in > 0 else None

        self._apply_runtime_tokens(
            access_token=new_access_token,
            refresh_token=new_refresh_token or refresh_token,
            expires_at=expires_at,
            source="refresh",
            persist=True,
        )

        logger.info(
            "[%s] Token refresh success. account_id=%s access_token=%s refresh_token=%s expires_in=%s expires_at=%s",
            getattr(self, "account_name", None) or getattr(self, "account_id", None),
            getattr(self, "account_id", None),
            _mask_token(new_access_token),
            _mask_token(new_refresh_token or refresh_token),
            expires_in,
            expires_at,
        )
        _notify_info(
            self,
            event="ctrader_refresh_success",
            message="cTrader token refresh succeeded",
            expires_in=expires_in,
            expires_at=expires_at,
        )
        return True

    return bool(_run_with_shared_token_lock(self, _do_refresh))


def _recover_account_auth(self, reason: str) -> None:
    """
    Recovery path order:
    1. Sync from shared token state
    2. Try refresh once
    3. Try bootstrap/.env fallback once
    4. Mark auth dead
    """
    if getattr(self, "auth_failed", False):
        logger.warning(
            "[%s] Recovery skipped because auth is already marked dead. reason=%s",
            getattr(self, "account_name", None) or getattr(self, "account_id", None),
            reason,
        )
        return

    self.is_account_authed = False
    _sync_shared_tokens(self, reason="recover_start")

    steps = getattr(self, "_auth_recovery_steps", set())
    if not isinstance(steps, set):
        steps = set()

    if "refresh" not in steps and getattr(self, "refresh_token", None):
        steps.add("refresh")
        self._auth_recovery_steps = steps
        if _refresh_access_token(self, reason=reason):
            logger.warning(
                "[%s] Retrying account authorization after successful refresh",
                getattr(self, "account_name", None) or getattr(self, "account_id", None),
            )
            _notify_warning(
                self,
                event="ctrader_reauth_after_refresh",
                message="Retrying account authorization after token refresh",
                reason=reason,
            )
            authorize_account(self)
            return
        logger.warning(
            "[%s] Refresh recovery failed; will try .env fallback next",
            getattr(self, "account_name", None) or getattr(self, "account_id", None),
        )

    if "env" not in steps:
        steps.add("env")
        self._auth_recovery_steps = steps
        if self._use_bootstrap_tokens(source="env_fallback"):
            logger.warning(
                "[%s] Retrying account authorization with .env fallback tokens",
                getattr(self, "account_name", None) or getattr(self, "account_id", None),
            )
            _notify_warning(
                self,
                event="ctrader_reauth_after_env_fallback",
                message="Retrying account authorization with bootstrap tokens",
                reason=reason,
            )
            authorize_account(self)
            return
        logger.warning(
            "[%s] .env fallback unavailable or unchanged",
            getattr(self, "account_name", None) or getattr(self, "account_id", None),
        )

    _mark_auth_dead(self, reason)


# ----------------------------------------------------------------------
# Application Authentication
# ----------------------------------------------------------------------


def authenticate_app(self) -> None:
    timeout_sec = _get_auth_timeout_sec(self)
    self.is_app_authed = False

    logger.info(
        "Authenticating application... client_id=%s timeout=%ss",
        str(self.client_id)[:8] + "..." if self.client_id else None,
        timeout_sec,
    )

    if not self.client_id or not self.client_secret:
        logger.error("Client ID / Secret missing")
        _notify_error(
            self,
            event="ctrader_app_auth_missing_client_credentials",
            message="Client ID / Secret missing before app authentication",
        )
        return

    req = ProtoOAApplicationAuthReq()
    req.clientId = self.client_id
    req.clientSecret = self.client_secret

    try:
        d = self.client.send(req, timeout=timeout_sec)
    except TypeError:
        d = self.client.send(req)
        try:
            d.addTimeout(timeout_sec, self.client.reactor)
        except Exception:
            logger.debug("Unable to attach addTimeout() to app auth deferred", exc_info=True)
    except Exception as e:
        logger.exception("Failed to send application auth request")
        _notify_error(
            self,
            event="ctrader_app_auth_send_failed",
            message="Failed to send application auth request",
            exc=e,
        )
        return

    def _ok(result):
        return on_app_auth_success(self, result)

    def _err(failure):
        self.is_app_authed = False
        if failure.check(TwistedTimeoutError):
            logger.error(
                "Application auth timed out after %ss for account_id=%s env=%s",
                timeout_sec,
                getattr(self, "account_id", None),
                getattr(self, "host", None),
            )
            _notify_error(
                self,
                event="ctrader_app_auth_timeout",
                message=f"Application auth timed out after {timeout_sec}s",
                exc=Exception(str(failure)),
            )
        else:
            logger.error(
                "Application auth failed for account_id=%s: %s",
                getattr(self, "account_id", None),
                failure,
            )
            _notify_error(
                self,
                event="ctrader_app_auth_failed",
                message="Application auth failed",
                exc=Exception(_failure_text(failure)),
            )
        return self._on_error(failure)

    d.addCallback(_ok)
    d.addErrback(_err)


def on_app_auth_success(self, result) -> None:
    try:
        payload = Protobuf.extract(result)
    except Exception as e:
        logger.exception("Failed to extract app auth response")
        _notify_error(
            self,
            event="ctrader_app_auth_extract_failed",
            message="Failed to extract app auth response",
            exc=e,
        )
        return

    if not isinstance(payload, ProtoOAApplicationAuthRes):
        logger.error("Unexpected app auth response type: %s", type(payload))
        _notify_error(
            self,
            event="ctrader_app_auth_unexpected_response",
            message="Unexpected app auth response type",
            response_type=type(payload).__name__,
        )
        return

    logger.info("Application authenticated successfully")
    self.is_app_authed = True
    _notify_info(
        self,
        event="ctrader_app_auth_success",
        message="Application authenticated successfully",
    )

    _sync_shared_tokens(self, reason="post_app_auth")

    if self.account_id and (
        self.access_token
        or self.refresh_token
        or self.bootstrap_access_token
        or self.bootstrap_refresh_token
    ):
        authorize_account(self)
    else:
        logger.warning(
            "Account credentials not set yet (call set_account_credentials before connect())"
        )
        _notify_warning(
            self,
            event="ctrader_account_credentials_not_set",
            message="Account credentials not set yet; call set_account_credentials before connect",
        )


# ----------------------------------------------------------------------
# Account Authentication
# ----------------------------------------------------------------------


def authorize_account(self) -> None:
    timeout_sec = _get_auth_timeout_sec(self)

    if getattr(self, "auth_failed", False):
        logger.warning(
            "[%s] authorize_account skipped because auth is marked dead",
            getattr(self, "account_name", None) or getattr(self, "account_id", None),
        )
        return

    if not self.is_app_authed:
        logger.warning("Cannot authorize account before app authentication")
        _notify_warning(
            self,
            event="ctrader_account_auth_before_app_auth",
            message="Cannot authorize account before app authentication",
        )
        return

    if not self.account_id:
        logger.error("Account ID missing")
        _notify_error(
            self,
            event="ctrader_account_auth_missing_account_id",
            message="Account ID missing before account authentication",
        )
        return

    self.is_account_authed = False
    _sync_shared_tokens(self, reason="before_authorize_account")

    if not self.access_token:
        logger.warning(
            "[%s] Access token missing before account auth. Trying recovery path.",
            getattr(self, "account_name", None) or getattr(self, "account_id", None),
        )
        _recover_account_auth(self, "access token missing before authorize_account")
        return

    logger.info(
        "Authorizing account %s... token_source=%s token_present=%s refresh_present=%s timeout=%ss",
        self.account_id,
        getattr(self, "current_token_source", None),
        bool(self.access_token),
        bool(getattr(self, "refresh_token", None)),
        timeout_sec,
    )

    req = ProtoOAAccountAuthReq()
    req.ctidTraderAccountId = int(self.account_id)
    req.accessToken = self.access_token

    try:
        d = self.client.send(req, timeout=timeout_sec)
    except TypeError:
        d = self.client.send(req)
        try:
            d.addTimeout(timeout_sec, self.client.reactor)
        except Exception:
            logger.debug(
                "Unable to attach addTimeout() to account auth deferred",
                exc_info=True,
            )
    except Exception as e:
        logger.exception("Failed to send account auth request")
        _notify_error(
            self,
            event="ctrader_account_auth_send_failed",
            message="Failed to send account auth request",
            exc=e,
        )
        _recover_account_auth(self, "send account auth request failed")
        return

    def _ok(result):
        return on_account_auth_success(self, result)

    def _err(failure):
        reason = _failure_text(failure)
        if failure.check(TwistedTimeoutError):
            logger.error(
                "Account auth timed out after %ss for account_id=%s",
                timeout_sec,
                self.account_id,
            )
            reason = f"account auth timeout after {timeout_sec}s"
            _notify_error(
                self,
                event="ctrader_account_auth_timeout",
                message=reason,
                exc=Exception(str(failure)),
            )
        else:
            logger.error(
                "Account auth failed for account_id=%s: %s",
                self.account_id,
                failure,
            )
            _notify_error(
                self,
                event="ctrader_account_auth_failed",
                message="Account auth failed",
                exc=Exception(reason),
            )

        _recover_account_auth(self, reason)
        return self._on_error(failure)

    d.addCallback(_ok)
    d.addErrback(_err)


def on_account_auth_success(self, result) -> None:
    try:
        payload = Protobuf.extract(result)
    except Exception as e:
        logger.exception("Failed to extract account auth response")
        _notify_error(
            self,
            event="ctrader_account_auth_extract_failed",
            message="Failed to extract account auth response",
            exc=e,
        )
        return

    if not isinstance(payload, ProtoOAAccountAuthRes):
        logger.error("Unexpected account auth response type: %s", type(payload))
        _notify_error(
            self,
            event="ctrader_account_auth_unexpected_response",
            message="Unexpected account auth response type",
            response_type=type(payload).__name__,
        )
        _recover_account_auth(self, f"unexpected account auth response type {type(payload)}")
        return

    self.auth_failed = False
    self.auth_failure_reason = None
    self._auth_recovery_steps = set()

    logger.info(
        "Account %s authorized successfully (token_source=%s)",
        self.account_id,
        getattr(self, "current_token_source", None),
    )
    self.is_account_authed = True
    _notify_info(
        self,
        event="ctrader_account_auth_success",
        message="Account authorized successfully",
        token_source=getattr(self, "current_token_source", None),
    )

    if getattr(self, "current_token_source", None) == "env_fallback":
        self._save_token_state(source="env_fallback_recovered")

    try:
        self._load_symbol_map()
    except Exception as e:
        logger.exception("Symbol map loading failed")
        _notify_error(
            self,
            event="ctrader_symbol_map_load_failed_after_auth",
            message="Symbol map loading failed after successful account auth",
            exc=e,
        )
