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
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAAccountAuthReq,
)

logger = logging.getLogger(__name__)


def authenticate_app(self) -> None:
    logger.info("Authenticating application...")
    req = ProtoOAApplicationAuthReq()
    req.clientId = self.client_id
    req.clientSecret = self.client_secret

    d = self.client.send(req)
    d.addCallback(lambda result: on_app_auth_success(self, result))
    d.addErrback(self._on_error)


def on_app_auth_success(self, result) -> None:
    logger.info("Application authenticated successfully")
    self.is_app_authed = True

    if self.account_id and self.access_token:
        authorize_account(self)
    else:
        logger.warning(
            "Account credentials not set yet (call set_account_credentials before connect())"
        )

    if self._on_connect_callback:
        try:
            self._on_connect_callback()
        except Exception:
            logger.exception("on_connect callback crashed")


def authorize_account(self) -> None:
    logger.info("Authorizing account %s...", self.account_id)
    req = ProtoOAAccountAuthReq()
    req.ctidTraderAccountId = int(self.account_id)
    req.accessToken = self.access_token

    d = self.client.send(req)
    d.addCallback(lambda result: on_account_auth_success(self, result))
    d.addErrback(self._on_error)


def on_account_auth_success(self, result) -> None:
    logger.info("Account %s authorized successfully", self.account_id)
    self.is_account_authed = True
    self._load_symbol_map()
