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
from ctrader_open_api import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAApplicationAuthRes,
    ProtoOAAccountAuthReq,
    ProtoOAAccountAuthRes,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Application Authentication
# ----------------------------------------------------------------------

def authenticate_app(self) -> None:
    logger.info("Authenticating application...")

    if not self.client_id or not self.client_secret:
        logger.error("Client ID / Secret missing")
        return

    req = ProtoOAApplicationAuthReq()
    req.clientId = self.client_id
    req.clientSecret = self.client_secret

    d = self.client.send(req)
    d.addCallback(lambda result: on_app_auth_success(self, result))
    d.addErrback(self._on_error)


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

    # Only now proceed to account auth
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
    if not self.is_app_authed:
        logger.warning("Cannot authorize account before app authentication")
        return

    if not self.account_id or not self.access_token:
        logger.error("Account ID or access token missing")
        return

    logger.info("Authorizing account %s...", self.account_id)

    req = ProtoOAAccountAuthReq()
    req.ctidTraderAccountId = int(self.account_id)
    req.accessToken = self.access_token

    d = self.client.send(req)
    d.addCallback(lambda result: on_account_auth_success(self, result))
    d.addErrback(self._on_error)


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

    # Load symbols only AFTER confirmed account auth
    try:
        self._load_symbol_map()
    except Exception:
        logger.exception("Symbol map loading failed")
