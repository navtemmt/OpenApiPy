#!/usr/bin/env python3
"""
Authentication helpers extracted from ctrader_client.py.

Move-only refactor: keep CTraderClient attribute names unchanged.
Uses:
  - self.client_id, self.client_secret
  - self.account_id, self.access_token
  - self.client (low-level OpenApiPy Client)
  - self.is_app_authed, self.is_account_authed
  - self._on_error, self._load_symbol_map
  - self._on_connect_callback
"""

import logging
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


def _get_auth_timeout_sec(self) -> int:
    try:
        v = int(getattr(self, "auth_timeout_sec", DEFAULT_AUTH_TIMEOUT_SEC) or DEFAULT_AUTH_TIMEOUT_SEC)
        return v if v > 0 else DEFAULT_AUTH_TIMEOUT_SEC
    except Exception:
        return DEFAULT_AUTH_TIMEOUT_SEC


# ----------------------------------------------------------------------
# Application Authentication
# ----------------------------------------------------------------------

def authenticate_app(self) -> None:
    timeout_sec = _get_auth_timeout_sec(self)

    logger.info(
        "Authenticating application... client_id=%s timeout=%ss",
        str(self.client_id)[:8] + "..." if self.client_id else None,
        timeout_sec,
    )

    if not self.client_id or not self.client_secret:
        logger.error("Client ID / Secret missing")
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
    except Exception:
        logger.exception("Failed to send application auth request")
        return

    def _ok(result):
        return on_app_auth_success(self, result)

    def _err(failure):
        if failure.check(TwistedTimeoutError):
            logger.error(
                "Application auth timed out after %ss for account_id=%s env=%s",
                timeout_sec,
                getattr(self, "account_id", None),
                getattr(self, "host", None),
            )
        else:
            logger.error(
                "Application auth failed for account_id=%s: %s",
                getattr(self, "account_id", None),
                failure,
            )
        return self._on_error(failure)

    d.addCallback(_ok)
    d.addErrback(_err)


def on_app_auth_success(self, result) -> None:
    try:
        payload = Protobuf.extract(result)
    except Exception:
        logger.exception("Failed to extract app auth response")
        return

    if not isinstance(payload, ProtoOAApplicationAuthRes):
        logger.error("Unexpected app auth response type: %s", type(payload))
        return

    logger.info("Application authenticated successfully")
    self.is_app_authed = True

    if self.account_id and self.access_token:
        authorize_account(self)
    else:
        logger.warning(
            "Account credentials not set yet (call set_account_credentials before connect())"
        )


# ----------------------------------------------------------------------
# Account Authentication
# ----------------------------------------------------------------------

def authorize_account(self) -> None:
    timeout_sec = _get_auth_timeout_sec(self)

    if not self.is_app_authed:
        logger.warning("Cannot authorize account before app authentication")
        return

    if not self.account_id or not self.access_token:
        logger.error("Account ID or access token missing")
        return

    logger.info(
        "Authorizing account %s... token_present=%s timeout=%ss",
        self.account_id,
        bool(self.access_token),
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
            logger.debug("Unable to attach addTimeout() to account auth deferred", exc_info=True)
    except Exception:
        logger.exception("Failed to send account auth request")
        return

    def _ok(result):
        return on_account_auth_success(self, result)

    def _err(failure):
        if failure.check(TwistedTimeoutError):
            logger.error(
                "Account auth timed out after %ss for account_id=%s",
                timeout_sec,
                self.account_id,
            )
        else:
            logger.error(
                "Account auth failed for account_id=%s: %s",
                self.account_id,
                failure,
            )
        return self._on_error(failure)

    d.addCallback(_ok)
    d.addErrback(_err)


def on_account_auth_success(self, result) -> None:
    try:
        payload = Protobuf.extract(result)
    except Exception:
        logger.exception("Failed to extract account auth response")
        return

    if not isinstance(payload, ProtoOAAccountAuthRes):
        logger.error("Unexpected account auth response type: %s", type(payload))
        return

    logger.info("Account %s authorized successfully", self.account_id)
    self.is_account_authed = True

    try:
        self._load_symbol_map()
    except Exception:
        logger.exception("Symbol map loading failed")
